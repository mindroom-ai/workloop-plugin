"""Hook handlers for the MindRoom workloop plugin."""

from __future__ import annotations

import asyncio
import sys
from importlib import import_module, util
from pathlib import Path
from types import ModuleType

_PLUGIN_ROOT = Path(__file__).resolve().parent
_PACKAGE_NAME = f"{__name__}_modules"

if _PACKAGE_NAME not in sys.modules:
    _PACKAGE_SPEC = util.spec_from_loader(_PACKAGE_NAME, loader=None, is_package=True)
    _PACKAGE_MODULE = ModuleType(_PACKAGE_NAME)
    _PACKAGE_MODULE.__file__ = str(_PLUGIN_ROOT / "__init__.py")
    _PACKAGE_MODULE.__package__ = _PACKAGE_NAME
    _PACKAGE_MODULE.__path__ = [str(_PLUGIN_ROOT)]
    if _PACKAGE_SPEC is not None:
        _PACKAGE_SPEC.submodule_search_locations = [str(_PLUGIN_ROOT)]
        _PACKAGE_MODULE.__spec__ = _PACKAGE_SPEC
    sys.modules[_PACKAGE_NAME] = _PACKAGE_MODULE

_types = import_module(f"{_PACKAGE_NAME}.types")
_state = import_module(f"{_PACKAGE_NAME}.state")
_todos = import_module(f"{_PACKAGE_NAME}.todos")
_formatting = import_module(f"{_PACKAGE_NAME}.formatting")
_poke = import_module(f"{_PACKAGE_NAME}.poke")
_commands = import_module(f"{_PACKAGE_NAME}.commands")

workloop_types = _types
state = _state
todos = _todos
formatting = _formatting
poke = _poke
commands = _commands

logger = _types.logger
ROUTER_AGENT_NAME = _types.ROUTER_AGENT_NAME
_PLUGIN_NAME = _types._PLUGIN_NAME
_AUTO_POKE_HOOK_SOURCE = _types._AUTO_POKE_HOOK_SOURCE
_TRIGGER_DISPATCH_CONTENT_KEY = _types._TRIGGER_DISPATCH_CONTENT_KEY
VALID_PRIORITIES = _types.VALID_PRIORITIES
TERMINAL_STATUSES = _types.TERMINAL_STATUSES
PRIORITY_EMOJI = _types.PRIORITY_EMOJI
PRIORITY_ORDER = _types.PRIORITY_ORDER
DEFAULT_POKE_INTERVAL_SECONDS = _types.DEFAULT_POKE_INTERVAL_SECONDS
PokeScanContext = _types.PokeScanContext
AutoPokeRuntime = _types.AutoPokeRuntime

_sanitize = _state._sanitize
_thread_key = _state._thread_key
_resolve_scope = _state._resolve_scope
_response_scope_thread_id = _state._response_scope_thread_id
_now_iso = _state._now_iso
_short_id = _state._short_id
_read_json = _state._read_json
_locked_update_json = _state._locked_update_json
_todos_path = _state._todos_path
_agent_state_path = _state._agent_state_path
_read_agent_state = _state._read_agent_state
_update_agent_state = _state._update_agent_state
_poke_agent_scope = _state._poke_agent_scope

_read_todos = _todos._read_todos
_ensure_thread_state = _todos._ensure_thread_state
is_blocked = _todos.is_blocked
is_actionable = _todos.is_actionable
_would_create_cycle = _todos._would_create_cycle
_newly_unblocked = _todos._newly_unblocked

_format_item_line = _formatting._format_item_line
_format_list = _formatting._format_list
_format_plan = _formatting._format_plan

_run_poke_scan = _poke._run_poke_scan
_parse_poke_interval_seconds = _poke._parse_poke_interval_seconds
_auto_poke_loop = _poke._auto_poke_loop
_build_auto_poke_runtime = _poke._build_auto_poke_runtime

start_auto_poke_loop = _poke.start_auto_poke_loop
stop_auto_poke_loop = _poke.stop_auto_poke_loop
workloop_command = _commands.workloop_command
inject_todos = _poke.inject_todos
track_idle = _poke.track_idle
workloop_react = _todos.workloop_react

__all__ = [
    "AutoPokeRuntime",
    "DEFAULT_POKE_INTERVAL_SECONDS",
    "PokeScanContext",
    "PRIORITY_EMOJI",
    "PRIORITY_ORDER",
    "ROUTER_AGENT_NAME",
    "TERMINAL_STATUSES",
    "VALID_PRIORITIES",
    "_AUTO_POKE_HOOK_SOURCE",
    "_TRIGGER_DISPATCH_CONTENT_KEY",
    "_PLUGIN_NAME",
    "_agent_state_path",
    "_auto_poke_loop",
    "_build_auto_poke_runtime",
    "_ensure_thread_state",
    "_format_item_line",
    "_format_list",
    "_format_plan",
    "_locked_update_json",
    "_newly_unblocked",
    "_now_iso",
    "_parse_poke_interval_seconds",
    "_poke_agent_scope",
    "_read_agent_state",
    "_read_json",
    "_read_todos",
    "_resolve_scope",
    "_response_scope_thread_id",
    "_run_poke_scan",
    "_sanitize",
    "_short_id",
    "_thread_key",
    "_todos_path",
    "_update_agent_state",
    "_would_create_cycle",
    "commands",
    "formatting",
    "inject_todos",
    "is_actionable",
    "is_blocked",
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
