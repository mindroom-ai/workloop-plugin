"""Startup-safe deferral of scheduled fires while sandbox workers are starting.

A scheduled fire that lands right after a service restart can dispatch an
agent before sandbox workers are ready: its worker-routed tool calls fail
while the worker is still initializing and the scheduled run is lost. This
module gates ``schedule:fired`` deliveries during the startup window:

- When workers are ready (the steady-state case) the fire is untouched, and
  outside the startup window the gate does not even probe.
- When workers are still starting, the fire is journaled to disk, suppressed,
  and re-posted by a background waiter once workers become ready — or once a
  bounded cap expires, in which case it posts anyway. A gated fire is never
  silently dropped.

Durability: the journal entry is written *before* the original fire is
suppressed and removed only *after* the re-post succeeds, so a process
restart mid-wait resumes the fire from the journal (the lifecycle hooks in
``hooks.py`` restart the waiter). The remaining duplicate window is a crash
between a successful re-post and the journal removal that follows it; there
is no loss window: if the process dies before the journal write completes the
hook never suppressed, so the platform delivers (or retries) the fire itself.
"""

from __future__ import annotations

import asyncio
import random
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from .state import locked_update_json, read_json

DEFAULT_WORKER_READY_GATE_WINDOW_SECONDS = 300
DEFAULT_WORKER_READY_CAP_SECONDS = 300
DEFAULT_WORKER_READY_POLL_SECONDS = 5
DEFAULT_WORKER_READY_POST_JITTER_SECONDS = 2
PROBE_TIMEOUT_SECONDS = 3.0
MAX_POST_ATTEMPTS = 5

_MODULE_LOADED_AT_MONOTONIC = time.monotonic()

# Patchable indirections so tests can drive the waiter without real sleeps.
_sleep = asyncio.sleep


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _monotonic() -> float:
    return time.monotonic()


class DeferredFireRuntime(Protocol):
    """What the gate and waiter need from a hook context or poke runtime."""

    settings: dict[str, Any]
    config: Any
    runtime_paths: Any
    logger: Any
    state_root: Path

    async def send_message(
        self,
        room_id: str,
        text: str,
        *,
        thread_id: str | None = None,
        trigger_dispatch: bool = False,
    ) -> str | None: ...


def _setting_seconds(settings: dict[str, Any], key: str, default: int, log: Any) -> int:
    raw = settings.get(key, default)
    try:
        return int(raw)
    except (TypeError, ValueError):
        log.warning(
            "workloop-deferred-fires: invalid %s=%r; using default %s",
            key,
            raw,
            default,
        )
        return default


def journal_path(state_root: Path) -> Path:
    return state_root / "deferred_fires.json"


def pending_fires(state_root: Path) -> list[dict[str, Any]]:
    """Return journaled fires in enqueue (FIFO) order."""
    path = journal_path(state_root)
    if not path.exists():
        return []
    entries = read_json(path).get("entries", [])
    return [entry for entry in entries if isinstance(entry, dict)]


def has_pending_fires(state_root: Path) -> bool:
    return bool(pending_fires(state_root))


def enqueue_fire(state_root: Path, entry: dict[str, Any]) -> bool:
    """Append one fire to the journal unless its task is already pending."""

    def mutate(data: dict[str, Any]) -> bool:
        entries: list[dict[str, Any]] = data.setdefault("entries", [])
        if any(existing.get("task_id") == entry["task_id"] for existing in entries):
            return False
        entries.append(entry)
        return True

    return bool(locked_update_json(journal_path(state_root), mutate))


def discard_fire(state_root: Path, task_id: str) -> bool:
    path = journal_path(state_root)
    if not path.exists():
        return False

    def mutate(data: dict[str, Any]) -> bool:
        entries: list[dict[str, Any]] = data.get("entries", [])
        kept = [entry for entry in entries if entry.get("task_id") != task_id]
        data["entries"] = kept
        return len(kept) != len(entries)

    return bool(locked_update_json(path, mutate))


def in_startup_window(window_seconds: int) -> bool:
    return _monotonic() - _MODULE_LOADED_AT_MONOTONIC < window_seconds


def _workers_ready_blocking(runtime_paths: Any, config: Any) -> bool:
    """Return whether worker-routed tool calls can currently be served.

    True means "do not gate": every provisioned worker is past its starting
    phase, or no dedicated worker backend is configured at all. Scaled-down
    (idle) workers do not gate — on-demand provisioning already waits for
    readiness on that path.
    """
    # why-lazy: only the startup-window probe needs the worker runtime, and
    # these modules pull in backend clients that plugin import must not pay.
    from mindroom.tool_system.sandbox_proxy import sandbox_proxy_config  # noqa: PLC0415
    from mindroom.workers.runtime import (  # noqa: PLC0415
        lease_primary_worker_manager,
        primary_worker_backend_available,
        primary_worker_backend_is_dedicated,
        primary_worker_backend_name,
        serialized_kubernetes_worker_validation_snapshot,
    )

    if not primary_worker_backend_is_dedicated(runtime_paths):
        return True
    proxy_config = sandbox_proxy_config(runtime_paths)
    if not primary_worker_backend_available(
        runtime_paths,
        proxy_url=proxy_config.proxy_url,
        proxy_token=proxy_config.proxy_token,
    ):
        return True
    snapshot = None
    if primary_worker_backend_name(runtime_paths) == "kubernetes":
        snapshot = serialized_kubernetes_worker_validation_snapshot(
            runtime_paths,
            runtime_config=config,
        )
    # Same lease recipe as the platform's own workers endpoint: matching the
    # active manager's config signature borrows it instead of rebuilding it.
    with lease_primary_worker_manager(
        runtime_paths,
        proxy_url=proxy_config.proxy_url,
        proxy_token=proxy_config.proxy_token,
        storage_root=runtime_paths.storage_root,
        kubernetes_tool_validation_snapshot=snapshot,
        worker_grantable_credentials=config.get_worker_grantable_credentials(),
    ) as worker_manager:
        handles = worker_manager.list_workers(include_idle=True)
    return all(handle.status != "starting" for handle in handles)


async def workers_ready(runtime_paths: Any, config: Any, log: Any) -> bool:
    """Probe worker readiness, failing open so the gate can never block a fire."""
    try:
        async with asyncio.timeout(PROBE_TIMEOUT_SECONDS):
            return await asyncio.to_thread(_workers_ready_blocking, runtime_paths, config)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.warning(
            "workloop-deferred-fires: worker readiness probe failed; assuming ready",
            exc_info=True,
        )
        return True


async def gate_scheduled_fire(ctx: Any) -> bool:
    """Suppress and journal one scheduled fire when workers are not ready.

    Returns True when the fire was deferred; the caller must then ensure the
    waiter task is running. Any exception escaping this gate leaves
    ``ctx.suppress`` unset, so the platform delivers the fire itself — the
    gate fails open, never toward a dropped run.
    """
    settings = ctx.settings or {}
    log = ctx.logger
    window = _setting_seconds(
        settings,
        "worker_ready_gate_window_seconds",
        DEFAULT_WORKER_READY_GATE_WINDOW_SECONDS,
        log,
    )
    if window <= 0:
        return False
    state_root = ctx.state_root
    if not in_startup_window(window) or await workers_ready(
        ctx.runtime_paths, ctx.config, log
    ):
        # A live fire supersedes any journaled copy of the same task left
        # over from an earlier deferral, so the run still posts exactly once.
        discard_fire(state_root, ctx.task_id)
        return False
    cap = _setting_seconds(
        settings,
        "worker_ready_cap_seconds",
        DEFAULT_WORKER_READY_CAP_SECONDS,
        log,
    )
    now = _utcnow()
    enqueue_fire(
        state_root,
        {
            "task_id": ctx.task_id,
            "room_id": ctx.room_id,
            "thread_id": ctx.thread_id,
            "message_text": ctx.message_text,
            "enqueued_at": now.isoformat(),
            "fire_by": (now + timedelta(seconds=cap)).isoformat(),
        },
    )
    ctx.suppress = True
    log.warning(
        "workloop-deferred-fires: workers not ready; deferring scheduled fire %s in room %s (cap %ss)",
        ctx.task_id,
        ctx.room_id,
        cap,
    )
    return True


def _is_capped(entry: dict[str, Any], now: datetime) -> bool:
    try:
        return now >= datetime.fromisoformat(entry["fire_by"])
    except (KeyError, TypeError, ValueError):
        return True


def _post_jitter_seconds(settings: dict[str, Any], log: Any) -> float:
    jitter = _setting_seconds(
        settings,
        "worker_ready_post_jitter_seconds",
        DEFAULT_WORKER_READY_POST_JITTER_SECONDS,
        log,
    )
    return random.uniform(0, max(jitter, 0))


async def _post_deferred_fire(
    runtime: DeferredFireRuntime,
    entry: dict[str, Any],
    *,
    ready: bool,
    post_attempts: dict[str, int],
) -> bool:
    log = runtime.logger
    task_id = str(entry.get("task_id"))
    if not any(
        pending.get("task_id") == task_id for pending in pending_fires(runtime.state_root)
    ):
        return True  # discarded by a live fire while this batch was posting
    event_id: str | None = None
    try:
        event_id = await runtime.send_message(
            entry["room_id"],
            entry["message_text"],
            thread_id=entry.get("thread_id"),
            trigger_dispatch=True,
        )
    except Exception:
        log.exception("workloop-deferred-fires: posting deferred fire %s raised", task_id)
    if event_id is None:
        attempts = post_attempts.get(task_id, 0) + 1
        post_attempts[task_id] = attempts
        if attempts >= MAX_POST_ATTEMPTS:
            discard_fire(runtime.state_root, task_id)
            log.error(
                "workloop-deferred-fires: giving up on deferred fire %s after %d failed posts",
                task_id,
                attempts,
            )
        else:
            log.warning(
                "workloop-deferred-fires: failed to post deferred fire %s (attempt %d); will retry",
                task_id,
                attempts,
            )
        return False
    discard_fire(runtime.state_root, task_id)
    log.info(
        "workloop-deferred-fires: posted deferred scheduled fire %s (%s)",
        task_id,
        "workers ready" if ready else "cap expired",
    )
    return True


async def deferred_fire_loop(runtime: DeferredFireRuntime) -> None:
    """Re-post journaled scheduled fires once workers are ready or capped out."""
    log = runtime.logger
    settings = runtime.settings or {}
    poll = max(
        1,
        _setting_seconds(
            settings,
            "worker_ready_poll_seconds",
            DEFAULT_WORKER_READY_POLL_SECONDS,
            log,
        ),
    )
    post_attempts: dict[str, int] = {}
    log.info("workloop-deferred-fires: waiter started")
    try:
        while True:
            entries = pending_fires(runtime.state_root)
            if not entries:
                log.info("workloop-deferred-fires: journal drained; waiter stopped")
                return
            ready = await workers_ready(runtime.runtime_paths, runtime.config, log)
            now = _utcnow()
            due = entries if ready else [e for e in entries if _is_capped(e, now)]
            posted_all = True
            if due:
                await _sleep(_post_jitter_seconds(settings, log))
                for entry in due:
                    posted = await _post_deferred_fire(
                        runtime, entry, ready=ready, post_attempts=post_attempts
                    )
                    posted_all = posted_all and posted
                if posted_all:
                    continue
            await _sleep(poll)
    except asyncio.CancelledError:
        log.info("workloop-deferred-fires: waiter cancelled")
        raise
