"""Workloop list and plan formatting helpers."""

from __future__ import annotations

from typing import Any

from .todos import is_actionable, is_blocked
from .runtime import PRIORITY_EMOJI, PRIORITY_ORDER, TERMINAL_STATUSES


def format_item_line(item: dict[str, Any], *, show_status: bool = False) -> str:
    emoji = PRIORITY_EMOJI.get(item.get("priority", "medium"), "\u26aa")
    status_mark = ""
    if show_status:
        if item["status"] == "done":
            status_mark = "\u2705 "
        elif item["status"] == "cancelled":
            status_mark = "\u274c "
    assigned = f" @{item['assigned_agent']}" if item.get("assigned_agent") else ""
    return f"  {emoji} `{item['id']}` {status_mark}{item['title']} [{item.get('priority', 'medium')}]{assigned}"


def format_list(items: list[dict[str, Any]], *, show_all: bool = False) -> str:
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
            lines.append(format_item_line(i))

    if blocked:
        lines.append("\n**Blocked:**")
        for i in blocked:
            waiting = [
                d
                for d in i.get("depends_on", [])
                if items_by_id.get(d, {}).get("status") not in TERMINAL_STATUSES
            ]
            waiting_str = ", ".join(f"`{d}`" for d in waiting)
            lines.append(f"{format_item_line(i)} waiting on {waiting_str}")

    if show_all and done:
        lines.append("\n**Done/Cancelled:**")
        for i in done:
            lines.append(format_item_line(i, show_status=True))

    return "\n".join(lines)


def format_plan(items: list[dict[str, Any]]) -> str:
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
