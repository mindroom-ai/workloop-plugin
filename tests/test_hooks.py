from __future__ import annotations

import asyncio
import importlib.util
import sys
import uuid
from dataclasses import dataclass
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
    entity_name: str

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
        entity_name=entity_name,
    )


def _make_runtime(module, tmp_path: Path, *, settings: dict[str, Any] | None = None):
    return module.AutoPokeRuntime(
        settings=settings or {"poke_interval_seconds": 1},
        config=_make_config(agents={"worker": object()}),
        state_root=tmp_path,
        logger=Mock(),
        _message_sender=AsyncMock(return_value="$event"),
    )


@pytest.mark.asyncio
async def test_router_start_creates_exactly_one_task(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
async def test_non_router_start_is_noop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_hooks_module()
    create_task = Mock(side_effect=AssertionError("create_task should not be called"))
    monkeypatch.setattr(module.asyncio, "create_task", create_task)

    ctx = _make_lifecycle_context(tmp_path, entity_name="worker", agents={"worker": object()})

    await module.start_auto_poke_loop(ctx)

    assert module._AUTO_POKE_TASK is None
    create_task.assert_not_called()


@pytest.mark.asyncio
async def test_second_router_start_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
async def test_router_stop_cancels_task(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
async def test_auto_poke_loop_calls_scan_on_interval(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
async def test_legacy_schedule_is_suppressed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_hooks_module()
    run_scan = AsyncMock(side_effect=AssertionError("legacy schedule should not run scans"))
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
    send_message.assert_awaited_once_with("!room:test", "🔄 Workloop tick: 2 poke(s) sent.", thread_id=None)
