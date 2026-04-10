"""Auto-poke and agent activity hooks."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mindroom.hooks import (
    AfterResponseContext,
    AgentLifecycleContext,
    CancelledResponseContext,
    EnrichmentItem,
    MessageEnrichContext,
    hook,
)

from .state import (
    agent_state_path,
    locked_update_json,
    now_iso,
    poke_agent_scope,
    read_agent_state,
    response_scope_thread_id,
    todos_path,
)
from .todos import is_actionable, is_blocked, read_todos
from .types import (
    AutoPokeRuntime,
    DEFAULT_POKE_INTERVAL_SECONDS,
    PRIORITY_EMOJI,
    PRIORITY_ORDER,
    PokeScanContext,
    ROUTER_AGENT_NAME,
    TERMINAL_STATUSES,
    _PLUGIN_NAME,
    logger,
)

_AUTO_POKE_TASK: asyncio.Task[None] | None = None


def _clear_active_run(
    state_root: Path,
    agent_name: str,
    run_key: str,
    *,
    record_last_response: bool,
) -> None:
    path = agent_state_path(state_root, agent_name)

    def _remove_active_run(data: dict[str, Any]) -> None:
        active_runs = data.get("active_runs", {})
        active_runs.pop(run_key, None)
        data["active_runs"] = active_runs
        if record_last_response:
            data["last_response_at"] = now_iso()

    locked_update_json(path, _remove_active_run)


def _should_poke_agent(
    state_root: Path,
    agent_name: str,
    now: datetime,
    cooldown: int,
    grace: int,
    stale_busy: int,
    *,
    scope_key: str | None = None,
    min_idle: int = 0,
) -> bool:
    state = read_agent_state(state_root, agent_name)

    # Check active runs — agent is busy if any non-stale runs exist
    active_runs: dict[str, Any] = state.get("active_runs", {})
    for _scope, run_info in list(active_runs.items()):
        started = run_info.get("started_at")
        if started is None:
            continue
        if (now - datetime.fromisoformat(started)).total_seconds() < stale_busy:
            return False  # At least one fresh active run → agent is busy

    # Minimum idle time: don't poke unless the agent has been idle long enough.
    if min_idle > 0:
        last_response = state.get("last_response_at")
        if last_response:
            resp_time = datetime.fromisoformat(last_response)
            if (now - resp_time).total_seconds() < min_idle:
                return False

    # Per-scope poke cooldown: each thread has its own cooldown so that
    # poking an agent in one thread does not suppress pokes in other threads.
    poked_scopes: dict[str, str] = state.get("poked_scopes", {})
    if scope_key and scope_key in poked_scopes:
        poked_time = datetime.fromisoformat(poked_scopes[scope_key])
        if (now - poked_time).total_seconds() < cooldown:
            return False
    elif not scope_key:
        # Legacy fallback for callers that don't provide scope_key
        last_poked = state.get("last_poked_at")
        if last_poked:
            poked_time = datetime.fromisoformat(last_poked)
            if (now - poked_time).total_seconds() < cooldown:
                return False

    last_response = state.get("last_response_at")
    if last_response:
        resp_time = datetime.fromisoformat(last_response)
        if (now - resp_time).total_seconds() < grace:
            return False

    return True


def _group_actionable_items_by_agent(
    actionable: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in actionable:
        agent = item.get("assigned_agent")
        if agent:
            grouped.setdefault(agent, []).append(item)
    return grouped


def _format_poke_message(
    agent_name: str,
    agent_items: list[dict[str, Any]],
    all_items: list[dict[str, Any]],
) -> str:
    total = len(all_items)
    done_count = len([i for i in all_items if i["status"] in TERMINAL_STATUSES])
    lines = [f"@{agent_name} workloop resume.\n\nNext actionable items:"]
    for item in agent_items[:5]:
        emoji = PRIORITY_EMOJI.get(item.get("priority", "medium"), "\u26aa")
        lines.append(
            f"- {emoji} `{item['id']}` {item['title']} [{item.get('priority', 'medium')}]"
        )
    if len(agent_items) > 5:
        lines.append(f"- ... and {len(agent_items) - 5} more")
    lines.append(f"\nProgress: {done_count}/{total} complete.")
    lines.append("Use `complete_todo` as you finish items.")
    return "\n".join(lines)


async def _has_pending_schedules(
    ctx: PokeScanContext, room_id: str, thread_id: str | None
) -> bool:
    result = await ctx.query_room_state(room_id, "com.mindroom.scheduled.task")
    if result is None:
        return False
    for _task_id, content in result.items():
        if not isinstance(content, dict):
            continue
        if content.get("status") != "pending":
            continue
        workflow = content.get("workflow") or {}
        if isinstance(workflow, str):
            try:
                workflow = json.loads(workflow)
            except json.JSONDecodeError:
                continue
        if not isinstance(workflow, dict):
            continue
        if workflow.get("thread_id") == thread_id:
            return True
    return False


async def _run_poke_scan(
    ctx: PokeScanContext,
) -> int:
    now = datetime.now(UTC)
    cooldown = int(ctx.settings.get("poke_cooldown_seconds", 300))
    grace = int(ctx.settings.get("recent_response_grace_seconds", 30))
    stale_busy = int(ctx.settings.get("stale_busy_seconds", 600))
    max_pokes = int(ctx.settings.get("max_pokes_per_tick", 3))
    min_idle = int(ctx.settings.get("min_idle_before_poke_seconds", 600))
    pokes_sent = 0
    configured_agents = set((ctx.config.agents or {}).keys())

    threads_dir = ctx.state_root / "threads"
    if not threads_dir.exists():
        return 0

    for thread_todos_path in sorted(threads_dir.glob("*/todos.json")):
        if pokes_sent >= max_pokes:
            break
        try:
            thread_state = read_todos(thread_todos_path)
        except Exception:
            logger.exception("workloop-poke: failed to read %s", thread_todos_path)
            continue
        items = thread_state.get("items", [])
        items_by_id = {item["id"]: item for item in items}
        actionable = [item for item in items if is_actionable(item, items_by_id)]
        if not actionable:
            continue
        grouped = _group_actionable_items_by_agent(actionable)
        for agent_name, agent_items in grouped.items():
            if pokes_sent >= max_pokes:
                break
            if agent_name not in configured_agents:
                continue
            matrix_thread_id = (
                None
                if thread_state.get("thread_id") == "main"
                else thread_state.get("thread_id")
            )
            room_id = thread_state.get("room_id", "")
            if not room_id:
                continue
            if await _has_pending_schedules(ctx, room_id, matrix_thread_id):
                continue
            scope_key = f"{room_id}:{matrix_thread_id or 'main'}"
            if not _should_poke_agent(
                ctx.state_root,
                agent_name,
                now,
                cooldown,
                grace,
                stale_busy,
                scope_key=scope_key,
                min_idle=min_idle,
            ):
                continue
            poke_text = _format_poke_message(agent_name, agent_items, items)
            try:
                await ctx.send_message(
                    room_id,
                    poke_text,
                    thread_id=matrix_thread_id,
                    trigger_dispatch=True,
                )
                poke_agent_scope(ctx.state_root, agent_name, scope_key, now)
                pokes_sent += 1
                logger.info(
                    "workloop-poke: poked %s in room %s thread %s",
                    agent_name,
                    room_id,
                    matrix_thread_id,
                )
            except Exception:
                logger.exception("workloop-poke: failed to poke %s", agent_name)

    return pokes_sent


async def run_poke_scan(ctx: PokeScanContext) -> int:
    """Run a single auto-poke scan."""
    return await _run_poke_scan(ctx)


def _parse_poke_interval_seconds(settings: dict[str, Any], runtime_logger: Any) -> int:
    raw_interval = settings.get("poke_interval_seconds", DEFAULT_POKE_INTERVAL_SECONDS)
    try:
        return int(raw_interval)
    except (TypeError, ValueError):
        runtime_logger.warning(
            "workloop-auto-poke: invalid poke_interval_seconds=%r; using default %s",
            raw_interval,
            DEFAULT_POKE_INTERVAL_SECONDS,
        )
        return DEFAULT_POKE_INTERVAL_SECONDS


async def _auto_poke_loop(runtime: AutoPokeRuntime) -> None:
    runtime.logger.info("workloop-auto-poke: started")
    try:
        while True:
            # Read the interval each cycle so a hot-reloaded setting does not leave a stale sleep cadence behind.
            interval = _parse_poke_interval_seconds(runtime.settings, runtime.logger)
            try:
                await asyncio.sleep(interval)
                pokes = await _run_poke_scan(runtime)
                runtime.logger.info(
                    "workloop-auto-poke: scan complete, %d poke(s) sent", pokes
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                runtime.logger.exception("workloop-auto-poke: scan failed; continuing")
    except asyncio.CancelledError:
        runtime.logger.info("workloop-auto-poke: stopped")
        raise


def _build_auto_poke_runtime(ctx: AgentLifecycleContext) -> AutoPokeRuntime:
    return AutoPokeRuntime(
        settings=ctx.settings,
        config=ctx.config,
        state_root=ctx.state_root,
        logger=ctx.logger,
        _message_sender=ctx.message_sender,
        _room_state_querier=ctx.room_state_querier,
    )


@hook(
    event="agent:started",
    name="workloop-auto-poke-start",
    agents=(ROUTER_AGENT_NAME,),
    priority=100,
    timeout_ms=5000,
)
async def start_auto_poke_loop(ctx: AgentLifecycleContext) -> None:
    """Start the background auto-poke loop once per router lifecycle."""
    global _AUTO_POKE_TASK

    if ctx.entity_name != ROUTER_AGENT_NAME:
        return
    if _AUTO_POKE_TASK is not None and not _AUTO_POKE_TASK.done():
        return

    runtime = _build_auto_poke_runtime(ctx)
    _AUTO_POKE_TASK = asyncio.create_task(
        _auto_poke_loop(runtime), name=f"{_PLUGIN_NAME}-auto-poke"
    )


@hook(
    event="agent:stopped",
    name="workloop-auto-poke-stop",
    agents=(ROUTER_AGENT_NAME,),
    priority=100,
    timeout_ms=5000,
)
async def stop_auto_poke_loop(ctx: AgentLifecycleContext) -> None:
    """Cancel the background auto-poke loop when the router stops."""
    global _AUTO_POKE_TASK

    if ctx.entity_name != ROUTER_AGENT_NAME:
        return
    task = _AUTO_POKE_TASK
    if task is None:
        return

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    finally:
        if _AUTO_POKE_TASK is task:
            _AUTO_POKE_TASK = None


@hook(
    event="message:enrich",
    name="workloop-context",
    priority=50,
)
async def inject_todos(ctx: MessageEnrichContext) -> list[EnrichmentItem]:
    """Inject thread work plan and mark target agent busy."""
    agent_name = ctx.target_entity_name
    room_id = ctx.envelope.room_id
    thread_id = response_scope_thread_id(ctx.envelope)

    # Mark agent as busy for this scope
    run_key = f"{room_id}:{thread_id}"
    try:

        def _add_active_run(data: dict[str, Any]) -> None:
            data.setdefault("active_runs", {})[run_key] = {"started_at": now_iso()}

        path = agent_state_path(ctx.state_root, agent_name)
        locked_update_json(path, _add_active_run)
    except Exception:
        logger.exception(
            "workloop-context: failed to update agent state for %s", agent_name
        )

    # Load thread plan
    path = todos_path(ctx.state_root, room_id, thread_id)
    try:
        state = read_todos(path)
    except Exception:
        logger.exception("workloop-context: failed to load todos")
        return []

    items = state.get("items", [])
    if not items:
        return []

    max_items = int(ctx.settings.get("max_items_in_enrichment", 10))
    items_by_id = {item["id"]: item for item in items}
    actionable = [i for i in items if is_actionable(i, items_by_id)]
    blocked = [i for i in items if i["status"] == "open" and is_blocked(i, items_by_id)]
    done = [i for i in items if i["status"] in TERMINAL_STATUSES]

    actionable.sort(key=lambda i: PRIORITY_ORDER.get(i.get("priority", "medium"), 9))

    total = len(items)
    done_count = len(done)
    lines = [f"Thread work plan: {done_count}/{total} complete."]

    if actionable:
        lines.append("\nActionable:")
        for i in actionable[:max_items]:
            emoji = PRIORITY_EMOJI.get(i.get("priority", "medium"), "\u26aa")
            lines.append(
                f"- {emoji} `{i['id']}` {i['title']} [{i.get('priority', 'medium')}]"
            )
        if len(actionable) > max_items:
            lines.append(f"... and {len(actionable) - max_items} more")

    if blocked:
        lines.append("\nBlocked:")
        for i in blocked[:max_items]:
            waiting = [
                d
                for d in i.get("depends_on", [])
                if items_by_id.get(d, {}).get("status") not in TERMINAL_STATUSES
            ]
            waiting_str = ", ".join(f"`{d}`" for d in waiting)
            lines.append(
                f"- `{i['id']}` {i['title']} [{i.get('priority', 'medium')}] waiting on {waiting_str}"
            )
        if len(blocked) > max_items:
            lines.append(f"... and {len(blocked) - max_items} more")

    if done:
        lines.append(f"\nDone: {done_count} item(s)")
        for i in done[:3]:
            lines.append(f"- `{i['id']}` {i['title']}")
        if len(done) > 3:
            lines.append(f"... and {len(done) - 3} more")

    lines.append("\nUse `complete_todo(todo_id)` when you finish an item.")

    return [
        EnrichmentItem(key="workloop", text="\n".join(lines), cache_policy="volatile")
    ]


@hook(
    event="message:after_response",
    name="workloop-track-idle",
    priority=100,
    timeout_ms=3000,
)
async def track_idle(ctx: AfterResponseContext) -> None:
    """Remove the active run for this scope and record last response time."""
    agent_name = ctx.result.envelope.agent_name
    room_id = ctx.result.envelope.room_id
    thread_id = response_scope_thread_id(ctx.result.envelope)
    run_key = f"{room_id}:{thread_id}"
    try:
        _clear_active_run(
            ctx.state_root,
            agent_name,
            run_key,
            record_last_response=True,
        )
    except Exception:
        logger.exception(
            "workloop-track-idle: failed to update agent state for %s", agent_name
        )


@hook(
    event="message:cancelled",
    name="workloop-track-cancelled",
    priority=100,
    timeout_ms=3000,
)
async def track_cancelled(ctx: CancelledResponseContext) -> None:
    """Remove the active run for this scope without recording a response timestamp."""
    agent_name = ctx.info.envelope.agent_name
    room_id = ctx.info.envelope.room_id
    thread_id = response_scope_thread_id(ctx.info.envelope)
    run_key = f"{room_id}:{thread_id}"
    try:
        _clear_active_run(
            ctx.state_root,
            agent_name,
            run_key,
            record_last_response=False,
        )
    except Exception:
        logger.exception(
            "workloop-track-cancelled: failed to update agent state for %s",
            agent_name,
        )
