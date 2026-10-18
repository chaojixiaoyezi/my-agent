from __future__ import annotations

# LLM: runner租约沿原attempt取消；Thread出生/退出出处由common唯一实现保存，未确认只留同异常/Context；Count0原顺序和1秒预算不变。
# 模块用途: 为子代理续租、传递精确停止和观察长等待，复用原心跳，不叠加后台计时器。
import logging
import os
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from ...common.runner_thread_origin import (
    _capture_runner_thread,
    _finish_original_runner_thread,
    runner_thread_error_origin,
    runner_thread_exit_unconfirmed,
)
from ...runtime_db.operations import RuntimeConflictError
from ...runtime_errors import runtime_error_report
from ...subagents.models import (
    SUBAGENT_RECOVERY_CLOSED_STATUSES,
    TaskStatus,
)


# LLM: 租约绑定原准确attempt；可选transport只运输同canonical child来源，不是新租约或权限，activity只读，新轮不接旧心跳。
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
    request_transport: object | None = None


# LLM: 首次canonical fence拒绝不得执行；正MP启动ACK异常也在T外stop/join原Thread，确认后才写session完成/失败，未知同对象上抛；Count0原序。
# 函数用途: 为已取得执行权的 worker 发布会话并续租，失效工作不得借租约继续启动。
@contextmanager
def runner_session_lease(lease: RunnerSessionPoolLease) -> Iterator[dict[str, object]]:
    """Record a durable runner session and keep heartbeats fresh while it runs."""
    session = _new_runner_session(lease)
    stop_event = threading.Event()
    if not _record_runner_session(lease, session, status="running", require_persisted=True):
        raise RuntimeConflictError("执行轮会话发布已失效")
    heartbeat = threading.Thread(target=_heartbeat_loop, args=(lease, session, stop_event),
        name=f"runner-session-{lease.run_id}", daemon=True)
    origin = (_capture_runner_thread(heartbeat, "child_runner_heartbeat_stop", lease.run_id, lease.attempt_id)
              if lease.request_transport is not None else None)
    if origin is None:
        heartbeat.start()
    primary = None
    try:
        if origin is not None:
            heartbeat.start()
        yield session
    except BaseException as exc:
        primary = exc
        if origin is None:
            _raise_transport_unknown(exc)
            _record_runner_session(lease, session, status="failed", error=runtime_error_report(exc, context="runner_session.failed"))
        raise
    else:
        if origin is None:
            _record_runner_session(lease, session, status="completed")
    finally:
        if origin is None:
            stop_event.set()
            heartbeat.join(timeout=1.0)
        else:
            _finish_original_runner_thread(origin, stop_event.set, primary)
            if primary is None:
                _record_runner_session(lease, session, status="completed")
            else:
                _raise_transport_unknown(primary)
                _record_runner_session(lease, session, status="failed", error=runtime_error_report(primary, context="runner_session.failed"))


# LLM: session保原准确attempt，真实child额外记录同一原进程出生供保守判活，不用TTL证明退出；Count0原payload保持。
# 函数用途: 生成一份可追溯到本轮执行的心跳记录。
def _new_runner_session(lease: RunnerSessionPoolLease) -> dict[str, object]:
    from ...subagents.process_control import PROCESS_EPOCH, running_in_dispatch_subprocess
    from ...tooling.process_registry import capture_process_birth_token

    now = time.time()
    session = {
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
    if lease.request_transport is not None:
        from ...gateway_parts.daemon_metadata import process_host_id
        from ...gateway_parts.multiprocess_executor_identity import process_birth

        session["worker_process"] = {"host_id": process_host_id(), "pid": os.getpid(), "start_time": process_birth(os.getpid())}
    return session


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


# LLM: 实际child设施线程没有隐式ContextVar，必须沿原transport重入同source；不从字段补scope，首次发布/旧代/普通瞬时错误语义保持。
# 函数用途: 写一拍 runner-session 状态，并告诉心跳线程该执行轮是否仍有资格继续。
def _record_runner_session(
    lease: RunnerSessionPoolLease,
    session: dict[str, object],
    *,
    status: str,
    error: dict[str, object] | None = None,
    require_persisted: bool = False,
) -> bool:
    from ...gateway_parts.multiprocess_subagent_execution import child_worker_scope

    if lease.request_transport is not None:
        with child_worker_scope(lease.request_transport, lease.request_transport.worker_agent):
            return _record_runner_session_unscoped(lease, session, status=status, error=error, require_persisted=require_persisted)
    return _record_runner_session_unscoped(lease, session, status=status, error=error, require_persisted=require_persisted)


# LLM: 调用方已安装真实child短写端口；正文保存、canonical窄心跳及普通重试完全保main，运输未结原样穿越，不新建租约账。
# 函数用途: 按原协议写一拍runner状态，不能把关闭scope后的写失败变成已完成。
def _record_runner_session_unscoped(lease, session, *, status, error=None, require_persisted=False):
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
    except Exception as failure:
        _raise_transport_unknown(failure)
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


# LLM: 实际canonical四类或原runner native锚点同对象穿越；字段/code/文本都不授unknown或重试权，最终仍核原claim。
# LLM: 原审批pure/native异常链只保同一个外层错误；不从wrapper授清理/执行权，同码文本不升级，不签终态。
# 函数用途: 保原子代理未结异常，避免普通runner失败覆盖退休责任。
def _raise_transport_unknown(error):
    from ...backends.bounded_call import BoundedCallExitUnconfirmedError
    from ...common.request_execution_ports import (
        RequestExecutionContextUnavailable,
        RequestExecutionOutcomeUncertainError,
    )
    from ...common.tool_write_context import ToolWriteContextUnavailable
    from ...conversation.agent_tool_approval import find_approval_retirement_error

    if find_approval_retirement_error(error) is not None:
        raise error
    if runner_thread_exit_unconfirmed(error):
        raise error
    if isinstance(error, (RequestExecutionContextUnavailable, RequestExecutionOutcomeUncertainError,
                          BoundedCallExitUnconfirmedError, ToolWriteContextUnavailable)):
        raise error
