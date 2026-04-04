from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest


def _load_hooks_module():
    hooks_path = Path(__file__).resolve().parents[1] / "hooks.py"
    module_name = f"workloop_hooks_test_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, hooks_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


@dataclass
class LifecycleContextStub:
    settings: dict[str, Any]
    config: Any
    _state_root: Path
    logger: Any
    send_message: AsyncMock
    message_sender: AsyncMock | None
    entity_name: str
    room_state_querier: AsyncMock | None = None

    @property
    def state_root(self) -> Path:
        return self._state_root


@dataclass
class ScheduleContextStub:
    message_text: str
    suppress: bool = False


@dataclass
class EnvelopeStub:
    body: str
    room_id: str = "!room:test"
    thread_id: str | None = None
    resolved_thread_id: str = "$reply"


@dataclass
class MessageContextStub:
    envelope: EnvelopeStub
    settings: dict[str, Any]
    config: Any
    _state_root: Path
    send_message: AsyncMock
    suppress: bool = False
    query_room_state: AsyncMock | None = None

    @property
    def state_root(self) -> Path:
        return self._state_root


def _make_config(*, agents: dict[str, Any] | None = None) -> Any:
    return SimpleNamespace(agents=agents or {})


def _make_lifecycle_context(
    tmp_path: Path,
    *,
    entity_name: str,
    settings: dict[str, Any] | None = None,
    agents: dict[str, Any] | None = None,
) -> LifecycleContextStub:
    return LifecycleContextStub(
        settings=settings or {},
        config=_make_config(agents=agents),
        _state_root=tmp_path,
        logger=Mock(),
        send_message=AsyncMock(return_value="$event"),
        message_sender=AsyncMock(return_value="$event"),
        entity_name=entity_name,
    )


def _make_runtime(
    module,
    tmp_path: Path,
    *,
    settings: dict[str, Any] | None = None,
    message_sender: AsyncMock | None = None,
    room_state_querier: AsyncMock | None = None,
):
    return module.AutoPokeRuntime(
        settings=settings or {"poke_interval_seconds": 1},
        config=_make_config(agents={"worker": object()}),
        state_root=tmp_path,
        logger=Mock(),
        _message_sender=message_sender or AsyncMock(return_value="$event"),
        _room_state_querier=room_state_querier,
    )


@pytest.mark.asyncio
async def test_router_start_creates_exactly_one_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_hooks_module()
    created_tasks: list[asyncio.Task[None]] = []
    real_create_task = asyncio.create_task
    stop_event = asyncio.Event()

    async def fake_loop(_runtime) -> None:
        await stop_event.wait()

    def record_create_task(coro, *, name=None):
        task = real_create_task(coro, name=name)
        created_tasks.append(task)
        return task

    monkeypatch.setattr(module, "_auto_poke_loop", fake_loop)
    monkeypatch.setattr(module.asyncio, "create_task", record_create_task)

    ctx = _make_lifecycle_context(
        tmp_path,
        entity_name=module.ROUTER_AGENT_NAME,
        agents={module.ROUTER_AGENT_NAME: object()},
    )

    await module.start_auto_poke_loop(ctx)

    assert len(created_tasks) == 1
    assert module._AUTO_POKE_TASK is created_tasks[0]

    await module.stop_auto_poke_loop(ctx)


@pytest.mark.asyncio
async def test_non_router_start_is_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_hooks_module()
    create_task = Mock(side_effect=AssertionError("create_task should not be called"))
    monkeypatch.setattr(module.asyncio, "create_task", create_task)

    ctx = _make_lifecycle_context(
        tmp_path, entity_name="worker", agents={"worker": object()}
    )

    await module.start_auto_poke_loop(ctx)

    assert module._AUTO_POKE_TASK is None
    create_task.assert_not_called()


@pytest.mark.asyncio
async def test_second_router_start_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_hooks_module()
    created_tasks: list[asyncio.Task[None]] = []
    real_create_task = asyncio.create_task
    stop_event = asyncio.Event()

    async def fake_loop(_runtime) -> None:
        await stop_event.wait()

    def record_create_task(coro, *, name=None):
        task = real_create_task(coro, name=name)
        created_tasks.append(task)
        return task

    monkeypatch.setattr(module, "_auto_poke_loop", fake_loop)
    monkeypatch.setattr(module.asyncio, "create_task", record_create_task)

    ctx = _make_lifecycle_context(
        tmp_path,
        entity_name=module.ROUTER_AGENT_NAME,
        agents={module.ROUTER_AGENT_NAME: object()},
    )

    await module.start_auto_poke_loop(ctx)
    await module.start_auto_poke_loop(ctx)

    assert len(created_tasks) == 1

    await module.stop_auto_poke_loop(ctx)


@pytest.mark.asyncio
async def test_router_stop_cancels_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_hooks_module()
    stop_event = asyncio.Event()

    async def fake_loop(_runtime) -> None:
        await stop_event.wait()

    monkeypatch.setattr(module, "_auto_poke_loop", fake_loop)

    ctx = _make_lifecycle_context(
        tmp_path,
        entity_name=module.ROUTER_AGENT_NAME,
        agents={module.ROUTER_AGENT_NAME: object()},
    )

    await module.start_auto_poke_loop(ctx)
    task = module._AUTO_POKE_TASK

    assert task is not None

    await module.stop_auto_poke_loop(ctx)

    assert task.cancelled()
    assert module._AUTO_POKE_TASK is None


@pytest.mark.asyncio
async def test_router_stop_without_start_is_noop(tmp_path: Path) -> None:
    module = _load_hooks_module()
    ctx = _make_lifecycle_context(
        tmp_path,
        entity_name=module.ROUTER_AGENT_NAME,
        agents={module.ROUTER_AGENT_NAME: object()},
    )

    await module.stop_auto_poke_loop(ctx)

    assert module._AUTO_POKE_TASK is None


@pytest.mark.asyncio
async def test_router_stop_does_not_clear_newer_task(tmp_path: Path) -> None:
    module = _load_hooks_module()

    class AwaitableTaskStub:
        def __init__(self, on_await) -> None:
            self.cancel_called = False
            self._on_await = on_await

        def cancel(self) -> None:
            self.cancel_called = True

        def __await__(self):
            async def _wait():
                await asyncio.sleep(0)
                self._on_await()

            return _wait().__await__()

    replacement = asyncio.create_task(asyncio.sleep(3600))
    task = AwaitableTaskStub(lambda: setattr(module, "_AUTO_POKE_TASK", replacement))
    module._AUTO_POKE_TASK = task
    ctx = _make_lifecycle_context(
        tmp_path,
        entity_name=module.ROUTER_AGENT_NAME,
        agents={module.ROUTER_AGENT_NAME: object()},
    )

    try:
        await module.stop_auto_poke_loop(ctx)

        assert task.cancel_called is True
        assert module._AUTO_POKE_TASK is replacement
    finally:
        replacement.cancel()
        with pytest.raises(asyncio.CancelledError):
            await replacement


@pytest.mark.asyncio
async def test_auto_poke_loop_calls_scan_on_interval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_hooks_module()
    runtime = _make_runtime(module, tmp_path, settings={"poke_interval_seconds": 7})
    real_sleep = asyncio.sleep
    sleep_calls: list[int] = []
    scan_calls: list[Any] = []

    async def fake_sleep(seconds: int) -> None:
        sleep_calls.append(seconds)
        await real_sleep(0)

    async def fake_scan(ctx) -> int:
        scan_calls.append(ctx)
        current_task = asyncio.current_task()
        assert current_task is not None
        current_task.cancel()
        return 1

    monkeypatch.setattr(module.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(module, "_run_poke_scan", fake_scan)

    with pytest.raises(asyncio.CancelledError):
        await module._auto_poke_loop(runtime)

    assert sleep_calls[0] == 7
    assert scan_calls == [runtime]


@pytest.mark.asyncio
async def test_auto_poke_loop_invalid_interval_uses_default_and_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_hooks_module()
    runtime = _make_runtime(
        module, tmp_path, settings={"poke_interval_seconds": "oops"}
    )
    real_sleep = asyncio.sleep
    sleep_calls: list[int] = []

    async def fake_sleep(seconds: int) -> None:
        sleep_calls.append(seconds)
        await real_sleep(0)

    async def fake_scan(_ctx) -> int:
        current_task = asyncio.current_task()
        assert current_task is not None
        current_task.cancel()
        await real_sleep(0)
        return 0

    monkeypatch.setattr(module.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(module, "_run_poke_scan", fake_scan)

    with pytest.raises(asyncio.CancelledError):
        await module._auto_poke_loop(runtime)

    assert sleep_calls[0] == module.DEFAULT_POKE_INTERVAL_SECONDS
    runtime.logger.warning.assert_called_once_with(
        "workloop-auto-poke: invalid poke_interval_seconds=%r; using default %s",
        "oops",
        module.DEFAULT_POKE_INTERVAL_SECONDS,
    )


@pytest.mark.asyncio
async def test_auto_poke_loop_logs_exceptions_and_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_hooks_module()
    runtime = _make_runtime(module, tmp_path)
    real_sleep = asyncio.sleep
    scan_attempts: list[int] = []

    async def fake_sleep(_seconds: int) -> None:
        await real_sleep(0)

    async def fake_scan(_ctx) -> int:
        scan_attempts.append(len(scan_attempts) + 1)
        if len(scan_attempts) == 1:
            raise RuntimeError("boom")
        current_task = asyncio.current_task()
        assert current_task is not None
        current_task.cancel()
        return 0

    monkeypatch.setattr(module.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(module, "_run_poke_scan", fake_scan)

    with pytest.raises(asyncio.CancelledError):
        await module._auto_poke_loop(runtime)

    assert scan_attempts == [1, 2]
    runtime.logger.exception.assert_called_once()


@pytest.mark.asyncio
async def test_legacy_schedule_is_suppressed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_hooks_module()
    run_scan = AsyncMock(
        side_effect=AssertionError("legacy schedule should not run scans")
    )
    warning = Mock()
    monkeypatch.setattr(module, "_run_poke_scan", run_scan)
    monkeypatch.setattr(module.logger, "warning", warning)

    ctx = ScheduleContextStub(message_text="!workloop-tick")

    await module.auto_poke(ctx)

    assert ctx.suppress is True
    run_scan.assert_not_called()
    warning.assert_called_once()


@pytest.mark.asyncio
async def test_manual_workloop_tick_still_runs_one_shot_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_hooks_module()
    run_scan = AsyncMock(return_value=2)
    monkeypatch.setattr(module, "_run_poke_scan", run_scan)
    send_message = AsyncMock(return_value="$reply")
    ctx = MessageContextStub(
        envelope=EnvelopeStub(body="!workloop-tick"),
        settings={},
        config=_make_config(agents={"worker": object()}),
        _state_root=tmp_path,
        send_message=send_message,
    )

    await module.workloop_command(ctx)

    assert ctx.suppress is True
    run_scan.assert_awaited_once_with(ctx)
    send_message.assert_awaited_once_with(
        "!room:test", "🔄 Workloop tick: 2 poke(s) sent.", thread_id=None
    )


@pytest.mark.asyncio
async def test_auto_poke_messages_use_background_hook_source(tmp_path: Path) -> None:
    module = _load_hooks_module()
    message_sender = AsyncMock(return_value="$event")
    runtime = _make_runtime(module, tmp_path, message_sender=message_sender)
    thread_dir = tmp_path / "threads" / "room_thread"
    thread_dir.mkdir(parents=True)
    (thread_dir / "todos.json").write_text(
        json.dumps(
            {
                "room_id": "!room:test",
                "thread_id": "$thread",
                "items": [
                    {
                        "id": "todo1",
                        "title": "Ship fix",
                        "status": "open",
                        "priority": "high",
                        "assigned_agent": "worker",
                        "depends_on": [],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    pokes = await module._run_poke_scan(runtime)

    assert pokes == 1
    args = message_sender.await_args.args
    assert args[0] == "!room:test"
    assert args[1].startswith("@worker workloop resume.")
    assert args[2] == "$thread"
    assert args[3] == module._AUTO_POKE_HOOK_SOURCE
    assert args[4] is None


def _write_thread_todos(
    tmp_path: Path,
    dir_name: str,
    room_id: str,
    thread_id: str,
    items: list[dict[str, Any]],
) -> None:
    thread_dir = tmp_path / "threads" / dir_name
    thread_dir.mkdir(parents=True, exist_ok=True)
    (thread_dir / "todos.json").write_text(
        json.dumps({"room_id": room_id, "thread_id": thread_id, "items": items}),
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_poke_scan_fires_in_multiple_threads_for_same_agent(
    tmp_path: Path,
) -> None:
    """An agent with actionable items in two different threads gets poked in both."""
    module = _load_hooks_module()
    message_sender = AsyncMock(return_value="$event")
    runtime = _make_runtime(
        module,
        tmp_path,
        settings={"poke_interval_seconds": 1, "poke_cooldown_seconds": 300},
        message_sender=message_sender,
    )

    item_a = {
        "id": "aa11",
        "title": "Task A",
        "status": "open",
        "priority": "medium",
        "assigned_agent": "worker",
        "depends_on": [],
    }
    item_b = {
        "id": "bb22",
        "title": "Task B",
        "status": "open",
        "priority": "medium",
        "assigned_agent": "worker",
        "depends_on": [],
    }

    _write_thread_todos(tmp_path, "room_threadA", "!room:test", "$threadA", [item_a])
    _write_thread_todos(tmp_path, "room_threadB", "!room:test", "$threadB", [item_b])

    pokes = await module._run_poke_scan(runtime)

    assert pokes == 2
    thread_ids = sorted(call.args[2] for call in message_sender.await_args_list)
    assert thread_ids == ["$threadA", "$threadB"]


@pytest.mark.asyncio
async def test_poke_cooldown_is_per_scope(tmp_path: Path) -> None:
    """After poking in one thread, the same thread is on cooldown but a different thread is not."""
    module = _load_hooks_module()
    message_sender = AsyncMock(return_value="$event")
    runtime = _make_runtime(
        module,
        tmp_path,
        settings={"poke_interval_seconds": 1, "poke_cooldown_seconds": 300},
        message_sender=message_sender,
    )

    item = {
        "id": "cc33",
        "title": "Task C",
        "status": "open",
        "priority": "medium",
        "assigned_agent": "worker",
        "depends_on": [],
    }

    _write_thread_todos(tmp_path, "room_threadA", "!room:test", "$threadA", [item])
    _write_thread_todos(tmp_path, "room_threadB", "!room:test", "$threadB", [item])

    # First scan pokes both threads
    pokes1 = await module._run_poke_scan(runtime)
    assert pokes1 == 2

    # Second scan — both on cooldown, zero pokes
    pokes2 = await module._run_poke_scan(runtime)
    assert pokes2 == 0

    # Add a third thread — only the new thread should be poked
    _write_thread_todos(tmp_path, "room_threadC", "!room:test", "$threadC", [item])
    pokes3 = await module._run_poke_scan(runtime)
    assert pokes3 == 1
    last_call_thread = message_sender.await_args_list[-1].args[2]
    assert last_call_thread == "$threadC"


# -- _has_pending_schedules tests --


@pytest.mark.asyncio
async def test_has_pending_schedules_returns_true_when_matching(tmp_path: Path) -> None:
    module = _load_hooks_module()

    async def fake_querier(room_id, event_type, state_key):
        return {
            "task-1": {
                "status": "pending",
                "workflow": json.dumps(
                    {"thread_id": "$threadA", "room_id": "!room:test"}
                ),
            },
        }

    runtime = _make_runtime(
        module, tmp_path, room_state_querier=AsyncMock(side_effect=fake_querier)
    )
    result = await module._has_pending_schedules(runtime, "!room:test", "$threadA")
    assert result is True


@pytest.mark.asyncio
async def test_has_pending_schedules_returns_false_when_no_pending(
    tmp_path: Path,
) -> None:
    module = _load_hooks_module()

    async def fake_querier(room_id, event_type, state_key):
        return {
            "task-1": {
                "status": "done",
                "workflow": json.dumps(
                    {"thread_id": "$threadA", "room_id": "!room:test"}
                ),
            },
        }

    runtime = _make_runtime(
        module, tmp_path, room_state_querier=AsyncMock(side_effect=fake_querier)
    )
    result = await module._has_pending_schedules(runtime, "!room:test", "$threadA")
    assert result is False


@pytest.mark.asyncio
async def test_has_pending_schedules_returns_false_when_querier_none(
    tmp_path: Path,
) -> None:
    module = _load_hooks_module()
    runtime = _make_runtime(module, tmp_path, room_state_querier=None)
    result = await module._has_pending_schedules(runtime, "!room:test", "$threadA")
    assert result is False


@pytest.mark.asyncio
async def test_has_pending_schedules_thread_mismatch(tmp_path: Path) -> None:
    module = _load_hooks_module()

    async def fake_querier(room_id, event_type, state_key):
        return {
            "task-1": {
                "status": "pending",
                "workflow": json.dumps(
                    {"thread_id": "$otherThread", "room_id": "!room:test"}
                ),
            },
        }

    runtime = _make_runtime(
        module, tmp_path, room_state_querier=AsyncMock(side_effect=fake_querier)
    )
    result = await module._has_pending_schedules(runtime, "!room:test", "$threadA")
    assert result is False


@pytest.mark.asyncio
async def test_has_pending_schedules_room_level(tmp_path: Path) -> None:
    """Pending task with thread_id=None matches a room-level poke (thread_id=None)."""
    module = _load_hooks_module()

    async def fake_querier(room_id, event_type, state_key):
        return {
            "task-1": {
                "status": "pending",
                "workflow": json.dumps({"thread_id": None, "room_id": "!room:test"}),
            },
        }

    runtime = _make_runtime(
        module, tmp_path, room_state_querier=AsyncMock(side_effect=fake_querier)
    )
    result = await module._has_pending_schedules(runtime, "!room:test", None)
    assert result is True


@pytest.mark.asyncio
async def test_has_pending_schedules_ignores_malformed_workflow(tmp_path: Path) -> None:
    module = _load_hooks_module()

    async def fake_querier(room_id, event_type, state_key):
        return {
            "task-1": {
                "status": "pending",
                "workflow": "{not-json",
            },
        }

    runtime = _make_runtime(
        module, tmp_path, room_state_querier=AsyncMock(side_effect=fake_querier)
    )
    result = await module._has_pending_schedules(runtime, "!room:test", "$threadA")
    assert result is False


# -- _should_poke_agent min_idle tests --


def test_should_poke_agent_respects_min_idle(tmp_path: Path) -> None:
    module = _load_hooks_module()
    now = datetime(2026, 3, 31, 12, 0, 0, tzinfo=UTC)

    # Agent responded 5 minutes ago, min_idle is 10 minutes → should NOT poke
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / "worker.json").write_text(
        json.dumps({"last_response_at": "2026-03-31T11:55:00+00:00"}),
        encoding="utf-8",
    )

    result = module._should_poke_agent(
        tmp_path, "worker", now, cooldown=300, grace=30, stale_busy=600, min_idle=600
    )
    assert result is False


def test_should_poke_agent_min_idle_expired(tmp_path: Path) -> None:
    module = _load_hooks_module()
    now = datetime(2026, 3, 31, 12, 0, 0, tzinfo=UTC)

    # Agent responded 15 minutes ago, min_idle is 10 minutes → should poke
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / "worker.json").write_text(
        json.dumps({"last_response_at": "2026-03-31T11:45:00+00:00"}),
        encoding="utf-8",
    )

    result = module._should_poke_agent(
        tmp_path, "worker", now, cooldown=300, grace=30, stale_busy=600, min_idle=600
    )
    assert result is True


def test_should_poke_agent_min_idle_zero_skips_check(tmp_path: Path) -> None:
    module = _load_hooks_module()
    now = datetime(2026, 3, 31, 12, 0, 0, tzinfo=UTC)

    # Agent responded 1 minute ago, min_idle=0 (disabled) → should poke (grace=30s already passed)
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / "worker.json").write_text(
        json.dumps({"last_response_at": "2026-03-31T11:59:00+00:00"}),
        encoding="utf-8",
    )

    result = module._should_poke_agent(
        tmp_path, "worker", now, cooldown=300, grace=30, stale_busy=600, min_idle=0
    )
    assert result is True


# -- _run_poke_scan skips threads with pending schedules --


@pytest.mark.asyncio
async def test_poke_scan_skips_threads_with_pending_schedules(tmp_path: Path) -> None:
    module = _load_hooks_module()
    message_sender = AsyncMock(return_value="$event")

    async def fake_querier(room_id, event_type, state_key):
        return {
            "task-1": {
                "status": "pending",
                "workflow": json.dumps(
                    {"thread_id": "$threadA", "room_id": "!room:test"}
                ),
            },
        }

    runtime = _make_runtime(
        module,
        tmp_path,
        settings={"poke_interval_seconds": 1, "min_idle_before_poke_seconds": 0},
        message_sender=message_sender,
        room_state_querier=AsyncMock(side_effect=fake_querier),
    )

    item = {
        "id": "dd44",
        "title": "Task D",
        "status": "open",
        "priority": "medium",
        "assigned_agent": "worker",
        "depends_on": [],
    }

    _write_thread_todos(tmp_path, "room_threadA", "!room:test", "$threadA", [item])
    _write_thread_todos(tmp_path, "room_threadB", "!room:test", "$threadB", [item])

    pokes = await module._run_poke_scan(runtime)

    # Only threadB should be poked; threadA has a pending schedule
    assert pokes == 1
    assert message_sender.await_args.args[2] == "$threadB"
