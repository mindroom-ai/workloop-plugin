"""Todo state helpers and reaction handling."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from mindroom.hooks import ReactionReceivedContext, hook

from .state import locked_update_json, now_iso, read_json
from .runtime import TERMINAL_STATUSES, logger


def read_todos(path: Path) -> dict[str, Any]:
    data = read_json(path)
    if not data:
        return {
            "room_id": "",
            "thread_id": "main",
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "items": [],
        }
    return data


def ensure_thread_state(
    data: dict[str, Any], room_id: str, thread_id: str | None
) -> None:
    """Ensure data dict has the required top-level fields."""
    resolved = thread_id or "main"
    if "items" not in data:
        data["room_id"] = room_id
        data["thread_id"] = resolved
        data["created_at"] = now_iso()
        data["updated_at"] = now_iso()
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


def would_create_cycle(
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


def newly_unblocked(
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
            state = read_todos(todos_path)
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
                    item["updated_at"] = now_iso()
                    if new_status == "done":
                        item["completed_at"] = now_iso()
                    data["updated_at"] = now_iso()
                    unblocked = newly_unblocked(data["items"], item["id"])
                    if new_status == "done":
                        msg = f"\u2705 Completed: **{item['title']}**"
                    else:
                        msg = f"\u274c Cancelled: **{item['title']}**"
                    if unblocked:
                        names = ", ".join(
                            f"`{u['id']}` {u['title']}" for u in unblocked
                        )
                        msg += f"\n\u2197\ufe0f Now unblocked: {names}"
                    return msg
            return None

        try:
            result = locked_update_json(todos_path, react_update)
            if result:
                matrix_thread_id = (
                    None if state.get("thread_id") == "main" else state.get("thread_id")
                )
                await ctx.send_message(ctx.room_id, result, thread_id=matrix_thread_id)
                logger.info(
                    "workloop-react: %s item via reaction on %s",
                    new_status,
                    target_event_id,
                )
        except Exception:
            logger.exception("workloop-react: error handling reaction")
        return
