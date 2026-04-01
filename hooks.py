"""Hook entrypoint for the MindRoom workloop plugin."""

from __future__ import annotations

import asyncio
import sys
from importlib import import_module, util
from pathlib import Path
from types import ModuleType
from typing import Any

from mindroom.hooks import hook

_PLUGIN_ROOT = Path(__file__).resolve().parent
_PACKAGE_NAME = f"{__name__}_modules"


def _ensure_package() -> None:
    """Register a synthetic package so split modules can use relative imports."""
    if _PACKAGE_NAME in sys.modules:
        return

    _PACKAGE_SPEC = util.spec_from_loader(_PACKAGE_NAME, loader=None, is_package=True)
    _PACKAGE_MODULE = ModuleType(_PACKAGE_NAME)
    _PACKAGE_MODULE.__file__ = str(_PLUGIN_ROOT / "__init__.py")
    _PACKAGE_MODULE.__package__ = _PACKAGE_NAME
    _PACKAGE_MODULE.__path__ = [str(_PLUGIN_ROOT)]
    if _PACKAGE_SPEC is not None:
        _PACKAGE_SPEC.submodule_search_locations = [str(_PLUGIN_ROOT)]
        _PACKAGE_MODULE.__spec__ = _PACKAGE_SPEC
    sys.modules[_PACKAGE_NAME] = _PACKAGE_MODULE


def _load_module(name: str) -> ModuleType:
    return import_module(f"{_PACKAGE_NAME}.{name}")


_ensure_package()

workloop_types = _load_module("types")
state = _load_module("state")
todos = _load_module("todos")
formatting = _load_module("formatting")
poke = _load_module("poke")
commands = _load_module("commands")

logger = workloop_types.logger
ROUTER_AGENT_NAME = workloop_types.ROUTER_AGENT_NAME
_AUTO_POKE_HOOK_SOURCE = workloop_types._AUTO_POKE_HOOK_SOURCE
DEFAULT_POKE_INTERVAL_SECONDS = workloop_types.DEFAULT_POKE_INTERVAL_SECONDS
AutoPokeRuntime = workloop_types.AutoPokeRuntime

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


inject_todos = poke.inject_todos
track_idle = poke.track_idle
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
    "start_auto_poke_loop",
    "state",
    "stop_auto_poke_loop",
    "todos",
    "track_idle",
    "workloop_command",
    "workloop_react",
    "workloop_types",
]
