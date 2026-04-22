"""Auto-poke and agent activity hooks."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mindroom.constants import STREAM_STATUS_KEY, STREAM_STATUS_STREAMING
from mindroom.hooks import (
    AfterResponseContext,
    AgentLifecycleContext,
    EnrichmentItem,
    MessageEnrichContext,
    hook,
)
from mindroom.matrix.cache import get_latest_agent_message_snapshot
from mindroom.matrix.identity import MatrixID

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
from .runtime import (
    AutoPokeRuntime,
    DEFAULT_POKE_INTERVAL_SECONDS,
    PRIORITY_EMOJI,
    PRIORITY_ORDER,
    PokeScanContext,
    TERMINAL_STATUSES,
    ThreadMessageSnapshot,
    logger,
)

STREAM_STATUS_SANITY_TIMEOUT_SECONDS = 1800


async def _read_latest_thread_message(
    ctx: PokeScanContext,
    room_id: str,
    thread_id: str | None,
    sender: str,
) -> ThreadMessageSnapshot | None:
    reader = getattr(ctx, "read_latest_thread_message", None)
    if callable(reader):
        return await reader(room_id, thread_id, sender)
    cache_config = getattr(ctx.config, "cache", None)
    if cache_config is not None and hasattr(cache_config, "resolve_db_path"):
        db_path = cache_config.resolve_db_path(ctx.runtime_paths)
    else:
        db_path = ctx.runtime_paths.storage_root / "event_cache.db"
    snapshot = await asyncio.to_thread(
        get_latest_agent_message_snapshot,
        db_path=db_path,
        room_id=room_id,
        thread_id=thread_id,
        sender=sender,
        runtime_started_at=getattr(ctx, "runtime_started_at", None),
    )
    if snapshot is None:
        return None
    return ThreadMessageSnapshot(
        content=snapshot.content,
        origin_server_ts=datetime.fromtimestamp(
            snapshot.origin_server_ts / 1000,
            tz=UTC,
        ),
    )


def _agent_matrix_user_id(ctx: PokeScanContext, agent_name: str) -> str:
    domain = ctx.config.get_domain(ctx.runtime_paths)
    return MatrixID.from_agent(agent_name, domain, ctx.runtime_paths).full_id


def _record_last_response(state_root: Path, agent_name: str) -> None:
    def _mutate(data: dict[str, Any]) -> None:
        if not data:
            data["agent_name"] = agent_name
        if "is_busy" in data:
            data.pop("is_busy", None)
            data.pop("last_message_at", None)
            data.pop("current_room_id", None)
            data.pop("current_thread_id", None)
        data.setdefault("poked_scopes", {})
        data.setdefault("poked_scope_messages", {})
        data["last_response_at"] = now_iso()

    locked_update_json(agent_state_path(state_root, agent_name), _mutate)


async def _should_poke_agent(
    ctx: PokeScanContext,
    agent_name: str,
    room_id: str,
    thread_id: str | None,
    now: datetime,
    cooldown: int,
    grace: int,
    *,
    scope_key: str | None = None,
    min_idle: int = 0,
) -> bool:
    try:
        last_message = await _read_latest_thread_message(
            ctx,
            room_id,
            thread_id,
            _agent_matrix_user_id(ctx, agent_name),
        )
    except Exception:
        logger.warning(
            "workloop-poke: latest message snapshot read failed for %s in room %s thread %s",
            agent_name,
            room_id,
            thread_id,
            exc_info=True,
        )
        return False
    if last_message is not None:
        stream_status = last_message.content.get(STREAM_STATUS_KEY)
        age_seconds = (now - last_message.origin_server_ts).total_seconds()
        if (
            stream_status == STREAM_STATUS_STREAMING
            and age_seconds < STREAM_STATUS_SANITY_TIMEOUT_SECONDS
        ):
            return False

    state = read_agent_state(ctx.state_root, agent_name)

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


def _pending_schedule_thread_ids(
    result: dict[str, Any] | None,
) -> set[str | None]:
    pending_thread_ids: set[str | None] = set()
    if result is None:
        return pending_thread_ids
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
        pending_thread_ids.add(workflow.get("thread_id"))
    return pending_thread_ids


async def _has_pending_schedules(
    ctx: PokeScanContext, room_id: str, thread_id: str | None
) -> bool:
    result = await ctx.query_room_state(room_id, "com.mindroom.scheduled.task")
    return thread_id in _pending_schedule_thread_ids(result)


async def _run_poke_scan(
    ctx: PokeScanContext,
) -> int:
    now = datetime.now(UTC)
    cooldown = int(ctx.settings.get("poke_cooldown_seconds", 300))
    grace = int(ctx.settings.get("recent_response_grace_seconds", 30))
    max_pokes = int(ctx.settings.get("max_pokes_per_tick", 3))
    min_idle = int(ctx.settings.get("min_idle_before_poke_seconds", 600))
    pokes_sent = 0
    configured_agents = set((ctx.config.agents or {}).keys())
    pending_schedule_threads_by_room: dict[str, set[str | None]] = {}

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
            pending_thread_ids = pending_schedule_threads_by_room.get(room_id)
            if pending_thread_ids is None:
                result = await ctx.query_room_state(
                    room_id, "com.mindroom.scheduled.task"
                )
                pending_thread_ids = _pending_schedule_thread_ids(result)
                pending_schedule_threads_by_room[room_id] = pending_thread_ids
            if matrix_thread_id in pending_thread_ids:
                continue
            scope_key = f"{room_id}:{matrix_thread_id or 'main'}"
            if not await _should_poke_agent(
                ctx,
                agent_name,
                room_id,
                matrix_thread_id,
                now,
                cooldown,
                grace,
                scope_key=scope_key,
                min_idle=min_idle,
            ):
                continue
            poke_text = _format_poke_message(agent_name, agent_items, items)
            agent_state = read_agent_state(ctx.state_root, agent_name)
            last_poke_text = agent_state.get("poked_scope_messages", {}).get(scope_key)
            if last_poke_text == poke_text:
                logger.debug(
                    "workloop-poke: skipping duplicate poke for %s in room %s thread %s",
                    agent_name,
                    room_id,
                    matrix_thread_id,
                )
                continue
            try:
                await ctx.send_message(
                    room_id,
                    poke_text,
                    thread_id=matrix_thread_id,
                    trigger_dispatch=True,
                )
                poke_agent_scope(
                    ctx.state_root, agent_name, scope_key, now, message_text=poke_text
                )
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


def _build_auto_poke_runtime(ctx: AgentLifecycleContext) -> AutoPokeRuntime:
    return AutoPokeRuntime(
        settings=ctx.settings,
        config=ctx.config,
        state_root=ctx.state_root,
        runtime_paths=ctx.runtime_paths,
        runtime_started_at=getattr(ctx, "runtime_started_at", None),
        logger=ctx.logger,
        _message_sender=ctx.message_sender,
        _room_state_querier=ctx.room_state_querier,
    )


@hook(
    event="message:enrich",
    name="workloop-context",
    priority=50,
)
async def inject_todos(ctx: MessageEnrichContext) -> list[EnrichmentItem]:
    """Inject the current thread work plan into agent context."""
    room_id = ctx.envelope.room_id
    thread_id = response_scope_thread_id(ctx.envelope)

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
    """Record the agent's most recent response time for idle gating."""
    agent_name = ctx.result.envelope.agent_name
    try:
        _record_last_response(ctx.state_root, agent_name)
    except Exception:
        logger.exception(
            "workloop-track-idle: failed to update agent state for %s", agent_name
        )
