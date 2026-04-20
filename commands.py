"""Workloop command hook."""

from __future__ import annotations

from typing import Any

from mindroom.hooks import MessageReceivedContext, hook

from .formatting import format_list, format_plan
from .poke import run_poke_scan
from .state import locked_update_json, now_iso, resolve_scope, short_id, todos_path
from .todos import ensure_thread_state, newly_unblocked, read_todos, would_create_cycle
from .runtime import (
    PRIORITY_EMOJI,
    ROUTER_AGENT_NAME,
    TERMINAL_STATUSES,
    VALID_PRIORITIES,
    logger,
)


@hook(
    event="message:received",
    name="workloop-command",
    agents=(ROUTER_AGENT_NAME,),
    priority=100,
    timeout_ms=15000,
)
async def workloop_command(ctx: MessageReceivedContext) -> None:
    """Handle `!todo` and `!workloop-tick` commands."""
    body = ctx.envelope.body.strip()
    room_id, thread_id, reply_tid = resolve_scope(ctx.envelope)

    if body == "!workloop-tick":
        try:
            pokes = await run_poke_scan(ctx)
            await ctx.send_message(
                room_id, f"🔄 Workloop tick: {pokes} poke(s) sent.", thread_id=reply_tid
            )
        except Exception:
            logger.exception("workloop-command: error running one-shot poke scan")
            await ctx.send_message(
                room_id, "⚠️ Error running workloop tick.", thread_id=reply_tid
            )
        ctx.suppress = True
        return

    if not body.startswith("!todo"):
        return

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

        path = todos_path(ctx.state_root, room_id, thread_id)

        # !todo list
        if parts == "list":
            state = read_todos(path)
            await ctx.send_message(
                room_id, format_list(state["items"]), thread_id=reply_tid
            )
            ctx.suppress = True
            return

        # !todo all
        if parts == "all":
            state = read_todos(path)
            await ctx.send_message(
                room_id, format_list(state["items"], show_all=True), thread_id=reply_tid
            )
            ctx.suppress = True
            return

        # !todo plan
        if parts == "plan":
            state = read_todos(path)
            await ctx.send_message(
                room_id, format_plan(state["items"]), thread_id=reply_tid
            )
            ctx.suppress = True
            return

        # !todo done <id>
        if parts.startswith("done "):
            todo_id = parts[5:].strip()

            def mark_done(data: dict[str, Any]) -> str:
                ensure_thread_state(data, room_id, thread_id)
                for item in data["items"]:
                    if item["id"] == todo_id:
                        if item["status"] in TERMINAL_STATUSES:
                            return f"Item `{todo_id}` is already {item['status']}."
                        item["status"] = "done"
                        item["completed_at"] = now_iso()
                        item["updated_at"] = now_iso()
                        data["updated_at"] = now_iso()
                        unblocked = newly_unblocked(data["items"], todo_id)
                        msg = f"\u2705 Completed: **{item['title']}** (`{todo_id}`)"
                        if unblocked:
                            names = ", ".join(
                                f"`{u['id']}` {u['title']}" for u in unblocked
                            )
                            msg += f"\n\u2197\ufe0f Now unblocked: {names}"
                        return msg
                return f"\u274c Todo `{todo_id}` not found."

            result = locked_update_json(path, mark_done)
            await ctx.send_message(room_id, result, thread_id=reply_tid)
            ctx.suppress = True
            return

        # !todo cancel <id>
        if parts.startswith("cancel "):
            todo_id = parts[7:].strip()

            def mark_cancel(data: dict[str, Any]) -> str:
                ensure_thread_state(data, room_id, thread_id)
                for item in data["items"]:
                    if item["id"] == todo_id:
                        if item["status"] in TERMINAL_STATUSES:
                            return f"Item `{todo_id}` is already {item['status']}."
                        item["status"] = "cancelled"
                        item["updated_at"] = now_iso()
                        data["updated_at"] = now_iso()
                        unblocked = newly_unblocked(data["items"], todo_id)
                        msg = f"\u274c Cancelled: **{item['title']}** (`{todo_id}`)"
                        if unblocked:
                            names = ", ".join(
                                f"`{u['id']}` {u['title']}" for u in unblocked
                            )
                            msg += f"\n\u2197\ufe0f Now unblocked: {names}"
                        return msg
                return f"\u274c Todo `{todo_id}` not found."

            result = locked_update_json(path, mark_cancel)
            await ctx.send_message(room_id, result, thread_id=reply_tid)
            ctx.suppress = True
            return

        # !todo rm <id>
        if parts.startswith("rm "):
            todo_id = parts[3:].strip()

            def remove(data: dict[str, Any]) -> str:
                ensure_thread_state(data, room_id, thread_id)
                original_len = len(data["items"])
                data["items"] = [i for i in data["items"] if i["id"] != todo_id]
                if len(data["items"]) == original_len:
                    return f"\u274c Todo `{todo_id}` not found."
                for item in data["items"]:
                    if todo_id in item.get("depends_on", []):
                        item["depends_on"].remove(todo_id)
                data["updated_at"] = now_iso()
                return f"\U0001f5d1\ufe0f Deleted todo `{todo_id}`."

            result = locked_update_json(path, remove)
            await ctx.send_message(room_id, result, thread_id=reply_tid)
            ctx.suppress = True
            return

        # !todo dep <id> <depends-on-id>
        if parts.startswith("dep "):
            dep_parts = parts[4:].strip().split()
            if len(dep_parts) != 2:
                await ctx.send_message(
                    room_id,
                    "\u274c Usage: `!todo dep <id> <depends-on-id>`",
                    thread_id=reply_tid,
                )
                ctx.suppress = True
                return
            item_id, dep_id = dep_parts

            def add_dep(data: dict[str, Any]) -> str:
                ensure_thread_state(data, room_id, thread_id)
                items_by_id = {i["id"]: i for i in data["items"]}
                if item_id not in items_by_id:
                    return f"\u274c Todo `{item_id}` not found."
                if dep_id not in items_by_id:
                    return f"\u274c Todo `{dep_id}` not found."
                if item_id == dep_id:
                    return "\u274c Cannot depend on itself."
                if dep_id in items_by_id[item_id].get("depends_on", []):
                    return f"Item `{item_id}` already depends on `{dep_id}`."
                if would_create_cycle(items_by_id, item_id, dep_id):
                    return "\u274c Adding this dependency would create a cycle."
                items_by_id[item_id].setdefault("depends_on", []).append(dep_id)
                items_by_id[item_id]["updated_at"] = now_iso()
                data["updated_at"] = now_iso()
                return f"\U0001f517 `{item_id}` now depends on `{dep_id}`."

            result = locked_update_json(path, add_dep)
            await ctx.send_message(room_id, result, thread_id=reply_tid)
            ctx.suppress = True
            return

        # !todo assign <id> <agent_name>
        if parts.startswith("assign "):
            assign_parts = parts[7:].strip().split()
            if len(assign_parts) != 2:
                await ctx.send_message(
                    room_id,
                    "\u274c Usage: `!todo assign <id> <agent>`",
                    thread_id=reply_tid,
                )
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
                ensure_thread_state(data, room_id, thread_id)
                for item in data["items"]:
                    if item["id"] == item_id:
                        item["assigned_agent"] = agent_name
                        item["updated_at"] = now_iso()
                        data["updated_at"] = now_iso()
                        return f"\U0001f464 `{item_id}` assigned to **{agent_name}**."
                return f"\u274c Todo `{item_id}` not found."

            result = locked_update_json(path, do_assign)
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
            await ctx.send_message(
                room_id,
                "\u274c Provide a title: `!todo add <title>`",
                thread_id=reply_tid,
            )
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
            ensure_thread_state(data, room_id, thread_id)
            existing_ids = {i["id"] for i in data["items"]}
            new_id = short_id(existing_ids)
            now = now_iso()
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

        new_item = locked_update_json(path, create_item)
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

            locked_update_json(path, save_event_id)
            logger.info(
                "workloop-command: created todo %s with event %s",
                new_item["id"],
                event_id,
            )
        ctx.suppress = True

    except Exception:
        logger.exception("workloop-command: error handling command")
        await ctx.send_message(
            room_id,
            "\u26a0\ufe0f Error processing workloop command.",
            thread_id=reply_tid,
        )
        ctx.suppress = True
