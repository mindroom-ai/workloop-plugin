"""Startup-safe deferral of scheduled fires.

Covers the `schedule:fired` gate and the deferred-fire waiter: fires that
land while sandbox workers are still starting are suppressed, journaled, and
re-posted exactly once after readiness (or at the cap), without blocking the
hook pipeline and without real sleeps in any test.
"""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from importlib import util
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

PACKAGE_NAME = (
    f"mindroom_plugin_{Path(__file__).resolve().parents[1].name.replace('-', '_')}"
)


def _load_hooks_module():
    for suffix in ("hooks", "deferral"):
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


@dataclass
class ScheduleFiredContextStub:
    task_id: str
    message_text: str
    _state_root: Path
    room_id: str = "!room:test"
    thread_id: str | None = "$thread"
    settings: dict[str, Any] = field(default_factory=dict)
    config: Any = None
    runtime_paths: Any = None
    logger: Mock = field(default_factory=Mock)
    suppress: bool = False
    send_message: AsyncMock = field(default_factory=lambda: AsyncMock(return_value="$event"))

    @property
    def state_root(self) -> Path:
        return self._state_root


@dataclass
class WaiterRuntimeStub:
    _state_root: Path
    config: Any = None
    runtime_paths: Any = None
    logger: Mock = field(default_factory=Mock)
    send_message: AsyncMock = field(default_factory=lambda: AsyncMock(return_value="$event"))

    @property
    def state_root(self) -> Path:
        return self._state_root


def _make_ctx(tmp_path: Path, task_id: str = "task-1", **kwargs: Any) -> ScheduleFiredContextStub:
    return ScheduleFiredContextStub(
        task_id=task_id,
        message_text=f"scheduled message for {task_id}",
        _state_root=tmp_path,
        **kwargs,
    )


def _force_window(monkeypatch: pytest.MonkeyPatch, deferral: Any, *, inside: bool) -> None:
    offset = 0.0 if inside else 1_000_000.0
    monkeypatch.setattr(
        deferral, "_monotonic", lambda: deferral._MODULE_LOADED_AT_MONOTONIC + offset
    )


def _set_readiness(
    monkeypatch: pytest.MonkeyPatch, deferral: Any, results: list[bool] | bool
) -> list[int]:
    """Patch the readiness probe; the final list element repeats forever."""
    calls: list[int] = []
    sequence = [results] if isinstance(results, bool) else list(results)

    async def fake_ready(_runtime_paths: Any, _config: Any, _log: Any) -> bool:
        calls.append(len(calls))
        index = min(len(calls) - 1, len(sequence) - 1)
        return sequence[index]

    monkeypatch.setattr(deferral, "workers_ready", fake_ready)
    return calls


def _instant_sleep(monkeypatch: pytest.MonkeyPatch, deferral: Any) -> list[float]:
    real_sleep = asyncio.sleep
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        await real_sleep(0)

    monkeypatch.setattr(deferral, "_sleep", fake_sleep)
    return sleeps


# --- gate behavior -----------------------------------------------------------


@pytest.mark.asyncio
async def test_fire_while_ready_posts_immediately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_hooks_module()
    deferral = module.deferral
    _force_window(monkeypatch, deferral, inside=True)
    probe_calls = _set_readiness(monkeypatch, deferral, True)
    monkeypatch.setattr(module, "_DEFERRED_FIRE_TASK", None)
    ctx = _make_ctx(tmp_path)

    await module.auto_poke(ctx)

    assert ctx.suppress is False
    assert probe_calls == [0]
    assert deferral.pending_fires(tmp_path) == []
    assert module._DEFERRED_FIRE_TASK is None


@pytest.mark.asyncio
async def test_fire_outside_startup_window_skips_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_hooks_module()
    deferral = module.deferral
    _force_window(monkeypatch, deferral, inside=False)
    probe_calls = _set_readiness(monkeypatch, deferral, False)
    monkeypatch.setattr(module, "_DEFERRED_FIRE_TASK", None)
    ctx = _make_ctx(tmp_path)

    await module.auto_poke(ctx)

    assert ctx.suppress is False
    assert probe_calls == []
    assert deferral.pending_fires(tmp_path) == []


@pytest.mark.asyncio
async def test_zero_cap_setting_disables_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_hooks_module()
    deferral = module.deferral
    _force_window(monkeypatch, deferral, inside=True)
    probe_calls = _set_readiness(monkeypatch, deferral, False)
    ctx = _make_ctx(tmp_path, settings={"worker_ready_cap_seconds": 0})

    deferred = await deferral.gate_scheduled_fire(ctx)

    assert deferred is False
    assert ctx.suppress is False
    assert probe_calls == []


@pytest.mark.asyncio
async def test_fire_while_unready_is_journaled_and_suppressed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_hooks_module()
    deferral = module.deferral
    _force_window(monkeypatch, deferral, inside=True)
    _set_readiness(monkeypatch, deferral, False)
    ctx = _make_ctx(tmp_path)

    deferred = await deferral.gate_scheduled_fire(ctx)

    assert deferred is True
    assert ctx.suppress is True
    entries = deferral.pending_fires(tmp_path)
    assert [entry["task_id"] for entry in entries] == ["task-1"]
    assert entries[0]["message_text"] == "scheduled message for task-1"
    assert entries[0]["room_id"] == "!room:test"
    assert entries[0]["thread_id"] == "$thread"
    fire_by = datetime.fromisoformat(entries[0]["fire_by"])
    enqueued_at = datetime.fromisoformat(entries[0]["enqueued_at"])
    assert fire_by - enqueued_at == timedelta(
        seconds=deferral.DEFAULT_WORKER_READY_CAP_SECONDS
    )


@pytest.mark.asyncio
async def test_repeat_fire_while_deferred_suppresses_without_duplicate_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_hooks_module()
    deferral = module.deferral
    _force_window(monkeypatch, deferral, inside=True)
    _set_readiness(monkeypatch, deferral, False)

    first = _make_ctx(tmp_path)
    second = _make_ctx(tmp_path)
    assert await deferral.gate_scheduled_fire(first) is True
    assert await deferral.gate_scheduled_fire(second) is True

    assert second.suppress is True
    assert [entry["task_id"] for entry in deferral.pending_fires(tmp_path)] == ["task-1"]


@pytest.mark.asyncio
async def test_live_fire_supersedes_journaled_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_hooks_module()
    deferral = module.deferral
    _force_window(monkeypatch, deferral, inside=True)
    _set_readiness(monkeypatch, deferral, [False, True])

    deferred_ctx = _make_ctx(tmp_path)
    assert await deferral.gate_scheduled_fire(deferred_ctx) is True
    assert deferral.has_pending_fires(tmp_path)

    live_ctx = _make_ctx(tmp_path)
    assert await deferral.gate_scheduled_fire(live_ctx) is False

    assert live_ctx.suppress is False
    assert deferral.pending_fires(tmp_path) == []


@pytest.mark.asyncio
async def test_probe_failure_fails_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_hooks_module()
    deferral = module.deferral

    def boom(_runtime_paths: Any, _config: Any) -> bool:
        raise RuntimeError("backend unavailable")

    monkeypatch.setattr(deferral, "_workers_ready_blocking", boom)
    log = Mock()

    assert await deferral.workers_ready(None, None, log) is True
    log.warning.assert_called_once()


@pytest.mark.asyncio
async def test_probe_reports_blocking_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_hooks_module()
    deferral = module.deferral
    monkeypatch.setattr(deferral, "_workers_ready_blocking", lambda _p, _c: False)

    assert await deferral.workers_ready(None, None, Mock()) is False


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        (["ready", "idle"], True),
        (["ready", "starting"], False),
        ([], True),
        (["failed"], True),
    ],
)
def test_blocking_probe_gates_only_on_starting_workers(
    monkeypatch: pytest.MonkeyPatch, statuses: list[str], expected: bool
) -> None:
    """Only workers still starting gate the fire; idle/failed ones do not."""
    module = _load_hooks_module()
    deferral = module.deferral
    import mindroom.tool_system.sandbox_proxy as sandbox_proxy
    import mindroom.workers.runtime as workers_runtime

    @dataclass
    class HandleStub:
        status: str

    class LeaseStub:
        def __enter__(self) -> Any:
            return Mock(list_workers=Mock(return_value=[HandleStub(s) for s in statuses]))

        def __exit__(self, *args: Any) -> bool:
            return False

    monkeypatch.setattr(workers_runtime, "primary_worker_backend_is_dedicated", lambda _p: True)
    monkeypatch.setattr(workers_runtime, "primary_worker_backend_available", lambda _p, **_k: True)
    monkeypatch.setattr(workers_runtime, "primary_worker_backend_name", lambda _p: "docker")
    monkeypatch.setattr(
        workers_runtime, "lease_primary_worker_manager", lambda *_a, **_k: LeaseStub()
    )
    monkeypatch.setattr(
        sandbox_proxy,
        "sandbox_proxy_config",
        lambda _p: Mock(proxy_url="http://proxy", proxy_token="token"),
    )
    runtime_paths = Mock(storage_root=Path("/tmp/storage"))
    config = Mock(get_worker_grantable_credentials=Mock(return_value=frozenset()))

    assert deferral._workers_ready_blocking(runtime_paths, config) is expected


def test_blocking_probe_skips_non_dedicated_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_hooks_module()
    deferral = module.deferral
    import mindroom.workers.runtime as workers_runtime

    monkeypatch.setattr(
        workers_runtime, "primary_worker_backend_is_dedicated", lambda _p: False
    )
    lease = Mock(side_effect=AssertionError("must not lease a manager"))
    monkeypatch.setattr(workers_runtime, "lease_primary_worker_manager", lease)

    assert deferral._workers_ready_blocking(Mock(), Mock()) is True
    lease.assert_not_called()


# --- waiter behavior ---------------------------------------------------------


@pytest.mark.asyncio
async def test_unready_then_ready_posts_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_hooks_module()
    deferral = module.deferral
    _force_window(monkeypatch, deferral, inside=True)
    probe_calls = _set_readiness(monkeypatch, deferral, [False, False, True])
    sleeps = _instant_sleep(monkeypatch, deferral)

    ctx = _make_ctx(tmp_path)
    assert await deferral.gate_scheduled_fire(ctx) is True

    runtime = WaiterRuntimeStub(_state_root=tmp_path)
    await deferral.deferred_fire_loop(runtime)

    runtime.send_message.assert_awaited_once_with(
        "!room:test",
        "scheduled message for task-1",
        thread_id="$thread",
        trigger_dispatch=True,
    )
    assert deferral.pending_fires(tmp_path) == []
    # Gate probe plus two waiter probes (unready, then ready).
    assert len(probe_calls) == 3
    # One poll sleep while unready, one jitter sleep before posting.
    assert len(sleeps) == 2


@pytest.mark.asyncio
async def test_readiness_never_comes_posts_at_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_hooks_module()
    deferral = module.deferral
    _force_window(monkeypatch, deferral, inside=True)
    _set_readiness(monkeypatch, deferral, False)
    sleeps = _instant_sleep(monkeypatch, deferral)

    base = datetime(2024, 1, 1, tzinfo=UTC)
    clock = {"now": base}
    monkeypatch.setattr(deferral, "_utcnow", lambda: clock["now"])

    ctx = _make_ctx(tmp_path, settings={"worker_ready_cap_seconds": 60})
    assert await deferral.gate_scheduled_fire(ctx) is True

    runtime = WaiterRuntimeStub(_state_root=tmp_path)

    original_sleep = deferral._sleep

    async def advancing_sleep(seconds: float) -> None:
        clock["now"] = clock["now"] + timedelta(seconds=30)
        await original_sleep(seconds)

    monkeypatch.setattr(deferral, "_sleep", advancing_sleep)
    await deferral.deferred_fire_loop(runtime)

    runtime.send_message.assert_awaited_once()
    assert deferral.pending_fires(tmp_path) == []
    runtime.logger.info.assert_any_call(
        "workloop-deferred-fires: posted deferred scheduled fire %s (%s)",
        "task-1",
        "cap expired",
    )
    # Two poll sleeps before the cap (30s each), then the pre-post jitter sleep.
    assert len(sleeps) == 3


@pytest.mark.asyncio
async def test_two_schedules_both_post_once_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_hooks_module()
    deferral = module.deferral
    _force_window(monkeypatch, deferral, inside=True)
    _set_readiness(monkeypatch, deferral, [False, False, True])
    _instant_sleep(monkeypatch, deferral)

    assert await deferral.gate_scheduled_fire(_make_ctx(tmp_path, "task-a")) is True
    assert await deferral.gate_scheduled_fire(_make_ctx(tmp_path, "task-b")) is True

    runtime = WaiterRuntimeStub(_state_root=tmp_path)
    await deferral.deferred_fire_loop(runtime)

    posted = [call.args[1] for call in runtime.send_message.await_args_list]
    assert posted == [
        "scheduled message for task-a",
        "scheduled message for task-b",
    ]
    assert deferral.pending_fires(tmp_path) == []


@pytest.mark.asyncio
async def test_failed_post_is_retried_until_delivered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_hooks_module()
    deferral = module.deferral
    _force_window(monkeypatch, deferral, inside=True)
    _set_readiness(monkeypatch, deferral, True)
    _instant_sleep(monkeypatch, deferral)

    deferral.enqueue_fire(
        tmp_path,
        {
            "task_id": "task-1",
            "room_id": "!room:test",
            "thread_id": None,
            "message_text": "scheduled message for task-1",
            "enqueued_at": deferral._utcnow().isoformat(),
            "fire_by": deferral._utcnow().isoformat(),
        },
    )
    runtime = WaiterRuntimeStub(_state_root=tmp_path)
    runtime.send_message = AsyncMock(side_effect=[None, "$event"])

    await deferral.deferred_fire_loop(runtime)

    assert runtime.send_message.await_count == 2
    assert deferral.pending_fires(tmp_path) == []


@pytest.mark.asyncio
async def test_post_gives_up_loudly_after_max_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_hooks_module()
    deferral = module.deferral
    _set_readiness(monkeypatch, deferral, True)
    _instant_sleep(monkeypatch, deferral)

    deferral.enqueue_fire(
        tmp_path,
        {
            "task_id": "task-1",
            "room_id": "!room:test",
            "thread_id": None,
            "message_text": "scheduled message for task-1",
            "enqueued_at": deferral._utcnow().isoformat(),
            "fire_by": deferral._utcnow().isoformat(),
        },
    )
    runtime = WaiterRuntimeStub(_state_root=tmp_path)
    runtime.send_message = AsyncMock(return_value=None)

    await deferral.deferred_fire_loop(runtime)

    assert runtime.send_message.await_count == deferral.MAX_POST_ATTEMPTS
    assert deferral.pending_fires(tmp_path) == []
    runtime.logger.error.assert_called_once()


@pytest.mark.asyncio
async def test_gate_does_not_block_hook_pipeline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hook returns while the waiter is still parked on an unready probe."""
    module = _load_hooks_module()
    deferral = module.deferral
    _force_window(monkeypatch, deferral, inside=True)
    monkeypatch.setattr(module, "_DEFERRED_FIRE_TASK", None)

    release = asyncio.Event()

    async def gated_ready(_runtime_paths: Any, _config: Any, _log: Any) -> bool:
        if release.is_set():
            return True
        return False

    async def parked_sleep(_seconds: float) -> None:
        await release.wait()

    monkeypatch.setattr(deferral, "workers_ready", gated_ready)
    monkeypatch.setattr(deferral, "_sleep", parked_sleep)

    ctx = _make_ctx(tmp_path)
    await module.auto_poke(ctx)  # completes even though readiness never resolved

    assert ctx.suppress is True
    waiter = module._DEFERRED_FIRE_TASK
    assert waiter is not None
    assert not waiter.done()

    release.set()
    await asyncio.wait_for(waiter, timeout=5)
    ctx.send_message.assert_awaited_once()
    assert deferral.pending_fires(tmp_path) == []


@pytest.mark.asyncio
async def test_journal_resumes_after_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fires journaled before a restart are re-posted when the plugin starts."""
    module = _load_hooks_module()
    deferral = module.deferral
    deferral.enqueue_fire(
        tmp_path,
        {
            "task_id": "task-1",
            "room_id": "!room:test",
            "thread_id": "$thread",
            "message_text": "scheduled message for task-1",
            "enqueued_at": deferral._utcnow().isoformat(),
            "fire_by": deferral._utcnow().isoformat(),
        },
    )

    resumed: list[Any] = []

    async def fake_waiter(runtime: Any) -> None:
        resumed.append(runtime)

    monkeypatch.setattr(module, "_deferred_fire_loop", fake_waiter)
    monkeypatch.setattr(module, "_DEFERRED_FIRE_TASK", None)
    runtime = WaiterRuntimeStub(_state_root=tmp_path)

    module._resume_deferred_fires(runtime)
    assert module._DEFERRED_FIRE_TASK is not None
    await asyncio.wait_for(module._DEFERRED_FIRE_TASK, timeout=5)

    assert resumed == [runtime]


@pytest.mark.asyncio
async def test_resume_skips_when_journal_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_hooks_module()
    monkeypatch.setattr(module, "_DEFERRED_FIRE_TASK", None)
    runtime = WaiterRuntimeStub(_state_root=tmp_path)

    module._resume_deferred_fires(runtime)

    assert module._DEFERRED_FIRE_TASK is None


def test_journal_round_trip_preserves_order(tmp_path: Path) -> None:
    module = _load_hooks_module()
    deferral = module.deferral
    for task_id in ("task-a", "task-b", "task-c"):
        assert deferral.enqueue_fire(
            tmp_path,
            {"task_id": task_id, "room_id": "!room:test", "thread_id": None,
             "message_text": task_id, "enqueued_at": "2024-01-01T00:00:00+00:00",
             "fire_by": "2024-01-01T00:05:00+00:00"},
        )
    assert not deferral.enqueue_fire(
        tmp_path,
        {"task_id": "task-b", "room_id": "!room:test", "thread_id": None,
         "message_text": "dup", "enqueued_at": "2024-01-01T00:00:00+00:00",
         "fire_by": "2024-01-01T00:05:00+00:00"},
    )
    assert [e["task_id"] for e in deferral.pending_fires(tmp_path)] == [
        "task-a", "task-b", "task-c",
    ]
    assert deferral.discard_fire(tmp_path, "task-b")
    assert not deferral.discard_fire(tmp_path, "task-b")
    assert [e["task_id"] for e in deferral.pending_fires(tmp_path)] == [
        "task-a", "task-c",
    ]
    raw = json.loads(deferral.journal_path(tmp_path).read_text(encoding="utf-8"))
    assert {entry["task_id"] for entry in raw["entries"]} == {"task-a", "task-c"}
