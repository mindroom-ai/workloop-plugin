"""Integration coverage for ISSUE-179.

Historical regressions covered here:
- ISSUE-067: response-thread scope drift
- ISSUE-091: standalone pytest package bootstrap drift
- ISSUE-162: multi-thread and multi-agent scan isolation
- Cancellation hook gaps: idle/cancel cleanup now tested end to end
"""

from __future__ import annotations

import json
import multiprocessing
import sys
from datetime import UTC, datetime, timedelta
from importlib import util
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest
from mindroom.matrix.cache import CacheUnavailable
from mindroom.matrix.cache.event_cache import _EventCache

PACKAGE_NAME = (
    f"mindroom_plugin_{Path(__file__).resolve().parents[1].name.replace('-', '_')}"
)


class CacheConfigStub:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path

    def resolve_db_path(self, _runtime_paths: Any) -> Path:
        return self._db_path


class RuntimePathsStub:
    def __init__(self, storage_root: Path) -> None:
        self.storage_root = storage_root

    def env_value(self, _name: str, *, default: str | None = None) -> str | None:
        return default


class ConfigStub:
    def __init__(self, agents: dict[str, Any], db_path: Path) -> None:
        self.agents = agents
        self.cache = CacheConfigStub(db_path)

    def get_domain(self, _runtime_paths: Any) -> str:
        return "test"


def _load_hooks_module():
    for suffix in (
        "hooks",
        "poke",
        "state",
        "todos",
        "types",
        "commands",
        "formatting",
    ):
        sys.modules.pop(f"{PACKAGE_NAME}.{suffix}", None)
    hooks_path = Path(__file__).resolve().parents[1] / "hooks.py"
    module_name = f"{PACKAGE_NAME}.hooks"
    spec = util.spec_from_file_location(module_name, hooks_path)
    assert spec is not None
    assert spec.loader is not None
    module = util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class ScanContextStub:
    def __init__(
        self,
        state_root: Path,
        *,
        agents: dict[str, Any],
        settings: dict[str, Any] | None = None,
        runtime_started_at: float | None = 0.0,
    ) -> None:
        self.state_root = state_root
        self.runtime_paths = RuntimePathsStub(state_root)
        self.runtime_started_at = runtime_started_at
        self.config = ConfigStub(agents, state_root / "event_cache.db")
        self.settings = {
            "poke_cooldown_seconds": 300,
            "recent_response_grace_seconds": 30,
            "max_pokes_per_tick": 10,
            "min_idle_before_poke_seconds": 0,
            **(settings or {}),
        }
        self.sent_messages: list[dict[str, Any]] = []
        self.query_calls: list[tuple[str, str, str | None]] = []
        self.latest_messages: dict[tuple[str, str | None, str], Any] = {}
        self.query_result = Mock()
        self.query_result.payloads = []
        self.query_result.items.return_value = []

    async def send_message(
        self,
        room_id: str,
        text: str,
        *,
        thread_id: str | None = None,
        extra_content: dict[str, Any] | None = None,
        trigger_dispatch: bool = False,
    ) -> str:
        self.sent_messages.append(
            {
                "room_id": room_id,
                "text": text,
                "thread_id": thread_id,
                "extra_content": extra_content,
                "trigger_dispatch": trigger_dispatch,
            }
        )
        return "$event"

    async def query_room_state(
        self,
        room_id: str,
        event_type: str,
        state_key: str | None = None,
    ) -> Mock:
        self.query_calls.append((room_id, event_type, state_key))
        return self.query_result

    async def read_latest_thread_message(
        self,
        room_id: str,
        thread_id: str | None,
        sender: str,
    ) -> Any:
        return self.latest_messages.get((room_id, thread_id, sender))


def _write_thread_todos(
    module,
    state_root: Path,
    room_id: str,
    thread_id: str | None,
    items: list[dict[str, Any]],
) -> Path:
    path = module.state.todos_path(state_root, room_id, thread_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "room_id": room_id,
                "thread_id": thread_id or "main",
                "items": items,
            }
        ),
        encoding="utf-8",
    )
    return path


def _todo(todo_id: str, title: str, agent_name: str) -> dict[str, Any]:
    return {
        "id": todo_id,
        "title": title,
        "status": "open",
        "priority": "medium",
        "assigned_agent": agent_name,
        "depends_on": [],
    }


def _make_envelope(
    *,
    room_id: str,
    source_thread_id: str | None,
    resolved_thread_id: str | None,
    agent_name: str = "worker",
):
    thread_id = source_thread_id
    return SimpleNamespace(
        room_id=room_id,
        body="",
        agent_name=agent_name,
        target=SimpleNamespace(
            source_thread_id=source_thread_id,
            thread_id=thread_id,
            resolved_thread_id=resolved_thread_id,
        ),
    )


def _increment_locked_counter(
    path: Path, state_module_path: Path, iterations: int
) -> None:
    spec = util.spec_from_file_location(
        f"{PACKAGE_NAME}.state_lock_worker", state_module_path
    )
    assert spec is not None
    assert spec.loader is not None
    state_module = util.module_from_spec(spec)
    spec.loader.exec_module(state_module)
    for _ in range(iterations):
        state_module.locked_update_json(
            path, lambda data: data.__setitem__("counter", data.get("counter", 0) + 1)
        )


def _set_latest_message(
    module,
    ctx: ScanContextStub,
    *,
    room_id: str,
    thread_id: str | None,
    agent_name: str,
    status: str,
    age_minutes: int,
) -> None:
    sender = module.poke._agent_matrix_user_id(ctx, agent_name)
    ctx.latest_messages[(room_id, thread_id, sender)] = module.poke.ThreadMessageSnapshot(
        content={module.poke.STREAM_STATUS_KEY: status},
        origin_server_ts=datetime.now(UTC) - timedelta(minutes=age_minutes),
    )


@pytest.mark.asyncio
async def test_streaming_thread_blocks_poke(tmp_path: Path) -> None:
    module = _load_hooks_module()
    room_id = "!room:test"
    thread_id = "$threadA"
    _write_thread_todos(
        module, tmp_path, room_id, thread_id, [_todo("t1", "Task A", "worker")]
    )
    ctx = ScanContextStub(
        tmp_path,
        agents={"worker": object()},
        settings={
            "poke_cooldown_seconds": 0,
            "recent_response_grace_seconds": 0,
            "min_idle_before_poke_seconds": 0,
        },
    )
    _set_latest_message(
        module,
        ctx,
        room_id=room_id,
        thread_id=thread_id,
        agent_name="worker",
        status="streaming",
        age_minutes=2,
    )

    pokes = await module._run_poke_scan(ctx)

    assert pokes == 0
    assert ctx.sent_messages == []


@pytest.mark.asyncio
async def test_streaming_main_scope_blocks_poke(tmp_path: Path) -> None:
    module = _load_hooks_module()
    room_id = "!room:test"
    _write_thread_todos(module, tmp_path, room_id, None, [_todo("t-main", "Task A", "worker")])
    ctx = ScanContextStub(
        tmp_path,
        agents={"worker": object()},
        settings={
            "poke_cooldown_seconds": 0,
            "recent_response_grace_seconds": 0,
            "min_idle_before_poke_seconds": 0,
        },
    )
    _set_latest_message(
        module,
        ctx,
        room_id=room_id,
        thread_id=None,
        agent_name="worker",
        status="streaming",
        age_minutes=2,
    )

    pokes = await module._run_poke_scan(ctx)

    assert pokes == 0
    assert ctx.sent_messages == []


@pytest.mark.asyncio
async def test_latest_message_read_failure_fails_closed(tmp_path: Path) -> None:
    module = _load_hooks_module()
    room_id = "!room:test"
    thread_id = "$threadA"
    _write_thread_todos(
        module, tmp_path, room_id, thread_id, [_todo("t-read-fail", "Task A", "worker")]
    )
    ctx = ScanContextStub(
        tmp_path,
        agents={"worker": object()},
        settings={
            "poke_cooldown_seconds": 0,
            "recent_response_grace_seconds": 0,
            "min_idle_before_poke_seconds": 0,
        },
    )
    ctx.read_latest_thread_message = AsyncMock(side_effect=RuntimeError("boom"))

    pokes = await module._run_poke_scan(ctx)

    assert pokes == 0
    assert ctx.sent_messages == []


@pytest.mark.asyncio
async def test_room_scope_reader_uses_real_cache_accessor(tmp_path: Path) -> None:
    module = _load_hooks_module()
    room_id = "!room:test"
    cache = _EventCache(tmp_path / "event_cache.db")
    await cache.initialize()
    try:
        await cache.store_events_batch(
            [
                (
                    "$room-message",
                    room_id,
                    {
                        "event_id": "$room-message",
                        "sender": "@worker:test",
                        "origin_server_ts": 2000,
                        "type": "m.room.message",
                        "content": {
                            "body": "Room timeline reply",
                            "msgtype": "m.text",
                            module.poke.STREAM_STATUS_KEY: "streaming",
                        },
                    },
                ),
            ],
        )
    finally:
        await cache.close()

    ctx = ScanContextStub(
        tmp_path,
        agents={"worker": object()},
        settings={},
        runtime_started_at=0.0,
    )
    ctx.read_latest_thread_message = None

    snapshot = await module._read_latest_thread_message(
        ctx,
        room_id,
        None,
        "@worker:test",
    )

    assert snapshot is not None
    assert snapshot.content[module.poke.STREAM_STATUS_KEY] == "streaming"


@pytest.mark.asyncio
async def test_workloop_treats_cache_unavailable_as_busy(tmp_path: Path) -> None:
    module = _load_hooks_module()
    ctx = ScanContextStub(
        tmp_path,
        agents={"worker": object()},
        settings={},
        runtime_started_at=0.0,
    )
    ctx.read_latest_thread_message = None

    with patch.object(
        module.poke,
        "get_latest_agent_message_snapshot",
        side_effect=CacheUnavailable("cache unavailable"),
    ):
        should_poke = await module._should_poke_agent(
            ctx,
            "worker",
            "!room:test",
            "$threadA",
            datetime.now(UTC),
            0,
            0,
        )

    assert should_poke is False


@pytest.mark.asyncio
async def test_threaded_reader_uses_runtime_started_at_for_busy_gate(
    tmp_path: Path,
) -> None:
    module = _load_hooks_module()
    room_id = "!room:test"
    thread_id = "$threadA"
    cache = _EventCache(tmp_path / "event_cache.db")
    await cache.initialize()
    try:
        await cache.replace_thread(
            room_id,
            thread_id,
            [
                {
                    "event_id": thread_id,
                    "sender": "@user:test",
                    "origin_server_ts": 1000,
                    "type": "m.room.message",
                    "content": {"body": "Question", "msgtype": "m.text"},
                },
                {
                    "event_id": "$reply",
                    "sender": "@worker:test",
                    "origin_server_ts": 2000,
                    "type": "m.room.message",
                    "content": {
                        "body": "Previous runtime reply",
                        "msgtype": "m.text",
                        "m.relates_to": {"rel_type": "m.thread", "event_id": thread_id},
                    },
                },
            ],
            validated_at=1000.0,
        )
    finally:
        await cache.close()

    ctx = ScanContextStub(
        tmp_path,
        agents={"worker": object()},
        settings={},
        runtime_started_at=1001.0,
    )
    ctx.read_latest_thread_message = None

    should_poke = await module._should_poke_agent(
        ctx,
        "worker",
        room_id,
        thread_id,
        datetime.now(UTC),
        0,
        0,
    )

    assert should_poke is False


@pytest.mark.asyncio
async def test_completed_thread_pokeable_after_min_idle(tmp_path: Path) -> None:
    module = _load_hooks_module()
    room_id = "!room:test"
    thread_id = "$threadA"
    _write_thread_todos(
        module, tmp_path, room_id, thread_id, [_todo("t2", "Task A", "worker")]
    )
    ctx = ScanContextStub(
        tmp_path,
        agents={"worker": object()},
        settings={
            "poke_cooldown_seconds": 0,
            "recent_response_grace_seconds": 0,
            "min_idle_before_poke_seconds": 600,
        },
    )
    _set_latest_message(
        module,
        ctx,
        room_id=room_id,
        thread_id=thread_id,
        agent_name="worker",
        status="completed",
        age_minutes=15,
    )
    module.state.update_agent_state(
        tmp_path,
        "worker",
        {"last_response_at": (datetime.now(UTC) - timedelta(minutes=15)).isoformat()},
    )

    pokes = await module._run_poke_scan(ctx)

    assert pokes == 1
    assert len(ctx.sent_messages) == 1
    assert ctx.sent_messages[0]["thread_id"] == thread_id
    assert ctx.sent_messages[0]["text"].startswith("@worker workloop resume.")


@pytest.mark.asyncio
async def test_stuck_streaming_eventually_pokes(tmp_path: Path) -> None:
    module = _load_hooks_module()
    room_id = "!room:test"
    thread_id = "$threadA"
    _write_thread_todos(
        module, tmp_path, room_id, thread_id, [_todo("t3", "Task A", "worker")]
    )
    ctx = ScanContextStub(
        tmp_path,
        agents={"worker": object()},
        settings={
            "poke_cooldown_seconds": 0,
            "recent_response_grace_seconds": 0,
            "min_idle_before_poke_seconds": 600,
        },
    )
    _set_latest_message(
        module,
        ctx,
        room_id=room_id,
        thread_id=thread_id,
        agent_name="worker",
        status="streaming",
        age_minutes=35,
    )

    module.state.update_agent_state(
        tmp_path,
        "worker",
        {"last_response_at": (datetime.now(UTC) - timedelta(minutes=5)).isoformat()},
    )
    first_pokes = await module._run_poke_scan(ctx)

    module.state.update_agent_state(
        tmp_path,
        "worker",
        {"last_response_at": (datetime.now(UTC) - timedelta(minutes=40)).isoformat()},
    )
    second_pokes = await module._run_poke_scan(ctx)

    assert first_pokes == 0
    assert second_pokes == 1
    assert len(ctx.sent_messages) == 1
    assert ctx.sent_messages[0]["thread_id"] == thread_id


@pytest.mark.asyncio
async def test_no_prior_message_falls_through_to_other_gates(tmp_path: Path) -> None:
    module = _load_hooks_module()
    room_id = "!room:test"
    thread_id = "$threadA"
    _write_thread_todos(
        module, tmp_path, room_id, thread_id, [_todo("t4", "Task A", "worker")]
    )
    ctx = ScanContextStub(
        tmp_path,
        agents={"worker": object()},
        settings={
            "poke_cooldown_seconds": 0,
            "recent_response_grace_seconds": 0,
            "min_idle_before_poke_seconds": 600,
        },
    )

    module.state.update_agent_state(
        tmp_path,
        "worker",
        {"last_response_at": (datetime.now(UTC) - timedelta(minutes=5)).isoformat()},
    )
    first_pokes = await module._run_poke_scan(ctx)

    module.state.update_agent_state(
        tmp_path,
        "worker",
        {"last_response_at": (datetime.now(UTC) - timedelta(minutes=20)).isoformat()},
    )
    second_pokes = await module._run_poke_scan(ctx)

    assert first_pokes == 0
    assert second_pokes == 1
    assert len(ctx.sent_messages) == 1
    assert ctx.sent_messages[0]["thread_id"] == thread_id


@pytest.mark.asyncio
async def test_cross_thread_isolation_unchanged(tmp_path: Path) -> None:
    module = _load_hooks_module()
    room_id = "!room:test"
    thread_a = "$threadA"
    thread_b = "$threadB"
    _write_thread_todos(
        module, tmp_path, room_id, thread_a, [_todo("a1", "Task A", "worker")]
    )
    _write_thread_todos(
        module, tmp_path, room_id, thread_b, [_todo("b1", "Task B", "worker")]
    )
    ctx = ScanContextStub(
        tmp_path,
        agents={"worker": object()},
        settings={
            "poke_cooldown_seconds": 0,
            "recent_response_grace_seconds": 0,
            "min_idle_before_poke_seconds": 0,
        },
    )
    _set_latest_message(
        module,
        ctx,
        room_id=room_id,
        thread_id=thread_a,
        agent_name="worker",
        status="streaming",
        age_minutes=2,
    )

    pokes = await module._run_poke_scan(ctx)

    assert pokes == 1
    assert len(ctx.sent_messages) == 1
    assert ctx.sent_messages[0]["thread_id"] == thread_b
    assert ctx.sent_messages[0]["text"].startswith("@worker workloop resume.")


def test_locked_update_json_uses_real_process_wide_fcntl_lock(tmp_path: Path) -> None:
    module = _load_hooks_module()
    if "fork" not in multiprocessing.get_all_start_methods():
        pytest.skip("fork start method unavailable")
    path = module.state.agent_state_path(tmp_path, "worker")
    state_module_path = Path(__file__).resolve().parents[1] / "state.py"
    ctx = multiprocessing.get_context("fork")
    first = ctx.Process(
        target=_increment_locked_counter, args=(path, state_module_path, 50)
    )
    second = ctx.Process(
        target=_increment_locked_counter, args=(path, state_module_path, 50)
    )

    first.start()
    second.start()
    first.join(10)
    second.join(10)
    if first.is_alive():
        first.terminate()
        first.join(5)
    if second.is_alive():
        second.terminate()
        second.join(5)

    assert first.exitcode == 0
    assert second.exitcode == 0
    assert module.state.read_json(path)["counter"] == 100


@pytest.mark.asyncio
async def test_inject_todos_still_enriches_without_touching_active_runs(
    tmp_path: Path,
) -> None:
    module = _load_hooks_module()
    room_id = "!room:test"
    resolved_thread_id = "$resolved-thread"
    envelope = _make_envelope(
        room_id=room_id,
        source_thread_id="$source-thread",
        resolved_thread_id=resolved_thread_id,
    )
    _write_thread_todos(
        module,
        tmp_path,
        room_id,
        resolved_thread_id,
        [_todo("t5", "Resume after restart", "worker")],
    )

    enrichments = await module.inject_todos(
        SimpleNamespace(
            target_entity_name="worker",
            envelope=envelope,
            settings={"max_items_in_enrichment": 10},
            state_root=tmp_path,
        )
    )

    state = module.state.read_agent_state(tmp_path, "worker")
    assert state.get("active_runs", {}) == {}
    assert len(enrichments) == 1
    assert "Thread work plan:" in enrichments[0].text


@pytest.mark.asyncio
async def test_track_idle_still_writes_last_response_at(tmp_path: Path) -> None:
    module = _load_hooks_module()
    room_id = "!room:test"
    resolved_thread_id = "$resolved-thread"
    envelope = _make_envelope(
        room_id=room_id,
        source_thread_id="$source-thread",
        resolved_thread_id=resolved_thread_id,
    )

    await module.track_idle(
        SimpleNamespace(
            state_root=tmp_path,
            result=SimpleNamespace(envelope=envelope),
        )
    )

    state = module.state.read_agent_state(tmp_path, "worker")
    assert state["last_response_at"] is not None


def test_track_cancelled_removed_or_inert(tmp_path: Path) -> None:
    module = _load_hooks_module()
    assert not hasattr(module, "track_cancelled")
