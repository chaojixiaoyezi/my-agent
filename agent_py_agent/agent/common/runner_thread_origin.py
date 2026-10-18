# LLM: 原runner在Thread出生前捕获唯一native出处；消费者只观察同异常/Thread/Context/PID，不运行Context、不据此取得claim或释放P3，修改须同验runner和Gateway未知出口。
# 模块用途: 保存并核验runner设施退出异常的真实来源，让执行端和请求收尾共用同一低层实现。
from __future__ import annotations

import contextvars
import os
import threading
from dataclasses import dataclass, field


# LLM: 只保存原producer在start前持有的Thread/_started/Context/PID和固定诊断；不建注册表，不能序列化为权限。
# 类用途: 给原runner设施退出异常保留不可复制的真实出处，后续原claim仍独立核验。
@dataclass(frozen=True, eq=False, slots=True, repr=False)
class _RunnerThreadOrigin:
    thread: threading.Thread
    started: threading.Event
    context: contextvars.Context
    phase: str
    run_id: str
    attempt_id: str
    pid: int
    error: BaseException | None = field(default=None, init=False)
    origin: object = field(init=False)

    # LLM: self身份只用于拒复制，不读取状态或启动设施。
    # 函数用途: 固定当前观察者的原对象身份。
    def __post_init__(self):
        object.__setattr__(self, "origin", self)


# LLM: 仅原runner心跳与worker producer在start前调用；Context是当前原执行引用，字段不授run/claim权限。
# 函数用途: 在原Thread出生前捕获它的只读出处，不新建线程或持久记录。
def _capture_runner_thread(thread, phase, run_id, attempt_id) -> _RunnerThreadOrigin:
    origin = _RunnerThreadOrigin(thread, thread._started, contextvars.copy_context(), phase, run_id, attempt_id, os.getpid())
    thread._runner_thread_origin = origin
    thread._runner_thread_context = origin.context
    return origin


# LLM: stop/join只在原事务门外执行；确认退出前不能写普通runner结果，原ACK异常始终同对象，不制造Bounded id或enteredFalse。
# 函数用途: 收尾原runner设施，退出确认失败时把真实原异常锚定给原请求未结出口。
def _finish_original_runner_thread(origin, stop, primary=None) -> None:
    try:
        stop()
        origin.thread.join()
        if origin.thread.is_alive():
            raise RuntimeError("原runner线程退出未确认")
    except BaseException as failure:
        target = primary if primary is not None else failure
        metadata = BaseException.__dict__["__dict__"].__get__(target)
        if "_runner_thread_origin" not in metadata:
            if origin.error is None:
                object.__setattr__(origin, "error", target)
            if origin.error is target:
                metadata["_runner_thread_origin"] = origin
        if primary is None:
            raise
        raise primary from failure


# LLM: 原异常native字典中的同observer/error才是实际未知锚点；只拒普通收口，不证明来源仍活或允许新增unknown事件。
# 函数用途: 保留已确认出处但引用漂移或生命周期失效后的原责任，复制error或同code没有资格。
def runner_thread_exit_unconfirmed(error) -> bool:
    if not isinstance(error, BaseException):
        return False
    origin = BaseException.__dict__["__dict__"].__get__(error).get("_runner_thread_origin")
    return type(origin) is _RunnerThreadOrigin and origin.origin is origin and origin.error is error


# LLM: 查询只比原Thread/_started/Context/PID和原异常锚点，不join/reap、运行Context或签权；消费者仍须当前同execution/lifetime/完整claim。
# 函数用途: 返回原设施捕获的Context及固定phase/run/attempt诊断，拒复制、跨PID和引用替换。
def runner_thread_error_origin(error) -> tuple[contextvars.Context, str, str, str] | None:
    if not runner_thread_exit_unconfirmed(error):
        return None
    origin = BaseException.__dict__["__dict__"].__get__(error)["_runner_thread_origin"]
    if (origin.pid != os.getpid() or type(origin.thread) is not threading.Thread
            or getattr(origin.thread, "_runner_thread_origin", None) is not origin
            or getattr(origin.thread, "_started", None) is not origin.started
            or getattr(origin.thread, "_runner_thread_context", None) is not origin.context
            or type(origin.context) is not contextvars.Context
            or origin.phase not in {"child_runner_heartbeat_stop", "child_runner_worker_stop"}
            or type(origin.run_id) is not str or not origin.run_id or type(origin.attempt_id) is not str or not origin.attempt_id):
        return None
    return origin.context, origin.phase, origin.run_id, origin.attempt_id
