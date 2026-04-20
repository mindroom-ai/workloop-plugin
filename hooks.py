"""Hook entrypoint for the MindRoom workloop plugin."""

from __future__ import annotations

import asyncio
from typing import Any

from mindroom.hooks import hook
from . import commands, formatting, poke, runtime as workloop_runtime, state, todos
from .runtime import (
    AutoPokeRuntime,
    DEFAULT_POKE_INTERVAL_SECONDS,
    ROUTER_AGENT_NAME,
    _AUTO_POKE_HOOK_SOURCE,
    logger,
)

_run_poke_scan = poke.run_poke_scan
_parse_poke_interval_seconds = poke._parse_poke_interval_seconds
_build_auto_poke_runtime = poke._build_auto_poke_runtime
_has_pending_schedules = poke._has_pending_schedules
_should_poke_agent = poke._should_poke_agent

_AUTO_POKE_TASK: asyncio.Task[None] | None = None


async def _auto_poke_loop(runtime: AutoPokeRuntime) -> None:
    """Run the background poke loop using this facade's patchable helpers."""
    runtime.logger.info("workloop-auto-poke: started")
    try:
        while True:
            interval = _parse_poke_interval_seconds(runtime.settings, runtime.logger)
            try:
                await asyncio.sleep(interval)
                pokes_sent = await _run_poke_scan(runtime)
                runtime.logger.info(
                    "workloop-auto-poke: scan complete, %d poke(s) sent", pokes_sent
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                runtime.logger.exception("workloop-auto-poke: scan failed; continuing")
    except asyncio.CancelledError:
        runtime.logger.info("workloop-auto-poke: stopped")
        raise


@hook(
    event="schedule:fired",
    name="auto_poke",
    priority=100,
    timeout_ms=5000,
)
async def auto_poke(ctx: Any) -> None:
    """Suppress deprecated scheduled `!workloop-tick` heartbeats."""
    if getattr(ctx, "message_text", "").strip() != "!workloop-tick":
        return
    logger.warning(
        "workloop-auto-poke: suppressing deprecated scheduled !workloop-tick heartbeat"
    )
    ctx.suppress = True


async def start_auto_poke_loop(ctx: Any) -> None:
    """Start the background auto-poke loop once per router lifecycle."""
    global _AUTO_POKE_TASK

    if ctx.entity_name != ROUTER_AGENT_NAME:
        return
    if _AUTO_POKE_TASK is not None and not _AUTO_POKE_TASK.done():
        return

    runtime = _build_auto_poke_runtime(ctx)
    _AUTO_POKE_TASK = asyncio.create_task(
        _auto_poke_loop(runtime), name="workloop-auto-poke"
    )


async def stop_auto_poke_loop(ctx: Any) -> None:
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


start_auto_poke_loop = hook(
    event="agent:started",
    name="workloop-auto-poke-start",
    agents=(ROUTER_AGENT_NAME,),
    priority=100,
    timeout_ms=5000,
)(start_auto_poke_loop)

stop_auto_poke_loop = hook(
    event="agent:stopped",
    name="workloop-auto-poke-stop",
    agents=(ROUTER_AGENT_NAME,),
    priority=100,
    timeout_ms=5000,
)(stop_auto_poke_loop)


async def restart_auto_poke_loop_on_reload(ctx: Any) -> None:
    """Restart the auto-poke loop after plugin hot-reload kills the prior task.

    `agent:started` only fires once per service boot, so plugin reloads (which
    replace the module instance and reset `_AUTO_POKE_TASK = None`) leave the
    loop dead until the service restarts. `config:reloaded` fires on every
    reload, so this hook re-creates the task idempotently.
    """
    global _AUTO_POKE_TASK

    if _AUTO_POKE_TASK is not None and not _AUTO_POKE_TASK.done():
        return

    runtime = _build_auto_poke_runtime(ctx)
    _AUTO_POKE_TASK = asyncio.create_task(
        _auto_poke_loop(runtime), name="workloop-auto-poke"
    )
    runtime.logger.info("workloop-auto-poke: started (after config reload)")


restart_auto_poke_loop_on_reload = hook(
    event="config:reloaded",
    name="workloop-auto-poke-restart-on-reload",
    priority=100,
    timeout_ms=5000,
)(restart_auto_poke_loop_on_reload)


async def workloop_command(ctx: Any) -> None:
    """Facade for the command hook that preserves local test patching."""
    body = ctx.envelope.body.strip()
    if body == "!workloop-tick":
        room_id, _, reply_tid = state.resolve_scope(ctx.envelope)
        pokes_sent = await _run_poke_scan(ctx)
        await ctx.send_message(
            room_id,
            f"🔄 Workloop tick: {pokes_sent} poke(s) sent.",
            thread_id=reply_tid,
        )
        ctx.suppress = True
        return
    await commands.workloop_command(ctx)


workloop_command = hook(
    event="message:received",
    name="workloop-command",
    agents=(ROUTER_AGENT_NAME,),
    priority=100,
    timeout_ms=15000,
)(workloop_command)


inject_todos = poke.inject_todos
track_idle = poke.track_idle
track_cancelled = poke.track_cancelled
workloop_react = todos.workloop_react

__all__ = [
    "_AUTO_POKE_TASK",
    "AutoPokeRuntime",
    "DEFAULT_POKE_INTERVAL_SECONDS",
    "ROUTER_AGENT_NAME",
    "_AUTO_POKE_HOOK_SOURCE",
    "_auto_poke_loop",
    "_build_auto_poke_runtime",
    "_has_pending_schedules",
    "_parse_poke_interval_seconds",
    "_run_poke_scan",
    "_should_poke_agent",
    "auto_poke",
    "commands",
    "formatting",
    "inject_todos",
    "logger",
    "poke",
    "restart_auto_poke_loop_on_reload",
    "start_auto_poke_loop",
    "state",
    "stop_auto_poke_loop",
    "todos",
    "track_cancelled",
    "track_idle",
    "workloop_command",
    "workloop_react",
    "workloop_runtime",
]
