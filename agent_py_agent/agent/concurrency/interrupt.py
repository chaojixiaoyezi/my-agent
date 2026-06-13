# LLM: per-thread 协作中断。
#   契约:①中断按线程隔离——同进程多 agent 并发时,取消一个会话不影响其他
#   会话正在跑的工具(gateway 场景的关键);②纯协作式:只立标志,靠执行方在
#   安全点轮询(round_execution 工具循环开头),绝不强杀线程;③register_
#   interruptible 的 finally 必清标志+注销名字——线程池 ident 会复用,脏标志
#   会让下一个任务莫名"被中断";④只覆盖进程内线程形态,独立进程形态照旧走
#   SIGTERM 升级链(subagents/process_control),gateway 停止语义不动。
#   改动时同步检查 round_execution 轮询点、background/dispatch worker 注册、
#   orchestration/tools/cancel.py 接线与 tests/test_thread_interrupt.py。
# 模块用途: 给"正在干活的线程"递一张暂停条:取消任务时立旗,线程在每个工具
#   开跑前看一眼,看到就体面收工,而不是被一刀杀掉。
from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager

_lock = threading.Lock()
_interrupted_threads: set[int] = set()
_named_threads: dict[str, int] = {}


# 函数用途: 给某线程立/撤中断旗(不传 thread_id 就是当前线程)。
def set_interrupt(active: bool, thread_id: int | None = None) -> None:
    tid = thread_id if thread_id is not None else threading.current_thread().ident
    if tid is None:
        return
    with _lock:
        if active:
            _interrupted_threads.add(tid)
        else:
            _interrupted_threads.discard(tid)


# 函数用途: 当前线程被要求中断了吗?(工具循环安全点逐次轮询用)
def is_interrupted() -> bool:
    tid = threading.current_thread().ident
    with _lock:
        return tid in _interrupted_threads


# 函数用途: 按登记名给后台线程立中断旗;返回是否找到了这个名字。
def interrupt_by_name(name: str) -> bool:
    with _lock:
        tid = _named_threads.get(name)
        if tid is None:
            return False
        _interrupted_threads.add(tid)
        return True


# 函数用途: worker 模板——进入登记"名字→本线程",退出 finally 注销并清旗
#   (线程复用安全:绝不把脏中断状态留给下一个任务)。
@contextmanager
def register_interruptible(name: str) -> Iterator[None]:
    tid = threading.current_thread().ident
    _register_named(name, tid)
    try:
        yield
    finally:
        _unregister_named(name, tid)


# 函数用途: 把"名字→线程"写进登记表(拿不到 ident 就什么都不做)。
def _register_named(name: str, tid: int | None) -> None:
    if tid is None:
        return
    with _lock:
        _named_threads[name] = tid


# 函数用途: 注销登记并清掉本线程中断旗(线程复用不带脏状态)。
def _unregister_named(name: str, tid: int | None) -> None:
    if tid is None:
        return
    with _lock:
        _named_threads.pop(name, None)
        _interrupted_threads.discard(tid)


__all__ = ["interrupt_by_name", "is_interrupted", "register_interruptible", "set_interrupt"]
