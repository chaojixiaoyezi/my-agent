"""按物理锁身份检查当前线程的完整持有集；生产只计数，不授予业务权限。"""
from __future__ import annotations

import asyncio
import threading
import traceback
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import IntEnum


class LockLevel(IntEnum):
    CONVERSATION = 20
    CG = 30
    T = 40
    M = 50
    L = 60
    TASK_STORE = 70
    JSON_PATH = 80
    SQLITE = 90


class LockOrderViolation(RuntimeError):
    pass


_LOCAL = threading.local()
_STATS_LOCK = threading.Lock()
_STRICT = False
_COUNTS = {"acquisitions": 0, "tries": 0, "uncategorised": 0, "violations": 0, "handler_overruns": 0}
_LAST: dict = {}
_PATH_CATEGORY: ContextVar[tuple | None] = ContextVar("lockdep_path_category", default=None)


@contextmanager
def path_category(path, level):
    """只为这一个物理路径提供分类，不另取锁，也不影响嵌套的其它JSON路径。"""
    token = _PATH_CATEGORY.set((str(path.absolute()), level))
    try:
        yield
    finally:
        _PATH_CATEGORY.reset(token)


def category_for_path(path, category):
    selected = _PATH_CATEGORY.get()
    return selected[1] if category is None and selected is not None and str(path.absolute()) == selected[0] else category


@dataclass
class HeldLock:
    lock_id: int
    level: LockLevel | None
    try_acquire: bool
    stack: tuple[str, ...] = ()
    yielded: list[bool] = field(default_factory=lambda: [False])
    yield_check: object = None


def configure(strict: bool) -> None:
    """严格模式仅由测试显式设置；切换模式不清掉已发生的违反。"""
    global _STRICT
    _STRICT = bool(strict)


def is_strict() -> bool:
    return _STRICT


def note_handler_overrun() -> None:
    with _STATS_LOCK:
        _COUNTS["handler_overruns"] += 1
    if _STRICT:
        raise LockOrderViolation("conversation mailbox handler exceeded its budget")


def reset_for_tests() -> None:
    """仅供反例测试隔离；插件不在用例之间重置累计诊断。"""
    global _LAST
    configure(strict=False)
    _LOCAL.held = []
    with _STATS_LOCK:
        for name in _COUNTS:
            _COUNTS[name] = 0
        _LAST = {}


def held_locks() -> tuple[HeldLock, ...]:
    return tuple(getattr(_LOCAL, "held", ()))


def _stack() -> tuple[str, ...]:
    # 诊断只保留函数和行号，绝不暴露文件路径、锁路径或局部内容。
    return tuple(f"{frame.name}:{frame.lineno}" for frame in traceback.extract_stack()[:-1])


def _violation(reason: str, level, held: HeldLock | None = None) -> None:
    global _LAST
    detail = {"reason": reason, "level": int(level) if level is not None else None,
              "held_level": int(held.level) if held is not None and held.level is not None else None,
              "acquire_stack": _stack(), "held_stack": held.stack if held is not None else ()}
    with _STATS_LOCK:
        _COUNTS["violations"] += 1
        _LAST = detail
    if _STRICT:
        raise LockOrderViolation(str(detail))


def check_order(level, lock_id: int, *, try_acquire: bool) -> None:
    if try_acquire or level is None:
        return
    entries = held_locks()
    if any(entry.lock_id == lock_id for entry in entries):
        return
    other = max((entry for entry in entries if entry.level is not None),
                key=lambda entry: entry.level, default=None)
    if other is not None and level <= other.level:
        _violation("blocking_lock_order", level, other)


def note_acquired(level, lock_id: int, *, try_acquire: bool) -> None:
    check_order(level, lock_id, try_acquire=try_acquire)
    entries = getattr(_LOCAL, "held", None)
    if entries is None:
        entries = _LOCAL.held = []
    entry = HeldLock(lock_id, level, try_acquire, _stack() if _STRICT else ())
    if _STRICT:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            if asyncio.current_task() is not None:
                entry.yield_check = loop.call_soon(entry.yielded.__setitem__, 0, True)
    entries.append(entry)
    with _STATS_LOCK:
        _COUNTS["acquisitions"] += 1
        _COUNTS["tries"] += int(try_acquire)
        _COUNTS["uncategorised"] += int(level is None)


def note_released(lock_id: int) -> None:
    entries = getattr(_LOCAL, "held", [])
    for index in range(len(entries) - 1, -1, -1):
        if entries[index].lock_id == lock_id:
            entry = entries.pop(index)
            if entry.yield_check is not None:
                entry.yield_check.cancel()
            if entry.yielded[0]:
                _violation("lock_held_across_await", entry.level, entry)
            return


def lockdep_snapshot() -> dict:
    with _STATS_LOCK:
        return {**_COUNTS, "last_violations": dict(_LAST)}


@contextmanager
def ordered_lock(level, lock_obj, *, blocking=True):
    # 严格模式先检查再等待，避免测试先真的死锁；生产只对成功获取计数。
    if _STRICT:
        check_order(level, id(lock_obj), try_acquire=not blocking)
    acquired = lock_obj.acquire(blocking=blocking)
    tracked = False
    try:
        if acquired:
            note_acquired(level, id(lock_obj), try_acquire=not blocking)
            tracked = True
        yield acquired
    finally:
        if acquired:
            try:
                if tracked:
                    note_released(id(lock_obj))
            finally:
                lock_obj.release()
