"""Hook handlers for the MindRoom workloop plugin.

This file is self-contained — all models, JSON helpers, lock helpers, formatting,
and hook logic are in one module to avoid relative-import issues with MindRoom's
plugin loader (which uses ``spec_from_file_location``).

Provides:
- ``workloop-auto-poke-start`` (agent:started): start the router-owned auto-poke loop
- ``workloop-auto-poke-stop`` (agent:stopped): stop the router-owned auto-poke loop
- ``workloop-command`` (message:received): ``!todo`` and ``!workloop-tick`` commands
- ``workloop-context`` (message:enrich): inject thread work plan + mark agent busy
- ``workloop-track-idle`` (message:after_response): mark agent idle
- ``workloop-poke`` (schedule:fired): suppress deprecated scheduled ``!workloop-tick``
- ``workloop-react`` (reaction:received): complete/cancel via reactions
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.hooks import (
    AfterResponseContext,
    AgentLifecycleContext,
    EnrichmentItem,
    HookMessageSender,
    MessageEnrichContext,
    MessageReceivedContext,
    ReactionReceivedContext,
    hook,
)

logger = logging.getLogger(__name__)

_PLUGIN_NAME = "workloop"
_AUTO_POKE_HOOK_SOURCE = f"{_PLUGIN_NAME}:auto_poke"

# ══════════════════════════════════════════════════════════════════════
# Constants
# ══════════════════════════════════════════════════════════════════════

VALID_PRIORITIES = {"low", "medium", "high", "critical"}
TERMINAL_STATUSES = {"done", "cancelled"}
PRIORITY_EMOJI: dict[str, str] = {
    "critical": "\U0001f534",
    "high": "\U0001f7e0",
    "medium": "\U0001f7e1",
    "low": "\U0001f7e2",
}
PRIORITY_ORDER: dict[str, int] = {"critical": 0, "high": 1, "medium": 2, "low": 3}
DEFAULT_POKE_INTERVAL_SECONDS = 120


class PokeScanContext(Protocol):
    settings: dict[str, Any]
    config: Any
    state_root: Path

    async def send_message(
        self,
        room_id: str,
        text: str,
        *,
        thread_id: str | None = None,
        extra_content: dict[str, Any] | None = None,
    ) -> str | None: ...


@dataclass(slots=True)
class AutoPokeRuntime:
    settings: dict[str, Any]
    config: Any
    state_root: Path
    logger: Any
    _message_sender: HookMessageSender | None

    async def send_message(
        self,
        room_id: str,
        text: str,
        *,
        thread_id: str | None = None,
        extra_content: dict[str, Any] | None = None,
    ) -> str | None:
        if self._message_sender is None:
            self.logger.warning("workloop-auto-poke: send_message called but no sender registered")
            return None
        return await self._message_sender(room_id, text, thread_id, _AUTO_POKE_HOOK_SOURCE, extra_content)


_AUTO_POKE_TASK: asyncio.Task[None] | None = None

# ══════════════════════════════════════════════════════════════════════
# Helpers: thread key, sanitization, timestamps
# ══════════════════════════════════════════════════════════════════════


def _sanitize(value: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^A-Za-z0-9]", "_", value)).strip("_")


def _thread_key(room_id: str, thread_id: str | None) -> str:
    resolved = thread_id or "main"
    return f"{_sanitize(room_id)}_{_sanitize(resolved)}"


def _resolve_scope(envelope: Any) -> tuple[str, str | None, str | None]:
    """Return (room_id, storage_thread_id, reply_thread_id).

    storage_thread_id: None for room-level → becomes "main" in _thread_key.
    reply_thread_id: resolved_thread_id for sending responses in the right thread.

    ``thread_id`` on the envelope is None for room-level messages.
    ``resolved_thread_id`` is always set (may equal the message's own event ID for
    room-level messages), so it is the correct value for replying in-thread.
    """
    room_id = envelope.room_id
    storage_tid = envelope.thread_id  # None for room-level, set for threads
    reply_tid = envelope.resolved_thread_id if envelope.thread_id else None
    return room_id, storage_tid, reply_tid


def _response_scope_thread_id(envelope: Any) -> str:
    """Return the actual response-scope thread key for agent-generated work state."""
    return envelope.resolved_thread_id or envelope.thread_id or "main"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _short_id(existing_ids: set[str]) -> str:
    while True:
        candidate = uuid.uuid4().hex[:8]
        if candidate not in existing_ids:
            return candidate


# ══════════════════════════════════════════════════════════════════════
# Helpers: JSON read/write with fcntl locks
# ══════════════════════════════════════════════════════════════════════

_T = Any  # generic return from mutate callback


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _locked_update_json(path: Path, mutate: Any) -> Any:
    """Read-modify-write JSON under an exclusive fcntl lock."""
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            result = mutate(data)
            path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            return result
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


# ══════════════════════════════════════════════════════════════════════
# Helpers: thread-state accessors
# ══════════════════════════════════════════════════════════════════════


def _todos_path(state_root: Path, room_id: str, thread_id: str | None) -> Path:
    key = _thread_key(room_id, thread_id)
    return state_root / "threads" / key / "todos.json"


def _read_todos(path: Path) -> dict[str, Any]:
    data = _read_json(path)
    if not data:
        return {"room_id": "", "thread_id": "main", "created_at": _now_iso(), "updated_at": _now_iso(), "items": []}
    return data


def _ensure_thread_state(data: dict[str, Any], room_id: str, thread_id: str | None) -> None:
    """Ensure data dict has the required top-level fields."""
    resolved = thread_id or "main"
    if "items" not in data:
        data["room_id"] = room_id
        data["thread_id"] = resolved
        data["created_at"] = _now_iso()
        data["updated_at"] = _now_iso()
        data["items"] = []


# ══════════════════════════════════════════════════════════════════════
# Helpers: dependency resolution
# ══════════════════════════════════════════════════════════════════════


def is_blocked(item: dict[str, Any], items_by_id: dict[str, dict[str, Any]]) -> bool:
    for dep_id in item.get("depends_on", []):
        dep = items_by_id.get(dep_id)
        if dep is None:
            continue
        if dep["status"] not in TERMINAL_STATUSES:
            return True
    return False


def is_actionable(item: dict[str, Any], items_by_id: dict[str, dict[str, Any]]) -> bool:
    return item["status"] == "open" and not is_blocked(item, items_by_id)


def _would_create_cycle(items_by_id: dict[str, dict[str, Any]], item_id: str, new_dep_id: str) -> bool:
    stack = [new_dep_id]
    seen: set[str] = set()
    while stack:
        current = stack.pop()
        if current == item_id:
            return True
        if current in seen:
            continue
        seen.add(current)
        current_item = items_by_id.get(current)
        if current_item is not None:
            stack.extend(current_item.get("depends_on", []))
    return False


def _newly_unblocked(items: list[dict[str, Any]], changed_id: str) -> list[dict[str, Any]]:
    items_by_id = {item["id"]: item for item in items}
    unblocked: list[dict[str, Any]] = []
    for item in items:
        if item["status"] != "open":
            continue
        if changed_id not in item.get("depends_on", []):
            continue
        if is_actionable(item, items_by_id):
            unblocked.append(item)
    return unblocked


# ══════════════════════════════════════════════════════════════════════
# Helpers: agent state
# ══════════════════════════════════════════════════════════════════════


def _agent_state_path(state_root: Path, agent_name: str) -> Path:
    return state_root / "agents" / f"{agent_name}.json"


def _read_agent_state(state_root: Path, agent_name: str) -> dict[str, Any]:
    path = _agent_state_path(state_root, agent_name)
    data = _read_json(path)
    if not data:
        return {
            "agent_name": agent_name,
            "active_runs": {},
            "last_response_at": None,
            "last_poked_at": None,
        }
    # Migrate legacy format
    if "is_busy" in data:
        data.pop("is_busy", None)
        data.pop("last_message_at", None)
        data.pop("current_room_id", None)
        data.pop("current_thread_id", None)
        data.setdefault("active_runs", {})
    return data


def _update_agent_state(state_root: Path, agent_name: str, updates: dict[str, Any]) -> None:
    path = _agent_state_path(state_root, agent_name)

    def mutate(data: dict[str, Any]) -> None:
        if not data:
            data["agent_name"] = agent_name
            data["active_runs"] = {}
            data["last_response_at"] = None
            data["last_poked_at"] = None
        # Migrate legacy format
        if "is_busy" in data:
            data.pop("is_busy", None)
            data.pop("last_message_at", None)
            data.pop("current_room_id", None)
            data.pop("current_thread_id", None)
            data.setdefault("active_runs", {})
        data.update(updates)

    _locked_update_json(path, mutate)


# ══════════════════════════════════════════════════════════════════════
# Helpers: formatting
# ══════════════════════════════════════════════════════════════════════


def _format_item_line(item: dict[str, Any], *, show_status: bool = False) -> str:
    emoji = PRIORITY_EMOJI.get(item.get("priority", "medium"), "\u26aa")
    status_mark = ""
    if show_status:
        if item["status"] == "done":
            status_mark = "\u2705 "
        elif item["status"] == "cancelled":
            status_mark = "\u274c "
    assigned = f" @{item['assigned_agent']}" if item.get("assigned_agent") else ""
    return f"  {emoji} `{item['id']}` {status_mark}{item['title']} [{item.get('priority', 'medium')}]{assigned}"


def _format_list(items: list[dict[str, Any]], *, show_all: bool = False) -> str:
    if not items:
        return "\u2728 No items in this thread's work plan."
    items_by_id = {item["id"]: item for item in items}
    actionable = [i for i in items if is_actionable(i, items_by_id)]
    blocked = [i for i in items if i["status"] == "open" and is_blocked(i, items_by_id)]
    done = [i for i in items if i["status"] in TERMINAL_STATUSES]

    actionable.sort(key=lambda i: PRIORITY_ORDER.get(i.get("priority", "medium"), 9))

    total = len(items)
    done_count = len(done)
    lines = [f"\U0001f4cb **Work plan: {done_count}/{total} complete.**"]

    if actionable:
        lines.append("\n**Actionable:**")
        for i in actionable:
            lines.append(_format_item_line(i))

    if blocked:
        lines.append("\n**Blocked:**")
        for i in blocked:
            waiting = [
                d for d in i.get("depends_on", []) if items_by_id.get(d, {}).get("status") not in TERMINAL_STATUSES
            ]
            waiting_str = ", ".join(f"`{d}`" for d in waiting)
            lines.append(f"{_format_item_line(i)} waiting on {waiting_str}")

    if show_all and done:
        lines.append("\n**Done/Cancelled:**")
        for i in done:
            lines.append(_format_item_line(i, show_status=True))

    return "\n".join(lines)


def _format_plan(items: list[dict[str, Any]]) -> str:
    """Full dependency-aware work plan view."""
    if not items:
        return "\u2728 No items in this thread's work plan."
    items_by_id = {item["id"]: item for item in items}
    total = len(items)
    done_count = len([i for i in items if i["status"] in TERMINAL_STATUSES])
    lines = [f"\U0001f4cb **Thread work plan: {done_count}/{total} complete.**\n"]

    for item in items:
        emoji = PRIORITY_EMOJI.get(item.get("priority", "medium"), "\u26aa")
        status = item["status"]
        if status == "done":
            mark = "\u2705"
        elif status == "cancelled":
            mark = "\u274c"
        elif is_blocked(item, items_by_id):
            mark = "\u23f3"
        else:
            mark = "\u25cb"

        assigned = f" (@{item['assigned_agent']})" if item.get("assigned_agent") else ""
        dep_str = ""
        deps = item.get("depends_on", [])
        if deps:
            dep_str = " \u2190 " + ", ".join(f"`{d}`" for d in deps)
        lines.append(
            f"{mark} {emoji} `{item['id']}` {item['title']} [{item.get('priority', 'medium')}]{assigned}{dep_str}"
        )

    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════
# Helpers: poke logic
# ══════════════════════════════════════════════════════════════════════


def _should_poke_agent(
    state_root: Path,
    agent_name: str,
    now: datetime,
    cooldown: int,
    grace: int,
    stale_busy: int,
) -> bool:
    state = _read_agent_state(state_root, agent_name)

    # Check active runs — agent is busy if any non-stale runs exist
    active_runs: dict[str, Any] = state.get("active_runs", {})
    for _scope, run_info in list(active_runs.items()):
        started = run_info.get("started_at")
        if started is None:
            continue
        if (now - datetime.fromisoformat(started)).total_seconds() < stale_busy:
            return False  # At least one fresh active run → agent is busy

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
        lines.append(f"- {emoji} `{item['id']}` {item['title']} [{item.get('priority', 'medium')}]")
    if len(agent_items) > 5:
        lines.append(f"- ... and {len(agent_items) - 5} more")
    lines.append(f"\nProgress: {done_count}/{total} complete.")
    lines.append("Use `complete_todo` as you finish items.")
    return "\n".join(lines)


async def _run_poke_scan(
    ctx: PokeScanContext,
) -> int:
    now = datetime.now(UTC)
    cooldown = int(ctx.settings.get("poke_cooldown_seconds", 300))
    grace = int(ctx.settings.get("recent_response_grace_seconds", 30))
    stale_busy = int(ctx.settings.get("stale_busy_seconds", 600))
    max_pokes = int(ctx.settings.get("max_pokes_per_tick", 3))
    pokes_sent = 0
    configured_agents = set((ctx.config.agents or {}).keys())

    threads_dir = ctx.state_root / "threads"
    if not threads_dir.exists():
        return 0

    for todos_path in sorted(threads_dir.glob("*/todos.json")):
        if pokes_sent >= max_pokes:
            break
        try:
            thread_state = _read_todos(todos_path)
        except Exception:
            logger.exception("workloop-poke: failed to read %s", todos_path)
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
            if not _should_poke_agent(ctx.state_root, agent_name, now, cooldown, grace, stale_busy):
                continue
            poke_text = _format_poke_message(agent_name, agent_items, items)
            matrix_thread_id = None if thread_state.get("thread_id") == "main" else thread_state.get("thread_id")
            room_id = thread_state.get("room_id", "")
            if not room_id:
                continue
            try:
                await ctx.send_message(room_id, poke_text, thread_id=matrix_thread_id)
                _update_agent_state(ctx.state_root, agent_name, {"last_poked_at": now.isoformat()})
                pokes_sent += 1
                logger.info("workloop-poke: poked %s in room %s thread %s", agent_name, room_id, matrix_thread_id)
            except Exception:
                logger.exception("workloop-poke: failed to poke %s", agent_name)

    return pokes_sent


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
                runtime.logger.info("workloop-auto-poke: scan complete, %d poke(s) sent", pokes)
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
    )


# ══════════════════════════════════════════════════════════════════════
# Hook 1: agent:started — start auto-poke loop (router only)
# ══════════════════════════════════════════════════════════════════════


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
    _AUTO_POKE_TASK = asyncio.create_task(_auto_poke_loop(runtime), name=f"{_PLUGIN_NAME}-auto-poke")


# ══════════════════════════════════════════════════════════════════════
# Hook 2: agent:stopped — stop auto-poke loop (router only)
# ══════════════════════════════════════════════════════════════════════


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


# ══════════════════════════════════════════════════════════════════════
# Hook 3: !todo command handler (message:received, router only)
# ══════════════════════════════════════════════════════════════════════


@hook(
    event="message:received",
    name="workloop-command",
    agents=(ROUTER_AGENT_NAME,),
    priority=100,
    timeout_ms=15000,
)
async def workloop_command(ctx: MessageReceivedContext) -> None:
    """Handle !todo and !workloop-tick commands."""
    body = ctx.envelope.body.strip()

    if not body.startswith("!todo"):
        return

    room_id, thread_id, reply_tid = _resolve_scope(ctx.envelope)
    parts = body[5:].strip()

    try:
        # !todo / !todo help
        if not parts or parts == "help":
            help_text = (
                "\U0001f4dd **Workloop Todo Commands**\n"
                "`!todo add <title>` \u2014 Create a new todo\n"
                "`!todo add [high] <title>` \u2014 Create with priority\n"
                "`!todo list` \u2014 List actionable & blocked items\n"
                "`!todo all` \u2014 List all items including done\n"
                "`!todo done <id>` \u2014 Complete a todo\n"
                "`!todo cancel <id>` \u2014 Cancel a todo\n"
                "`!todo rm <id>` \u2014 Delete permanently\n"
                "`!todo dep <id> <depends-on-id>` \u2014 Add dependency\n"
                "`!todo assign <id> <agent>` \u2014 Assign to agent\n"
                "`!todo plan` \u2014 Full dependency-aware plan view\n\n"
                "React \u2705 to complete, \u274c to cancel.\n"
                "Items are scoped to the current thread."
            )
            await ctx.send_message(room_id, help_text, thread_id=reply_tid)
            ctx.suppress = True
            return

        path = _todos_path(ctx.state_root, room_id, thread_id)

        # !todo list
        if parts == "list":
            state = _read_todos(path)
            await ctx.send_message(room_id, _format_list(state["items"]), thread_id=reply_tid)
            ctx.suppress = True
            return

        # !todo all
        if parts == "all":
            state = _read_todos(path)
            await ctx.send_message(room_id, _format_list(state["items"], show_all=True), thread_id=reply_tid)
            ctx.suppress = True
            return

        # !todo plan
        if parts == "plan":
            state = _read_todos(path)
            await ctx.send_message(room_id, _format_plan(state["items"]), thread_id=reply_tid)
            ctx.suppress = True
            return

        # !todo done <id>
        if parts.startswith("done "):
            todo_id = parts[5:].strip()

            def mark_done(data: dict[str, Any]) -> str:
                _ensure_thread_state(data, room_id, thread_id)
                for item in data["items"]:
                    if item["id"] == todo_id:
                        if item["status"] in TERMINAL_STATUSES:
                            return f"Item `{todo_id}` is already {item['status']}."
                        item["status"] = "done"
                        item["completed_at"] = _now_iso()
                        item["updated_at"] = _now_iso()
                        data["updated_at"] = _now_iso()
                        unblocked = _newly_unblocked(data["items"], todo_id)
                        msg = f"\u2705 Completed: **{item['title']}** (`{todo_id}`)"
                        if unblocked:
                            names = ", ".join(f"`{u['id']}` {u['title']}" for u in unblocked)
                            msg += f"\n\u2197\ufe0f Now unblocked: {names}"
                        return msg
                return f"\u274c Todo `{todo_id}` not found."

            result = _locked_update_json(path, mark_done)
            await ctx.send_message(room_id, result, thread_id=reply_tid)
            ctx.suppress = True
            return

        # !todo cancel <id>
        if parts.startswith("cancel "):
            todo_id = parts[7:].strip()

            def mark_cancel(data: dict[str, Any]) -> str:
                _ensure_thread_state(data, room_id, thread_id)
                for item in data["items"]:
                    if item["id"] == todo_id:
                        if item["status"] in TERMINAL_STATUSES:
                            return f"Item `{todo_id}` is already {item['status']}."
                        item["status"] = "cancelled"
                        item["updated_at"] = _now_iso()
                        data["updated_at"] = _now_iso()
                        unblocked = _newly_unblocked(data["items"], todo_id)
                        msg = f"\u274c Cancelled: **{item['title']}** (`{todo_id}`)"
                        if unblocked:
                            names = ", ".join(f"`{u['id']}` {u['title']}" for u in unblocked)
                            msg += f"\n\u2197\ufe0f Now unblocked: {names}"
                        return msg
                return f"\u274c Todo `{todo_id}` not found."

            result = _locked_update_json(path, mark_cancel)
            await ctx.send_message(room_id, result, thread_id=reply_tid)
            ctx.suppress = True
            return

        # !todo rm <id>
        if parts.startswith("rm "):
            todo_id = parts[3:].strip()

            def remove(data: dict[str, Any]) -> str:
                _ensure_thread_state(data, room_id, thread_id)
                original_len = len(data["items"])
                data["items"] = [i for i in data["items"] if i["id"] != todo_id]
                if len(data["items"]) == original_len:
                    return f"\u274c Todo `{todo_id}` not found."
                for item in data["items"]:
                    if todo_id in item.get("depends_on", []):
                        item["depends_on"].remove(todo_id)
                data["updated_at"] = _now_iso()
                return f"\U0001f5d1\ufe0f Deleted todo `{todo_id}`."

            result = _locked_update_json(path, remove)
            await ctx.send_message(room_id, result, thread_id=reply_tid)
            ctx.suppress = True
            return

        # !todo dep <id> <depends-on-id>
        if parts.startswith("dep "):
            dep_parts = parts[4:].strip().split()
            if len(dep_parts) != 2:
                await ctx.send_message(room_id, "\u274c Usage: `!todo dep <id> <depends-on-id>`", thread_id=reply_tid)
                ctx.suppress = True
                return
            item_id, dep_id = dep_parts

            def add_dep(data: dict[str, Any]) -> str:
                _ensure_thread_state(data, room_id, thread_id)
                items_by_id = {i["id"]: i for i in data["items"]}
                if item_id not in items_by_id:
                    return f"\u274c Todo `{item_id}` not found."
                if dep_id not in items_by_id:
                    return f"\u274c Todo `{dep_id}` not found."
                if item_id == dep_id:
                    return "\u274c Cannot depend on itself."
                if dep_id in items_by_id[item_id].get("depends_on", []):
                    return f"Item `{item_id}` already depends on `{dep_id}`."
                if _would_create_cycle(items_by_id, item_id, dep_id):
                    return "\u274c Adding this dependency would create a cycle."
                items_by_id[item_id].setdefault("depends_on", []).append(dep_id)
                items_by_id[item_id]["updated_at"] = _now_iso()
                data["updated_at"] = _now_iso()
                return f"\U0001f517 `{item_id}` now depends on `{dep_id}`."

            result = _locked_update_json(path, add_dep)
            await ctx.send_message(room_id, result, thread_id=reply_tid)
            ctx.suppress = True
            return

        # !todo assign <id> <agent_name>
        if parts.startswith("assign "):
            assign_parts = parts[7:].strip().split()
            if len(assign_parts) != 2:
                await ctx.send_message(room_id, "\u274c Usage: `!todo assign <id> <agent>`", thread_id=reply_tid)
                ctx.suppress = True
                return
            item_id, agent_name = assign_parts

            agent_configs = ctx.config.agents or {}
            if agent_name not in agent_configs:
                available = ", ".join(sorted(agent_configs.keys())) or "none"
                await ctx.send_message(
                    room_id,
                    f"\u274c Unknown agent `{agent_name}`. Available: {available}",
                    thread_id=reply_tid,
                )
                ctx.suppress = True
                return

            def do_assign(data: dict[str, Any]) -> str:
                _ensure_thread_state(data, room_id, thread_id)
                for item in data["items"]:
                    if item["id"] == item_id:
                        item["assigned_agent"] = agent_name
                        item["updated_at"] = _now_iso()
                        data["updated_at"] = _now_iso()
                        return f"\U0001f464 `{item_id}` assigned to **{agent_name}**."
                return f"\u274c Todo `{item_id}` not found."

            result = _locked_update_json(path, do_assign)
            await ctx.send_message(room_id, result, thread_id=reply_tid)
            ctx.suppress = True
            return

        # !todo add [priority] <title>
        if not parts.startswith("add "):
            # Reject unrecognized subcommands
            first_word = parts.split()[0] if parts.split() else ""
            await ctx.send_message(
                room_id,
                f"\u274c Unknown subcommand `{first_word}`. Use `!todo help` for usage.",
                thread_id=reply_tid,
            )
            ctx.suppress = True
            return

        title = parts[4:].strip()

        if not title:
            await ctx.send_message(room_id, "\u274c Provide a title: `!todo add <title>`", thread_id=reply_tid)
            ctx.suppress = True
            return

        priority = "medium"
        for p in VALID_PRIORITIES:
            prefix = f"[{p}] "
            if title.lower().startswith(prefix):
                priority = p
                title = title[len(prefix) :]
                break

        def create_item(data: dict[str, Any]) -> dict[str, Any]:
            _ensure_thread_state(data, room_id, thread_id)
            existing_ids = {i["id"] for i in data["items"]}
            new_id = _short_id(existing_ids)
            now = _now_iso()
            item = {
                "id": new_id,
                "title": title,
                "status": "open",
                "priority": priority,
                "depends_on": [],
                "assigned_agent": None,
                "event_id": None,
                "created_at": now,
                "updated_at": now,
                "completed_at": None,
            }
            data["items"].append(item)
            data["updated_at"] = now
            return item

        new_item = _locked_update_json(path, create_item)
        emoji = PRIORITY_EMOJI.get(priority, "")
        msg = (
            f"\U0001f4dd Added: **{title}** (`{new_item['id']}`)\n"
            f"Priority: {emoji} {priority}\n"
            f"React \u2705 to complete | \u274c to cancel"
        )
        event_id = await ctx.send_message(room_id, msg, thread_id=reply_tid)
        if event_id:

            def save_event_id(data: dict[str, Any]) -> None:
                for item in data.get("items", []):
                    if item["id"] == new_item["id"]:
                        item["event_id"] = event_id
                        break

            _locked_update_json(path, save_event_id)
            logger.info("workloop-command: created todo %s with event %s", new_item["id"], event_id)
        ctx.suppress = True

    except Exception:
        logger.exception("workloop-command: error handling command")
        await ctx.send_message(room_id, "\u26a0\ufe0f Error processing workloop command.", thread_id=reply_tid)
        ctx.suppress = True


# ══════════════════════════════════════════════════════════════════════
# Hook 4: message:enrich — inject thread work plan + mark busy
# ══════════════════════════════════════════════════════════════════════


@hook(
    event="message:enrich",
    name="workloop-context",
    priority=50,
)
async def inject_todos(ctx: MessageEnrichContext) -> list[EnrichmentItem]:
    """Inject thread work plan and mark target agent busy."""
    agent_name = ctx.target_entity_name
    room_id = ctx.envelope.room_id
    thread_id = _response_scope_thread_id(ctx.envelope)

    # Mark agent as busy for this scope
    run_key = f"{room_id}:{thread_id}"
    try:

        def _add_active_run(data: dict[str, Any]) -> None:
            data.setdefault("active_runs", {})[run_key] = {"started_at": _now_iso()}

        path = _agent_state_path(ctx.state_root, agent_name)
        _locked_update_json(path, _add_active_run)
    except Exception:
        logger.exception("workloop-context: failed to update agent state for %s", agent_name)

    # Load thread plan
    path = _todos_path(ctx.state_root, room_id, thread_id)
    try:
        state = _read_todos(path)
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
            lines.append(f"- {emoji} `{i['id']}` {i['title']} [{i.get('priority', 'medium')}]")
        if len(actionable) > max_items:
            lines.append(f"... and {len(actionable) - max_items} more")

    if blocked:
        lines.append("\nBlocked:")
        for i in blocked[:max_items]:
            waiting = [
                d for d in i.get("depends_on", []) if items_by_id.get(d, {}).get("status") not in TERMINAL_STATUSES
            ]
            waiting_str = ", ".join(f"`{d}`" for d in waiting)
            lines.append(f"- `{i['id']}` {i['title']} [{i.get('priority', 'medium')}] waiting on {waiting_str}")
        if len(blocked) > max_items:
            lines.append(f"... and {len(blocked) - max_items} more")

    if done:
        lines.append(f"\nDone: {done_count} item(s)")
        for i in done[:3]:
            lines.append(f"- `{i['id']}` {i['title']}")
        if len(done) > 3:
            lines.append(f"... and {len(done) - 3} more")

    lines.append("\nUse `complete_todo(todo_id)` when you finish an item.")

    return [EnrichmentItem(key="workloop", text="\n".join(lines), cache_policy="volatile")]


# ══════════════════════════════════════════════════════════════════════
# Hook 5: message:after_response — mark agent idle
# ══════════════════════════════════════════════════════════════════════


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
    thread_id = _response_scope_thread_id(ctx.result.envelope)
    run_key = f"{room_id}:{thread_id}"
    try:

        def _remove_active_run(data: dict[str, Any]) -> None:
            active_runs = data.get("active_runs", {})
            active_runs.pop(run_key, None)
            data["active_runs"] = active_runs
            data["last_response_at"] = _now_iso()

        path = _agent_state_path(ctx.state_root, agent_name)
        _locked_update_json(path, _remove_active_run)
    except Exception:
        logger.exception("workloop-track-idle: failed to update agent state for %s", agent_name)


# ══════════════════════════════════════════════════════════════════════
# Hook 6: schedule:fired — suppress legacy scheduled heartbeat
# ══════════════════════════════════════════════════════════════════════



# ══════════════════════════════════════════════════════════════════════
# Hook 7: reaction:received — quick completion via reactions
# ══════════════════════════════════════════════════════════════════════


@hook(
    event="reaction:received",
    name="workloop-react",
    priority=100,
    timeout_ms=5000,
)
async def workloop_react(ctx: ReactionReceivedContext) -> None:
    """Complete or cancel a todo via a reaction on its announcement message."""
    if ctx.reaction_key not in ("\u2705", "\u274c"):
        return

    # We need to scan thread dirs to find which thread file contains
    # an item with matching event_id
    threads_dir = ctx.state_root / "threads"
    if not threads_dir.exists():
        return

    target_event_id = ctx.target_event_id

    for todos_path in threads_dir.glob("*/todos.json"):
        try:
            state = _read_todos(todos_path)
        except Exception:
            continue

        matched_item = None
        for item in state.get("items", []):
            if item.get("event_id") == target_event_id:
                matched_item = item
                break

        if matched_item is None:
            continue

        if matched_item["status"] in TERMINAL_STATUSES:
            return

        new_status = "done" if ctx.reaction_key == "\u2705" else "cancelled"

        def react_update(data: dict[str, Any]) -> str | None:
            for item in data.get("items", []):
                if item.get("event_id") == target_event_id:
                    if item["status"] in TERMINAL_STATUSES:
                        return None
                    item["status"] = new_status
                    item["updated_at"] = _now_iso()
                    if new_status == "done":
                        item["completed_at"] = _now_iso()
                    data["updated_at"] = _now_iso()
                    unblocked = _newly_unblocked(data["items"], item["id"])
                    if new_status == "done":
                        msg = f"\u2705 Completed: **{item['title']}**"
                    else:
                        msg = f"\u274c Cancelled: **{item['title']}**"
                    if unblocked:
                        names = ", ".join(f"`{u['id']}` {u['title']}" for u in unblocked)
                        msg += f"\n\u2197\ufe0f Now unblocked: {names}"
                    return msg
            return None

        try:
            result = _locked_update_json(todos_path, react_update)
            if result:
                matrix_thread_id = None if state.get("thread_id") == "main" else state.get("thread_id")
                await ctx.send_message(ctx.room_id, result, thread_id=matrix_thread_id)
                logger.info("workloop-react: %s item via reaction on %s", new_status, target_event_id)
        except Exception:
            logger.exception("workloop-react: error handling reaction")
        return
