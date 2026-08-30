from __future__ import annotations

"""Process-level runner session heartbeat governance."""

import logging
import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from ...runtime_errors import runtime_error_report
from ...subagents.models import (
    SUBAGENT_RECOVERY_CLOSED_STATUSES,
    TaskStatus,
)


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
    from ...subagents.process_control import PROCESS_EPOCH, running_in_dispatch_subprocess

    now = time.time()
    return {
        "schema_version": "runner_session_pool.v1",
        "session_id": f"runsess-{lease.run_id}-{int(now * 1000)}-{os.getpid()}",
        "run_id": lease.run_id,
        "worker_id": lease.worker_id or f"pid:{os.getpid()}",
        "worker_pid": os.getpid(),
        # 记会话的进程实例身份:宿主已死回收据此判同代/异代——异代(重启换进程)记的
        # worker_pid 属旧进程不可信,只认心跳过期即回收(见 process_control.PROCESS_EPOCH)。
        "process_epoch": PROCESS_EPOCH,
        # in-process runner(网关进程内线程)才随网关存亡、适用 epoch 换代回收;独立派工子进程
        # 不随网关重启死,仍走 pid-liveness(保留 GC 抖动豁免)。
        "in_process": not running_in_dispatch_subprocess(),
        "started_at": now,
        "heartbeat_at": now,
        # 心跳节拍进会话事实:耐久判活(runner_session_liveness)按 6×interval 算新鲜窗。
        "interval_seconds": max(0.2, float(lease.interval_seconds or 5.0)),
        "ended_at": 0.0,
        "status": "starting",
    }


# LLM: Heartbeats stop themselves when the canonical lifecycle rejects a stale live-session
# projection; retryable persistence errors still keep the loop alive for the next interval.
# 函数用途: 周期刷新 runner 会话，任务已进入不可恢复终态时立即停止继续写心跳。
def _heartbeat_loop(lease: RunnerSessionPoolLease, session: dict[str, object], stop_event: threading.Event) -> None:
    interval = max(0.2, float(lease.interval_seconds or 5.0))
    while not stop_event.wait(interval):
        if not _record_runner_session(lease, session, status="running"):
            return


# LLM: This is the sole session-pool write adapter. False means canonical terminal state fenced
# this lease and callers must stop heartbeats; transient storage errors remain retryable True.
# 函数用途: 写一拍 runner-session 状态，并告诉心跳线程该执行轮是否仍有资格继续。
def _record_runner_session(
    lease: RunnerSessionPoolLease,
    session: dict[str, object],
    *,
    status: str,
    error: dict[str, object] | None = None,
) -> bool:
    try:
        task = lease.manager.load(lease.run_id)
        if _runner_session_update_is_stale_for_terminal_task(task, status):
            return False
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
        narrow_writer = getattr(type(lease.manager), "save_runner_session", None)
        if callable(narrow_writer):
            if lease.manager.save_runner_session(lease.run_id, current, now=now) is False:
                return False
        else:
            # Compatibility for embedders/test doubles with the historical
            # load/save manager surface only.
            lease.manager.save(task)
        return True
    except Exception:
        # 容忍:这是周期性 heartbeat,在后台线程里跑;单次 load/save 失败不能传播——
        # 否则会打死 heartbeat 线程/整轮 run,下一拍会重试。但不再无声:记日志可查
        # "session 元数据为何没更新"(续跑断链排障入口)。
        logging.getLogger(__name__).warning(
            "runner session metadata save failed (run_id=%s status=%s)",
            lease.run_id,
            status,
            exc_info=True,
        )
        return True


# LLM: Once the canonical task is recovery-closed, an old heartbeat cannot keep its runner
# session alive. DONE accepts only the worker's final completed receipt; cancelled/abandoned/
# taken-over tasks keep the terminal session written by their lifecycle controller.
# 函数用途: 判断当前 runner-session 更新是否来自已经失效的旧执行轮。
def _runner_session_update_is_stale_for_terminal_task(
    task: object,
    requested_status: str,
) -> bool:
    status = str(getattr(task, "status", "") or "").strip().upper()
    if status not in SUBAGENT_RECOVERY_CLOSED_STATUSES:
        return False
    return not (
        status == TaskStatus.DONE.value
        and str(requested_status or "").strip().lower() == "completed"
    )


__all__ = ["RunnerSessionPoolLease", "runner_session_lease"]
