from __future__ import annotations

# LLM: runner 租约沿原执行轮读取持久取消并转交本进程中断；可选活动观察失败不改变状态，联测独立/共享宿主。
# 模块用途: 为子代理续租、传递精确停止和观察长等待，复用原心跳，不叠加后台计时器。
import logging
import os
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from ...runtime_db.operations import RuntimeConflictError
from ...runtime_errors import runtime_error_report
from ...subagents.models import (
    SUBAGENT_RECOVERY_CLOSED_STATUSES,
    TaskStatus,
)


# LLM: 租约绑定已激活的准确 attempt；activity_observer 只提供只读采样，新轮不能接收旧心跳。
# 类用途: 声明子代理心跳间隔和附带检查，不负责超时杀进程或重新派工。
@dataclass(frozen=True)
class RunnerSessionPoolLease:
    manager: Any
    run_id: str
    worker_id: str = ""
    interval_seconds: float = 5.0
    activity_observer: Callable[[], None] | None = None
    attempt_id: str = ""
    launch_id: str = ""


# LLM: 第一次发布被 canonical fence 拒绝时不得进入执行体；后续心跳仍沿唯一窄写入口。
# 函数用途: 为已取得执行权的 worker 发布会话并续租，失效工作不得借租约继续启动。
@contextmanager
def runner_session_lease(lease: RunnerSessionPoolLease) -> Iterator[dict[str, object]]:
    """Record a durable runner session and keep heartbeats fresh while it runs."""

    session = _new_runner_session(lease)
    stop_event = threading.Event()
    if not _record_runner_session(lease, session, status="running", require_persisted=True):
        raise RuntimeConflictError("执行轮会话发布已失效")
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


# LLM: session 保存宿主已激活的 attempt 身份，不能从新鲜 task 反查并替换旧 worker 的身份。
# 函数用途: 生成一份可追溯到本轮执行的心跳记录。
def _new_runner_session(lease: RunnerSessionPoolLease) -> dict[str, object]:
    from ...subagents.process_control import PROCESS_EPOCH, running_in_dispatch_subprocess
    from ...tooling.process_registry import capture_process_birth_token

    now = time.time()
    return {
        "schema_version": "runner_session_pool.v1",
        "session_id": f"runsess-{lease.run_id}-{int(now * 1000)}-{os.getpid()}",
        "run_id": lease.run_id,
        "attempt_id": lease.attempt_id,
        "launch_id": lease.launch_id,
        "worker_id": lease.worker_id or f"pid:{os.getpid()}",
        "worker_pid": os.getpid(),
        "worker_birth_token": capture_process_birth_token(os.getpid()),
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


# LLM: 同一心跳读取持久取消事实并在 worker 所在进程中转精确中断；自然收口不发中断，暂时读取错误仍可重试。
# 函数用途: 续租、诊断长等待，并让独立进程收到针对原执行轮的停止，不额外创建轮询器。
def _heartbeat_loop(lease: RunnerSessionPoolLease, session: dict[str, object], stop_event: threading.Event) -> None:
    interval = max(0.2, float(lease.interval_seconds or 5.0))
    while not stop_event.wait(interval):
        if _relay_runner_stop(lease):
            return
        if not _record_runner_session(lease, session, status="running"):
            # 控制可能在上次读取与本次窄写之间关闭权限；拒绝心跳不能让跨进程取消传递一起退出。
            continue
        if lease.activity_observer is not None:
            try:
                lease.activity_observer()
            except Exception:
                logging.getLogger(__name__).warning("runner activity observation failed (run_id=%s)", lease.run_id, exc_info=True)


# LLM: 持久权威已关闭才中断同进程命名 token；不会向共享 PID 发信号，缺 token 留给注册后的同一状态复核。
# 函数用途: 将其它进程提交的停止转换为本 worker 的模型/工具协作中断。
def _relay_runner_stop(lease: RunnerSessionPoolLease) -> bool:
    from ...concurrency.interrupt import interrupt_by_name
    from ...subagents.runner_control import runner_attempt_cancelled

    try:
        if not runner_attempt_cancelled(lease.manager, lease.run_id, lease.attempt_id):
            return False
    except Exception:
        logging.getLogger(__name__).warning("runner 停止状态读取未确认 (run_id=%s)", lease.run_id, exc_info=True)
        return False
    interrupt_by_name(f"subagent-runner-attempt:{lease.run_id}:{lease.attempt_id}")
    return True


# LLM: 首次发布必须落盘后才能执行，旧代返回 False；周期心跳的瞬时错误保留原可重试语义。
# 函数用途: 写一拍 runner-session 状态，并告诉心跳线程该执行轮是否仍有资格继续。
def _record_runner_session(
    lease: RunnerSessionPoolLease,
    session: dict[str, object],
    *,
    status: str,
    error: dict[str, object] | None = None,
    require_persisted: bool = False,
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
        if require_persisted:
            raise
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
