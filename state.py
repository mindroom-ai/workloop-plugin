"""Workloop state helpers."""

from __future__ import annotations

import fcntl
import json
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_T = Any  # generic return from mutate callback


def _sanitize(value: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^A-Za-z0-9]", "_", value)).strip("_")


def _thread_key(room_id: str, thread_id: str | None) -> str:
    resolved = thread_id or "main"
    return f"{_sanitize(room_id)}_{_sanitize(resolved)}"


def resolve_scope(envelope: Any) -> tuple[str, str | None, str | None]:
    """Return (room_id, storage_thread_id, reply_thread_id).

    storage_thread_id: None for room-level → becomes "main" in _thread_key.
    reply_thread_id: resolved_thread_id for sending responses in the right thread.

    ``thread_id`` on the envelope is None for room-level messages.
    ``resolved_thread_id`` is always set (may equal the message's own event ID for
    room-level messages), so it is the correct value for replying in-thread.
    """
    room_id = envelope.room_id
    storage_tid = envelope.target.thread_id  # None for room-level, set for threads
    reply_tid = envelope.target.resolved_thread_id if envelope.target.thread_id else None
    return room_id, storage_tid, reply_tid


def response_scope_thread_id(envelope: Any) -> str:
    """Return the actual response-scope thread key for agent-generated work state."""
    return envelope.target.resolved_thread_id or envelope.target.thread_id or "main"


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def short_id(existing_ids: set[str]) -> str:
    while True:
        candidate = uuid.uuid4().hex[:8]
        if candidate not in existing_ids:
            return candidate


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def locked_update_json(path: Path, mutate: Any) -> Any:
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


def todos_path(state_root: Path, room_id: str, thread_id: str | None) -> Path:
    key = _thread_key(room_id, thread_id)
    return state_root / "threads" / key / "todos.json"


def agent_state_path(state_root: Path, agent_name: str) -> Path:
    return state_root / "agents" / f"{agent_name}.json"


def read_agent_state(state_root: Path, agent_name: str) -> dict[str, Any]:
    path = agent_state_path(state_root, agent_name)
    data = read_json(path)
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


def update_agent_state(state_root: Path, agent_name: str, updates: dict[str, Any]) -> None:
    path = agent_state_path(state_root, agent_name)

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

    locked_update_json(path, mutate)


def poke_agent_scope(state_root: Path, agent_name: str, scope_key: str, now: datetime) -> None:
    """Record a poke timestamp for a specific thread scope."""

    def mutate(data: dict[str, Any]) -> None:
        if not data:
            data["agent_name"] = agent_name
            data["active_runs"] = {}
            data["last_response_at"] = None
            data["last_poked_at"] = None
        poked_scopes: dict[str, str] = data.setdefault("poked_scopes", {})
        poked_scopes[scope_key] = now.isoformat()
        # Also set legacy field for backward compat
        data["last_poked_at"] = now.isoformat()

    path = agent_state_path(state_root, agent_name)
    locked_update_json(path, mutate)


# Backward-compatible aliases for older imports.
_resolve_scope = resolve_scope
_response_scope_thread_id = response_scope_thread_id
_now_iso = now_iso
_short_id = short_id
_read_json = read_json
_locked_update_json = locked_update_json
_todos_path = todos_path
_agent_state_path = agent_state_path
_read_agent_state = read_agent_state
_update_agent_state = update_agent_state
_poke_agent_scope = poke_agent_scope
