# LLM: 决策服务“observe 不挡主链路”的唯一后台执行器，只承接 decision_service 判定为非阻塞的调用（普通 thread 范围、
#   点位有效模式 observe、决策设置 observe_nonblocking_enabled 为真；apply 与实验永不进来）。整个进程只有一个 worker
#   串行执行，排队中的调用最多 decision_point_limits.OBSERVE_NONBLOCKING_MAX_PENDING_COUNT 条，满了入队失败、由决策服务记拒绝。
#   worker 在执行前装入发起时捕获的 runner 身份（让原复核在后台线程算出阶段身份），send 返回后立即恢复，之后的写行、结算
#   与完成回调都在恢复后的上下文里运行；不继承发起回合的取消（令牌检查由决策服务的 send 做）。
#   每条调用结束后在这里写一行结果日志（blocking=false）、把独立用量范围结算进会话 model_usage，再交回调方的完成回调；
#   不写发起回合的 live 状态或输出流。改动须同步 decision_service 的 _dispatch_nonblocking 与 test_decision_observe_nonblocking.py。
# 模块用途: 让 observe 模式的决策调用在后台完成，用户回合不再等待决策模型，同时保证结果日志和用量账都完整。
"""Bounded single-worker executor for non-blocking observe decisions."""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, replace
from types import SimpleNamespace

from ..runtime_context import restore_current_subagent_context, set_current_subagent_context
from . import decision_point_limits as limits
from .decision_outcome_log import append_decision_outcome, decision_outcome_row

# 后台 observe 调用在会话用量账里的来源标签；每次调用另有独立 request 编号（decision-observe:*）。
USAGE_SOURCE = "decision_observe"
_LOGGER = logging.getLogger(__name__)
_LOCK = threading.Condition()
_PENDING: deque[NonblockingObserve] = deque()
_WORKER: threading.Thread | None = None
_RUNNING = False


# LLM: 发起线程在登记在途决策之后构造。send 只收绝对期限并返回 DecisionOutcome，内部已把异常与取消映射成结果，不得抛出；
#   release 注销在途登记，执行器保证每条恰好调用一次。identity 是 set_current_subagent_context 的关键字参数
#   （run_id 取阶段身份），usage_request_id 是这次调用独立的用量范围编号。
# 类用途: 装着一次后台 observe 调用需要的全部结构化事实与回调。
@dataclass(frozen=True)
class NonblockingObserve:
    agent: object
    stage: object
    point: str
    budget_seconds: float
    usage_request_id: str
    send: Callable[[float], object]
    release: Callable[[], None]
    identity: dict
    on_result: Callable[[object], None] | None = None


# LLM: 只入队并按需启动唯一 worker，不等待；排队中的条数已到上限或线程起不来都返回 False，由调用方注销登记并记拒绝。
# 函数用途: 把一次后台 observe 调用放进有界队列（可能启动后台线程）。
def enqueue_nonblocking_observe(job: NonblockingObserve) -> bool:
    global _WORKER
    with _LOCK:
        if len(_PENDING) >= limits.OBSERVE_NONBLOCKING_MAX_PENDING_COUNT:
            return False
        _PENDING.append(job)
        if _WORKER is not None:
            return True
        worker = threading.Thread(target=_drain, name="decision-observe", daemon=True)
        try:
            worker.start()
        except RuntimeError:
            _PENDING.pop()
            return False
        _WORKER = worker
        return True


# LLM: 只读执行器状态并有界等待，不取消、不加速任何调用；给测试与需要确认后台已落账的收尾使用。
# 函数用途: 等到队列清空且没有正在执行的调用，超时返回 False。
def wait_nonblocking_idle(timeout: float) -> bool:
    with _LOCK:
        return _LOCK.wait_for(_idle, max(0.0, float(timeout)))


# LLM: 调用方持有 _LOCK（由 Condition.wait_for 保证）；三项都空才算空闲，worker 引用在它退出前一直在。
# 函数用途: 判断执行器是否已经没有排队、没有在执行、也没有存活的 worker。
def _idle() -> bool:
    return not _PENDING and not _RUNNING and _WORKER is None


# LLM: worker 主循环：逐条取出执行；队列空就退出，下一次入队再起新线程，线程不常驻。任何退出路径都释放 worker 引用。
# 函数用途: 在唯一后台线程里串行执行排队的 observe 调用。
def _drain() -> None:
    try:
        while (job := _next_job()) is not None:
            _run_guarded(job)
    finally:
        _release_worker()


# LLM: 单条异常只记日志不中断主循环；无论成败都清除“正在执行”标记并唤醒等待空闲的调用方。
# 函数用途: 执行一条后台调用并隔离它的异常。
def _run_guarded(job: NonblockingObserve) -> None:
    try:
        _run(job)
    except Exception:
        _LOGGER.warning("后台决策观察执行异常，已跳过这一条", exc_info=False)
    finally:
        _job_done()


# LLM: 只清掉指向本线程的引用（新入队可能已起了新 worker），并唤醒等待空闲的调用方。
# 函数用途: worker 退出时释放执行器里的线程引用。
def _release_worker() -> None:
    global _WORKER
    with _LOCK:
        if _WORKER is threading.current_thread():
            _WORKER = None
        _LOCK.notify_all()


# LLM: 与入队同一把锁；队列空时先清掉 worker 引用再返回 None，让之后的入队能起新线程。
# 函数用途: 取下一条待执行的调用，并标记“正在执行”。
def _next_job() -> NonblockingObserve | None:
    global _WORKER, _RUNNING
    with _LOCK:
        if not _PENDING:
            _WORKER = None
            _LOCK.notify_all()
            return None
        _RUNNING = True
        return _PENDING.popleft()


# 函数用途: 清除“正在执行”标记并唤醒等待空闲的调用方。
def _job_done() -> None:
    global _RUNNING
    with _LOCK:
        _RUNNING = False
        _LOCK.notify_all()


# LLM: 期限从 worker 真正开始这一条时起算（排队不吃预算）；结果行的耗时同样只算执行部分。三步的异常隔离靠各自的实现：
#   写行失败由 append_decision_outcome 吞掉记日志，结算失败由 settle_standalone_model_usage 吞掉，回调异常由 _notify 捕获；
#   万一仍有异常漏出，_run_guarded 记日志并继续下一条。结算在超时那一刻之后执行：worker 里迟到的供应商事实只留在进程内账本，
#   不再补进 model_usage（有意的边界）。
# 函数用途: 执行一条后台 observe 调用并完成它的全部落账（写结果行、结算用量、交回完成回调）。
def _run(job: NonblockingObserve) -> None:
    started = time.monotonic()
    outcome = replace(_execute(job, started + job.budget_seconds), blocking=False)
    append_decision_outcome(job.agent, decision_outcome_row(job.stage, job.point, outcome, time.monotonic() - started))
    _settle_usage(job)
    _notify(job, outcome)


# LLM: 身份只装在本 worker 线程，执行完按快照恢复；无论成败都注销在途登记，保证每条恰好注销一次。
# 函数用途: 以发起时捕获的身份执行原发送与复核，返回结构化结果。
def _execute(job: NonblockingObserve, deadline: float) -> object:
    try:
        previous = set_current_subagent_context(job.agent, **job.identity)
        try:
            return job.send(deadline)
        finally:
            restore_current_subagent_context(job.agent, previous)
    finally:
        job.release()


# LLM: 复用独立辅助调用的原结算入口：按本次独立 request 编号取累计快照追加一条会话用量事件（run/task 只作归属字段），
#   没有物理调用（发送前就作废）时不写；结算失败由原入口记日志，不影响结果行。
# 函数用途: 把这次后台调用的真实用量写进所属会话的 model_usage（写文件副作用）。
def _settle_usage(job: NonblockingObserve) -> None:
    from .auxiliary_model_call import settle_standalone_model_usage

    stage = job.stage
    settle_standalone_model_usage(SimpleNamespace(
        agent=job.agent, store=getattr(job.agent, "conversation_store", None),
        thread=SimpleNamespace(thread_id=stage.thread_id), request_id=job.usage_request_id,
        run_id=stage.run_id, task_id=stage.task_id,
    ), source=USAGE_SOURCE, usage_only=True)


# LLM: 回调由调用点提供（如选模型把建议编号补记进原请求），在 worker 线程执行；异常只记日志，结果行与用量账已先落盘。
# 函数用途: 把最终结果交给调用点的完成回调。
def _notify(job: NonblockingObserve, outcome: object) -> None:
    if job.on_result is None:
        return
    try:
        job.on_result(outcome)
    except Exception:
        _LOGGER.warning("后台决策观察的完成回调失败；结果行与用量账已保存", exc_info=False)


__all__ = [
    "USAGE_SOURCE",
    "NonblockingObserve",
    "enqueue_nonblocking_observe",
    "wait_nonblocking_idle",
]
