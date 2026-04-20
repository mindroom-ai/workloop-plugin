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
from unittest.mock import Mock

import pytest

PACKAGE_NAME = (
    f"mindroom_plugin_{Path(__file__).resolve().parents[1].name.replace('-', '_')}"
)


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
    ) -> None:
        self.state_root = state_root
        self.config = SimpleNamespace(agents=agents)
        self.settings = {
            "poke_cooldown_seconds": 300,
            "recent_response_grace_seconds": 30,
            "stale_busy_seconds": 600,
            "max_pokes_per_tick": 10,
            "min_idle_before_poke_seconds": 0,
            **(settings or {}),
        }
        self.sent_messages: list[dict[str, Any]] = []
        self.query_calls: list[tuple[str, str, str | None]] = []
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


@pytest.mark.asyncio
async def test_bug_a_scope_isolation_under_multi_thread_load(tmp_path: Path) -> None:
    module = _load_hooks_module()
    room_id = "!room:test"
    thread_x = "$threadX"
    thread_y = "$threadY"
    now = datetime.now(UTC)
    scope_x = f"{room_id}:{thread_x}"
    scope_y = f"{room_id}:{thread_y}"

    _write_thread_todos(
        module, tmp_path, room_id, thread_x, [_todo("x1", "Task X", "worker")]
    )
    _write_thread_todos(
        module, tmp_path, room_id, thread_y, [_todo("y1", "Task Y", "worker")]
    )
    module.state.update_agent_state(
        tmp_path,
        "worker",
        {
            "active_runs": {
                scope_x: {"started_at": (now - timedelta(seconds=60)).isoformat()}
            },
            "poked_scopes": {
                scope_y: (now - timedelta(seconds=3600)).isoformat(),
            },
            "poked_scope_messages": {},
        },
    )
    ctx = ScanContextStub(tmp_path, agents={"worker": object()})

    pokes = await module._run_poke_scan(ctx)

    assert pokes == 1
    assert len(ctx.sent_messages) == 1
    assert ctx.sent_messages[0]["room_id"] == room_id
    assert ctx.sent_messages[0]["thread_id"] == thread_y
    assert ctx.sent_messages[0]["text"].startswith("@worker workloop resume.")


@pytest.mark.asyncio
async def test_bug_b_zombie_cleanup_runs_at_scan_start(tmp_path: Path) -> None:
    module = _load_hooks_module()
    now = datetime.now(UTC)
    fresh_entry = {"started_at": (now - timedelta(seconds=60)).isoformat()}
    zombies = {
        f"scope-{index}": {
            "started_at": (now - timedelta(days=30, minutes=index)).isoformat()
        }
        for index in range(5)
    }
    module.state.update_agent_state(
        tmp_path,
        "worker",
        {"active_runs": {**zombies, "fresh": fresh_entry}},
    )

    pokes = await module._run_poke_scan(
        ScanContextStub(tmp_path, agents={"worker": object()})
    )

    assert pokes == 0
    assert module.state.read_agent_state(tmp_path, "worker")["active_runs"] == {
        "fresh": fresh_entry
    }


@pytest.mark.asyncio
async def test_bug_b_malformed_active_runs_are_dropped(tmp_path: Path) -> None:
    module = _load_hooks_module()
    now = datetime.now(UTC)
    fresh_entry = {"started_at": (now - timedelta(seconds=60)).isoformat()}
    module.state.update_agent_state(
        tmp_path,
        "worker",
        {
            "active_runs": {
                "missing-started-at": {},
                "non-dict-value": "oops",
                "unparseable": {"started_at": "not-a-timestamp"},
                "fresh": fresh_entry,
            }
        },
    )

    pokes = await module._run_poke_scan(
        ScanContextStub(tmp_path, agents={"worker": object()})
    )

    assert pokes == 0
    assert module.state.read_agent_state(tmp_path, "worker")["active_runs"] == {
        "fresh": fresh_entry
    }


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
async def test_inject_todos_and_poke_scan_use_matching_scope_keys(
    tmp_path: Path,
) -> None:
    module = _load_hooks_module()
    room_id = "!room:test"
    source_thread_id = "$source-thread"
    resolved_thread_id = "$resolved-thread"
    envelope = _make_envelope(
        room_id=room_id,
        source_thread_id=source_thread_id,
        resolved_thread_id=resolved_thread_id,
    )
    todos_path = _write_thread_todos(
        module,
        tmp_path,
        room_id,
        resolved_thread_id,
        [_todo("t5", "Resume after restart", "worker")],
    )

    await module.inject_todos(
        SimpleNamespace(
            target_entity_name="worker",
            envelope=envelope,
            settings={"max_items_in_enrichment": 10},
            state_root=tmp_path,
        )
    )

    state = module.state.read_agent_state(tmp_path, "worker")
    run_keys = list(state["active_runs"])
    thread_state = json.loads(todos_path.read_text(encoding="utf-8"))
    scan_scope_key = f"{thread_state['room_id']}:{thread_state['thread_id']}"

    assert run_keys == [f"{room_id}:{resolved_thread_id}"]
    assert run_keys[0] == scan_scope_key


@pytest.mark.asyncio
async def test_main_scope_behaves_like_any_other_scope(tmp_path: Path) -> None:
    module = _load_hooks_module()
    room_id = "!room:test"
    thread_id = "$threadA"
    settings = {
        "poke_cooldown_seconds": 0,
        "recent_response_grace_seconds": 0,
        "min_idle_before_poke_seconds": 0,
    }
    ctx = ScanContextStub(tmp_path, agents={"worker": object()}, settings=settings)
    _write_thread_todos(
        module, tmp_path, room_id, None, [_todo("m1", "Room task", "worker")]
    )
    _write_thread_todos(
        module, tmp_path, room_id, thread_id, [_todo("t6", "Thread task", "worker")]
    )

    module.state.update_agent_state(
        tmp_path,
        "worker",
        {
            "active_runs": {
                f"{room_id}:main": {
                    "started_at": (
                        datetime.now(UTC) - timedelta(seconds=60)
                    ).isoformat()
                }
            },
            "poked_scopes": {},
            "poked_scope_messages": {},
            "last_poked_at": None,
        },
    )
    first_pokes = await module._run_poke_scan(ctx)

    assert first_pokes == 1
    assert [message["thread_id"] for message in ctx.sent_messages] == [thread_id]

    ctx.sent_messages.clear()
    module.state.update_agent_state(
        tmp_path,
        "worker",
        {
            "active_runs": {
                f"{room_id}:{thread_id}": {
                    "started_at": (
                        datetime.now(UTC) - timedelta(seconds=60)
                    ).isoformat()
                }
            },
            "poked_scopes": {},
            "poked_scope_messages": {},
            "last_poked_at": None,
        },
    )
    second_pokes = await module._run_poke_scan(ctx)

    assert second_pokes == 1
    assert [message["thread_id"] for message in ctx.sent_messages] == [None]


@pytest.mark.asyncio
async def test_multi_agent_isolation_keeps_other_agent_pokable(tmp_path: Path) -> None:
    module = _load_hooks_module()
    room_id = "!room:test"
    thread_id = "$threadA"
    _write_thread_todos(
        module,
        tmp_path,
        room_id,
        thread_id,
        [
            _todo("a1", "Agent A task", "agent_a"),
            _todo("b1", "Agent B task", "agent_b"),
        ],
    )
    module.state.update_agent_state(
        tmp_path,
        "agent_a",
        {
            "active_runs": {
                f"{room_id}:{thread_id}": {
                    "started_at": (
                        datetime.now(UTC) - timedelta(seconds=60)
                    ).isoformat()
                }
            }
        },
    )
    ctx = ScanContextStub(
        tmp_path,
        agents={"agent_a": object(), "agent_b": object()},
        settings={
            "poke_cooldown_seconds": 0,
            "recent_response_grace_seconds": 0,
            "min_idle_before_poke_seconds": 0,
        },
    )

    pokes = await module._run_poke_scan(ctx)

    assert pokes == 1
    assert len(ctx.sent_messages) == 1
    assert ctx.sent_messages[0]["thread_id"] == thread_id
    assert ctx.sent_messages[0]["text"].startswith("@agent_b workloop resume.")


@pytest.mark.asyncio
async def test_full_lifecycle_clears_busy_run_on_idle(tmp_path: Path) -> None:
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
        [_todo("t8", "Finish lifecycle test", "worker")],
    )

    await module.inject_todos(
        SimpleNamespace(
            target_entity_name="worker",
            envelope=envelope,
            settings={"max_items_in_enrichment": 10},
            state_root=tmp_path,
        )
    )
    run_key = f"{room_id}:{resolved_thread_id}"
    assert run_key in module.state.read_agent_state(tmp_path, "worker")["active_runs"]

    await module.track_idle(
        SimpleNamespace(
            state_root=tmp_path,
            result=SimpleNamespace(envelope=envelope),
        )
    )

    state = module.state.read_agent_state(tmp_path, "worker")
    assert run_key not in state["active_runs"]
    assert state["last_response_at"] is not None
    assert (
        module._should_poke_agent(
            tmp_path,
            "worker",
            datetime.now(UTC),
            cooldown=0,
            grace=0,
            stale_busy=600,
            scope_key=run_key,
            min_idle=0,
        )
        is True
    )


@pytest.mark.asyncio
async def test_track_cancelled_clears_busy_run_without_touching_last_response(
    tmp_path: Path,
) -> None:
    module = _load_hooks_module()
    room_id = "!room:test"
    resolved_thread_id = "$resolved-thread"
    previous_response_at = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
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
        [_todo("t9", "Cancel lifecycle test", "worker")],
    )
    module.state.update_agent_state(
        tmp_path,
        "worker",
        {"last_response_at": previous_response_at},
    )

    await module.inject_todos(
        SimpleNamespace(
            target_entity_name="worker",
            envelope=envelope,
            settings={"max_items_in_enrichment": 10},
            state_root=tmp_path,
        )
    )
    run_key = f"{room_id}:{resolved_thread_id}"
    assert run_key in module.state.read_agent_state(tmp_path, "worker")["active_runs"]

    await module.track_cancelled(
        SimpleNamespace(
            state_root=tmp_path,
            info=SimpleNamespace(envelope=envelope),
        )
    )

    state = module.state.read_agent_state(tmp_path, "worker")
    assert run_key not in state["active_runs"]
    assert state["last_response_at"] == previous_response_at
