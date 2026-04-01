"""Agent-facing tools for the MindRoom workloop plugin.

This file is self-contained — all models, JSON helpers, lock helpers, and tool logic
are in one module to avoid relative-import issues with MindRoom's plugin loader
(which uses ``spec_from_file_location``).

Provides a ``WorkloopTodoManager`` toolkit that agents can use to create work plans,
add/complete/update/list per-thread todos with dependencies.
"""

from __future__ import annotations

import fcntl
import json
import logging
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agno.tools import Toolkit
from agno.agent import Agent
from agno.team.team import Team

from mindroom.tool_system.metadata import (
    SetupType,
    ToolCategory,
    ToolManagedInitArg,
    ToolStatus,
    register_tool_with_metadata,
)
from mindroom.tool_system.runtime_context import (
    get_plugin_state_root,
    get_tool_runtime_context,
)

# Runtime imports needed for Agno toolkit introspection.
if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths

logger = logging.getLogger(__name__)

_PLUGIN_NAME = "workloop"

# ══════════════════════════════════════════════════════════════════════
# Constants (duplicated from hooks.py — self-contained requirement)
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

# ══════════════════════════════════════════════════════════════════════
# Helpers (duplicated from hooks.py — self-contained requirement)
# ══════════════════════════════════════════════════════════════════════


def _sanitize(value: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^A-Za-z0-9]", "_", value)).strip("_")


def _thread_key(room_id: str, thread_id: str | None) -> str:
    resolved = thread_id or "main"
    return f"{_sanitize(room_id)}_{_sanitize(resolved)}"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _short_id(existing_ids: set[str]) -> str:
    while True:
        candidate = uuid.uuid4().hex[:8]
        if candidate not in existing_ids:
            return candidate


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _locked_update_json(path: Path, mutate: Any) -> Any:
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            data: dict[str, Any] = (
                json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            )
            result = mutate(data)
            path.write_text(
                json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            return result
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _todos_path(state_root: Path, room_id: str, thread_id: str | None) -> Path:
    key = _thread_key(room_id, thread_id)
    return state_root / "threads" / key / "todos.json"


def _ensure_thread_state(
    data: dict[str, Any], room_id: str, thread_id: str | None
) -> None:
    resolved = thread_id or "main"
    if "items" not in data:
        data["room_id"] = room_id
        data["thread_id"] = resolved
        data["created_at"] = _now_iso()
        data["updated_at"] = _now_iso()
        data["items"] = []


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


def _would_create_cycle(
    items_by_id: dict[str, dict[str, Any]], item_id: str, new_dep_id: str
) -> bool:
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


def _newly_unblocked(
    items: list[dict[str, Any]], changed_id: str
) -> list[dict[str, Any]]:
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
# Thread resolution via tool runtime context
# ══════════════════════════════════════════════════════════════════════


def _current_scope(runtime_paths: RuntimePaths | None) -> tuple[Path, str, str, str]:
    """Return (state_root, room_id, thread_id, agent_name) from runtime context."""
    ctx = get_tool_runtime_context()
    if ctx is None:
        msg = "workloop_todo_manager requires an active tool runtime context"
        raise RuntimeError(msg)
    state_root = get_plugin_state_root(_PLUGIN_NAME, runtime_paths=ctx.runtime_paths)
    # Agent tool calls must follow the actual response thread target so the
    # persisted work state lines up with later enrichment and auto-pokes.
    thread_id = ctx.resolved_thread_id or ctx.thread_id or "main"
    return state_root, ctx.room_id, thread_id, ctx.agent_name


def _configured_agent_names() -> set[str]:
    """Return the set of configured agent names from the runtime context."""
    ctx = get_tool_runtime_context()
    if ctx is None or ctx.config is None:
        return set()
    return set((ctx.config.agents or {}).keys())


# ══════════════════════════════════════════════════════════════════════
# Toolkit
# ══════════════════════════════════════════════════════════════════════


class WorkloopTodoManager(Toolkit):
    """Toolkit for managing per-thread work plans with dependencies.

    All operations are scoped to the current thread (or room-level if not
    in a thread). State is persisted in JSON files under the plugin state root.
    """

    def __init__(self, runtime_paths: object | None = None) -> None:
        self._runtime_paths = runtime_paths
        super().__init__(
            name="workloop_todo_manager",
            instructions=(
                "Use these tools to manage a per-thread work plan with dependencies. "
                "You can create plans, add individual tasks, complete them, and update "
                "priorities, assignments, and dependencies. Items are scoped to the "
                "current conversation thread. Use `plan` for multi-step work and "
                "`complete_todo` as you finish each item."
            ),
            tools=[
                self.plan,
                self.add_todo,
                self.complete_todo,
                self.list_todos,
                self.update_todo,
            ],
        )

    def plan(
        self,
        agent: Agent | Team,
        tasks: str,
    ) -> str:
        """Create a multi-step work plan for the current thread.

        Creates one todo item per non-empty line. Lines can optionally start
        with a ``[priority]`` prefix (e.g. ``[high] Implement auth``).
        All items are assigned to the calling agent by default.

        Args:
            agent: The calling agent (injected automatically).
            tasks: Multi-line string with one task per line.

        Returns:
            Confirmation with all created item IDs.

        """
        state_root, room_id, thread_id, agent_name = _current_scope(self._runtime_paths)
        path = _todos_path(state_root, room_id, thread_id)

        lines = [line.strip() for line in tasks.strip().splitlines() if line.strip()]
        if not lines:
            return "No tasks provided. Write one task per line."

        parsed: list[tuple[str, str]] = []
        for line in lines:
            priority = "medium"
            title = line
            # Strip leading list markers like "1.", "2.", "-", "*"
            title = re.sub(r"^(\d+[\.\)]\s*|[-*]\s+)", "", title).strip()
            for p in VALID_PRIORITIES:
                prefix = f"[{p}] "
                if title.lower().startswith(prefix):
                    priority = p
                    title = title[len(prefix) :]
                    break
            if title:
                parsed.append((title, priority))

        if not parsed:
            return "No valid tasks found after parsing."

        def create_plan(data: dict[str, Any]) -> list[dict[str, Any]]:
            _ensure_thread_state(data, room_id, thread_id)
            existing_ids = {i["id"] for i in data["items"]}
            created: list[dict[str, Any]] = []
            now = _now_iso()
            for title, priority in parsed:
                new_id = _short_id(existing_ids)
                existing_ids.add(new_id)
                item = {
                    "id": new_id,
                    "title": title,
                    "status": "open",
                    "priority": priority,
                    "depends_on": [],
                    "assigned_agent": agent_name,
                    "event_id": None,
                    "created_at": now,
                    "updated_at": now,
                    "completed_at": None,
                }
                data["items"].append(item)
                created.append(item)
            data["updated_at"] = now
            return created

        created = _locked_update_json(path, create_plan)

        result_lines = [f"Created {len(created)} item(s) in thread work plan:\n"]
        for item in created:
            emoji = PRIORITY_EMOJI.get(item["priority"], "")
            result_lines.append(
                f"- {emoji} `{item['id']}` {item['title']} [{item['priority']}]"
            )
        return "\n".join(result_lines)

    def add_todo(
        self,
        agent: Agent | Team,
        title: str,
        depends_on: str = "",
        priority: str = "medium",
        assigned_agent: str = "",
    ) -> str:
        """Add a single todo item to the current thread's work plan.

        Args:
            agent: The calling agent (injected automatically).
            title: Title or summary of the todo.
            depends_on: Comma-separated IDs of items this depends on.
            priority: Priority level: low, medium, high, or critical.
            assigned_agent: Agent name to assign to (defaults to calling agent).

        Returns:
            Confirmation message with the new todo's ID.

        """
        state_root, room_id, thread_id, agent_name = _current_scope(self._runtime_paths)
        path = _todos_path(state_root, room_id, thread_id)

        priority = priority.lower()
        if priority not in VALID_PRIORITIES:
            return (
                f"Invalid priority '{priority}'. Must be: low, medium, high, critical."
            )

        dep_ids = (
            [d.strip() for d in depends_on.split(",") if d.strip()]
            if depends_on
            else []
        )
        resolved_agent = assigned_agent.strip() or agent_name

        # Validate assignee against configured agents
        configured = _configured_agent_names()
        if resolved_agent and configured and resolved_agent not in configured:
            available = ", ".join(sorted(configured)) or "none"
            return f"Unknown agent '{resolved_agent}'. Available: {available}"

        def create_item(data: dict[str, Any]) -> dict[str, Any] | str:
            _ensure_thread_state(data, room_id, thread_id)
            items_by_id = {i["id"]: i for i in data["items"]}

            for dep_id in dep_ids:
                if dep_id not in items_by_id:
                    return f"Dependency `{dep_id}` not found."

            existing_ids = {i["id"] for i in data["items"]}
            new_id = _short_id(existing_ids)
            now = _now_iso()
            item = {
                "id": new_id,
                "title": title,
                "status": "open",
                "priority": priority,
                "depends_on": dep_ids,
                "assigned_agent": resolved_agent,
                "event_id": None,
                "created_at": now,
                "updated_at": now,
                "completed_at": None,
            }

            # Check cycles
            items_by_id[new_id] = item
            for dep_id in dep_ids:
                if _would_create_cycle(items_by_id, new_id, dep_id):
                    return f"Adding dependency `{dep_id}` would create a cycle."

            data["items"].append(item)
            data["updated_at"] = now
            return item

        result = _locked_update_json(path, create_item)
        if isinstance(result, str):
            return result

        emoji = PRIORITY_EMOJI.get(priority, "")
        msg = f"Created: {emoji} `{result['id']}` **{title}** [{priority}]"
        if dep_ids:
            msg += f" (depends on {', '.join(f'`{d}`' for d in dep_ids)})"
        if resolved_agent:
            msg += f" assigned to {resolved_agent}"
        return msg

    def complete_todo(
        self,
        agent: Agent | Team,
        todo_id: str,
    ) -> str:
        """Mark a todo item as completed.

        Args:
            agent: The calling agent (injected automatically).
            todo_id: The short ID of the todo to complete (e.g. "a1b2c3d4").

        Returns:
            Confirmation message, including any items that became unblocked.

        """
        state_root, room_id, thread_id, _ = _current_scope(self._runtime_paths)
        path = _todos_path(state_root, room_id, thread_id)

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
                        names = ", ".join(
                            f"`{u['id']}` {u['title']}" for u in unblocked
                        )
                        msg += f"\nNow unblocked: {names}"
                    return msg
            return f"Todo `{todo_id}` not found."

        return _locked_update_json(path, mark_done)

    def list_todos(
        self,
        agent: Agent | Team,
        show_all: bool = False,
    ) -> str:
        """List todo items in the current thread's work plan.

        Args:
            agent: The calling agent (injected automatically).
            show_all: If True, include done and cancelled items.

        Returns:
            Formatted list of matching todos.

        """
        state_root, room_id, thread_id, _ = _current_scope(self._runtime_paths)
        path = _todos_path(state_root, room_id, thread_id)
        state = _read_json(path)
        items = state.get("items", [])

        if not items:
            return "No items in this thread's work plan."

        items_by_id = {item["id"]: item for item in items}
        actionable = [i for i in items if is_actionable(i, items_by_id)]
        blocked = [
            i for i in items if i["status"] == "open" and is_blocked(i, items_by_id)
        ]
        done = [i for i in items if i["status"] in TERMINAL_STATUSES]

        actionable.sort(
            key=lambda i: PRIORITY_ORDER.get(i.get("priority", "medium"), 9)
        )

        total = len(items)
        done_count = len(done)
        result_lines = [f"Work plan: {done_count}/{total} complete.\n"]

        if actionable:
            result_lines.append("**Actionable:**")
            for i in actionable:
                emoji = PRIORITY_EMOJI.get(i.get("priority", "medium"), "")
                assigned = f" @{i['assigned_agent']}" if i.get("assigned_agent") else ""
                result_lines.append(
                    f"- {emoji} `{i['id']}` {i['title']} [{i.get('priority', 'medium')}]{assigned}"
                )

        if blocked:
            result_lines.append("\n**Blocked:**")
            for i in blocked:
                waiting = [
                    d
                    for d in i.get("depends_on", [])
                    if items_by_id.get(d, {}).get("status") not in TERMINAL_STATUSES
                ]
                waiting_str = ", ".join(f"`{d}`" for d in waiting)
                result_lines.append(
                    f"- `{i['id']}` {i['title']} waiting on {waiting_str}"
                )

        if show_all and done:
            result_lines.append("\n**Done/Cancelled:**")
            for i in done:
                mark = "\u2705" if i["status"] == "done" else "\u274c"
                result_lines.append(f"- {mark} `{i['id']}` {i['title']}")

        return "\n".join(result_lines)

    def update_todo(
        self,
        agent: Agent | Team,
        todo_id: str,
        title: str = "",
        priority: str = "",
        status: str = "",
        depends_on: str = "",
        assigned_agent: str = "",
    ) -> str:
        """Update fields on an existing todo item.

        Args:
            agent: The calling agent (injected automatically).
            todo_id: The short ID of the todo to update.
            title: New title (leave empty to keep current).
            priority: New priority: low, medium, high, critical (leave empty to keep).
            status: New status: open, done, cancelled (leave empty to keep).
            depends_on: Comma-separated dependency IDs, replaces existing (leave empty to keep).
            assigned_agent: New agent assignment (leave empty to keep).

        Returns:
            Confirmation with updated todo details.

        """
        state_root, room_id, thread_id, _ = _current_scope(self._runtime_paths)
        path = _todos_path(state_root, room_id, thread_id)

        if priority and priority.lower() not in VALID_PRIORITIES:
            return (
                f"Invalid priority '{priority}'. Must be: low, medium, high, critical."
            )
        if status and status.lower() not in {"open", "done", "cancelled"}:
            return f"Invalid status '{status}'. Must be: open, done, cancelled."

        # Validate assignee against configured agents
        if assigned_agent and assigned_agent.strip():
            configured = _configured_agent_names()
            if configured and assigned_agent.strip() not in configured:
                available = ", ".join(sorted(configured)) or "none"
                return (
                    f"Unknown agent '{assigned_agent.strip()}'. Available: {available}"
                )

        def do_update(data: dict[str, Any]) -> str:
            _ensure_thread_state(data, room_id, thread_id)
            items_by_id = {i["id"]: i for i in data["items"]}
            if todo_id not in items_by_id:
                return f"Todo `{todo_id}` not found."

            item = items_by_id[todo_id]
            changes: list[str] = []

            if title:
                item["title"] = title
                changes.append(f"title='{title}'")
            if priority:
                item["priority"] = priority.lower()
                changes.append(f"priority={priority.lower()}")
            if status:
                new_status = status.lower()
                item["status"] = new_status
                if new_status == "done":
                    item["completed_at"] = _now_iso()
                else:
                    item["completed_at"] = None
                changes.append(f"status={new_status}")
            if depends_on:
                dep_ids = [d.strip() for d in depends_on.split(",") if d.strip()]
                for dep_id in dep_ids:
                    if dep_id not in items_by_id:
                        return f"Dependency `{dep_id}` not found."
                    if dep_id == todo_id:
                        return "Cannot depend on itself."
                    if _would_create_cycle(items_by_id, todo_id, dep_id):
                        return f"Adding dependency `{dep_id}` would create a cycle."
                item["depends_on"] = dep_ids
                changes.append(f"depends_on={dep_ids}")
            if assigned_agent:
                item["assigned_agent"] = assigned_agent.strip()
                changes.append(f"assigned={assigned_agent.strip()}")

            if not changes:
                return "No fields to update."

            item["updated_at"] = _now_iso()
            data["updated_at"] = _now_iso()

            unblocked_msg = ""
            if status and status.lower() in TERMINAL_STATUSES:
                unblocked = _newly_unblocked(data["items"], todo_id)
                if unblocked:
                    names = ", ".join(f"`{u['id']}` {u['title']}" for u in unblocked)
                    unblocked_msg = f"\nNow unblocked: {names}"

            return f"Updated `{todo_id}`: {', '.join(changes)}{unblocked_msg}"

        return _locked_update_json(path, do_update)


# ══════════════════════════════════════════════════════════════════════
# Registration
# ══════════════════════════════════════════════════════════════════════


@register_tool_with_metadata(
    name="workloop_todo_manager",
    display_name="Workloop Todo Manager",
    description="Create and manage per-thread work plans with dependencies.",
    category=ToolCategory.PRODUCTIVITY,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    icon="SiTodoist",
    icon_color="text-blue-500",
    managed_init_args=(ToolManagedInitArg.RUNTIME_PATHS,),
)
def workloop_todo_manager_factory() -> type[WorkloopTodoManager]:
    """Factory function for the WorkloopTodoManager toolkit."""
    return WorkloopTodoManager
