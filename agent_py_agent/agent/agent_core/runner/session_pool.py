from __future__ import annotations

"""Process-level runner session heartbeat governance."""

import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from ...runtime_errors import runtime_error_report


@dataclass(frozen=True)
class RunnerSessionPoolLease:
    manager: Any
    run_id: str
    worker_id: str = ""
    interval_seconds: float = 5.0


@contextmanager
def runner_session_lease(lease: RunnerSessionPoolLease) -> Iterator[dict[str, object]]:
    """Record a durable runner session and keep heartbeats fresh while it runs."""

    session = _new_runner_session(lease)
    stop_event = threading.Event()
    _record_runner_session(lease, session, status="running")
    heartbeat = threading.Thread(
        target=_heartbeat_loop,
        args=(lease, session, stop_event),
        name=f"runner-session-{lease.run_id}",
        daemon=True,
    )
    heartbeat.start()
    try:
        yield session
    except BaseException as exc:
        _record_runner_session(lease, session, status="failed", error=runtime_error_report(exc, context="runner_session.failed"))
        raise
    else:
        _record_runner_session(lease, session, status="completed")
    finally:
        stop_event.set()
        heartbeat.join(timeout=1.0)


def _new_runner_session(lease: RunnerSessionPoolLease) -> dict[str, object]:
    now = time.time()
    return {
        "schema_version": "runner_session_pool.v1",
        "session_id": f"runsess-{lease.run_id}-{int(now * 1000)}-{os.getpid()}",
        "run_id": lease.run_id,
        "worker_id": lease.worker_id or f"pid:{os.getpid()}",
        "worker_pid": os.getpid(),
        "started_at": now,
        "heartbeat_at": now,
        "ended_at": 0.0,
        "status": "starting",
    }


def _heartbeat_loop(lease: RunnerSessionPoolLease, session: dict[str, object], stop_event: threading.Event) -> None:
    interval = max(0.2, float(lease.interval_seconds or 5.0))
    while not stop_event.wait(interval):
        _record_runner_session(lease, session, status="running")


def _record_runner_session(
    lease: RunnerSessionPoolLease,
    session: dict[str, object],
    *,
    status: str,
    error: dict[str, object] | None = None,
) -> None:
    try:
        task = lease.manager.load(lease.run_id)
        now = time.time()
        current = dict(session)
        current["status"] = status
        current["heartbeat_at"] = now
        if status in {"completed", "failed"}:
            current["ended_at"] = now
        if error is not None:
            current["error"] = error
        attrs = dict(getattr(task, "attributes", {}) or {})
        sessions = list(attrs.get("runner_session_history") or [])
        previous = attrs.get("runner_session")
        if isinstance(previous, dict) and previous.get("session_id") != current.get("session_id"):
            sessions.append(previous)
        attrs["runner_session"] = current
        attrs["runner_session_history"] = sessions[-10:]
        task.attributes = attrs
        task.heartbeat_at = now
        task.updated_at = now
        lease.manager.save(task)
    except Exception:
        return


__all__ = ["RunnerSessionPoolLease", "runner_session_lease"]
