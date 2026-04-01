"""Shared workloop types and constants."""

from __future__ import annotations

import os

if __name__ == "types":
    _STDLIB_TYPES_PATH = os.path.join(os.path.dirname(os.__file__), "types.py")
    with open(_STDLIB_TYPES_PATH, "rb") as _stdlib_types_file:
        exec(compile(_stdlib_types_file.read(), _STDLIB_TYPES_PATH, "exec"), globals())
else:
    import logging
    from dataclasses import dataclass
    from pathlib import Path
    from typing import Any, Protocol

    from mindroom.constants import ROUTER_AGENT_NAME
    from mindroom.hooks import HookMessageSender, HookRoomStateQuerier

    LOGGER_NAME = __name__.rsplit(".", 1)[0].removesuffix("_modules") if "." in __name__ else __name__
    logger = logging.getLogger(LOGGER_NAME)

    _PLUGIN_NAME = "workloop"
    _AUTO_POKE_HOOK_SOURCE = f"{_PLUGIN_NAME}:auto_poke"
    _TRIGGER_DISPATCH_CONTENT_KEY = "com.mindroom._trigger_dispatch"

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
            trigger_dispatch: bool = False,
        ) -> str | None: ...

        async def query_room_state(
            self,
            room_id: str,
            event_type: str,
            state_key: str | None = None,
        ) -> dict[str, Any] | None: ...


    @dataclass(slots=True)
    class AutoPokeRuntime:
        settings: dict[str, Any]
        config: Any
        state_root: Path
        logger: Any
        _message_sender: HookMessageSender | None
        _room_state_querier: HookRoomStateQuerier | None

        async def send_message(
            self,
            room_id: str,
            text: str,
            *,
            thread_id: str | None = None,
            extra_content: dict[str, Any] | None = None,
            trigger_dispatch: bool = False,
        ) -> str | None:
            if self._message_sender is None:
                self.logger.warning("workloop-auto-poke: send_message called but no sender registered")
                return None
            resolved_extra_content = dict(extra_content or {})
            if trigger_dispatch:
                resolved_extra_content[_TRIGGER_DISPATCH_CONTENT_KEY] = True
            return await self._message_sender(
                room_id,
                text,
                thread_id,
                _AUTO_POKE_HOOK_SOURCE,
                resolved_extra_content or None,
            )

        async def query_room_state(
            self,
            room_id: str,
            event_type: str,
            state_key: str | None = None,
        ) -> dict[str, Any] | None:
            if self._room_state_querier is None:
                self.logger.warning("workloop-auto-poke: query_room_state called but no querier registered")
                return None
            return await self._room_state_querier(room_id, event_type, state_key)
