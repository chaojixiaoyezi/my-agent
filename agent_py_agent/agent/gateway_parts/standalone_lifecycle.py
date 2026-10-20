# LLM: Count0只持同一canonical生命周期文件锁，原scope固定实际handle/FD；不登记C/W或签迁移许可，有限停机观察仍unknown。
# 模块用途: 防止单进程与多进程同时占用同一Gateway，并在原state事务中保留有限停机事实。
from __future__ import annotations

import json
import os
import stat
from contextlib import contextmanager
from pathlib import Path

from ..common.nofollow_fs import read_bytes_beneath
from ..common.strict_json import load_strict_json
from .io import (
    _flock_unlock,
    _open_private_lock_descriptor,
    _try_flock_exclusive,
    update_json_file_atomic,
)

# 只由实际guard登记原capture；公开handle属性不是可替换的锁来源。
_HELD = {}


# LLM: 该异常只表示真实锁争用或身份丢失，不能从它推断旧服务已排空。
# 类用途: 让公开入口明确拒绝重叠生命周期，避免启动第二个Gateway。
class StandaloneLifecycleUnavailable(RuntimeError):
    error_code = "GATEWAY_LIFECYCLE_UNAVAILABLE"


# LLM: 对象只能由standalone_lifecycle_guard出生；每次使用须与scope固定的原handle/FD相同，不授予MP或业务执行权。
# 类用途: 保存这次单进程服务实际持有的文件锁。
class StandaloneLifecycle:
    # LLM: 只保存已成功取锁的真实句柄，不从JSON或PID文件恢复持锁能力。
    # 函数用途: 捕获原句柄、路径与持锁进程，供最终状态事务复核。
    def __init__(self, handle, path: Path):
        self.handle, self.path, self.pid = handle, path, os.getpid()
        info = os.fstat(handle.fileno())
        self.signature = (info.st_dev, info.st_ino)

    # LLM: 每次末写先核原scope捕获的handle对象和FD仍有效，再核进程、no-follow路径与inode；同inode替换句柄也拒绝。
    # 函数用途: 确认当前服务仍持有自己最初取得的生命周期锁。
    def require_held(self) -> None:
        captured = _HELD.get(self)
        if captured is None:
            raise StandaloneLifecycleUnavailable("单进程生命周期锁已失效。")
        original_handle, original_descriptor = captured
        if (self.handle is not original_handle or original_handle.closed
                or self.pid != os.getpid() or original_handle.fileno() != original_descriptor):
            raise StandaloneLifecycleUnavailable("单进程生命周期锁已失效。")
        info = self.path.lstat()
        current = os.fstat(original_descriptor)
        if (not stat.S_ISREG(info.st_mode)
                or (info.st_dev, info.st_ino) != self.signature
                or (current.st_dev, current.st_ino) != self.signature):
            raise StandaloneLifecycleUnavailable("单进程生命周期锁路径已变化。")


# LLM: 与coordinator_guard共用canonical锁，原handle/FD只由此scope捕获；finally只释放该原资源，已close不再unlock，替换FD仍归调用方。
# 函数用途: 在创建Agent之前独占单进程生命周期；异常或属性替换都不能妨碍原锁释放或误关其它句柄。
@contextmanager
def standalone_lifecycle_guard(root: Path):
    path = Path(os.path.abspath(root)) / "coordinator.lock"
    descriptor = _open_private_lock_descriptor(path)
    os.set_inheritable(descriptor, False)
    handle = os.fdopen(descriptor, "r+", encoding="utf-8")
    acquired = False
    lifecycle = StandaloneLifecycle(handle, path)
    try:
        acquired = _try_flock_exclusive(handle)
        if not acquired:
            raise StandaloneLifecycleUnavailable("同一Gateway已有生命周期持锁者。")
        _HELD[lifecycle] = (handle, descriptor)
        lifecycle.require_held()
        yield lifecycle
    finally:
        _HELD.pop(lifecycle, None)
        try:
            if acquired and not handle.closed:
                _flock_unlock(handle)
        finally:
            handle.close()


# LLM: 仅在正常停止及原cleanup之后调用；原JSON事务内CAS完整前像及本代身份，有限观察永不签complete或旧MP闭封。
# 函数用途: 在现有state内保留真实启动身份和已观察循环结果，明确未接线责任库存仍未知。
def record_standalone_stop_observation(lifecycle, context, cleanup: dict) -> dict:
    lifecycle.require_held()
    if (context.control_plane is not None
            or type(context.coordinator_worker_count) is not int or context.coordinator_worker_count != 0):
        raise ValueError("单进程停机观察不能用于多进程代次。")
    identity = dict(context.process_identity)
    if not identity or identity.get("pid") != os.getpid():
        raise ValueError("缺少本次服务的真实启动身份。")
    raw = read_bytes_beneath(context.paths.root, ("gateway_state.json",), max_bytes=1024 * 1024)
    before = load_strict_json(raw) if raw is not None else None
    if type(before) is not dict:
        raise ValueError("单进程终止状态不可读取。")
    if before.get("status") != "stopped" or before.get("process_identity") != identity:
        raise ValueError("单进程终止状态不属于当前正常停止代次。")
    expected = json.dumps(before, sort_keys=True, ensure_ascii=False, allow_nan=False)

    # LLM: updater在原state文件锁内执行；变化或锁身份丢失必须拒绝，不覆盖后继者状态。
    # 函数用途: 将有限停机观察与完整前像做一次CAS，不新建业务账。
    def update(current):
        lifecycle.require_held()
        if (read_bytes_beneath(context.paths.root, ("gateway_state.json",), max_bytes=1024 * 1024) != raw
                or json.dumps(current, sort_keys=True, ensure_ascii=False, allow_nan=False) != expected):
            raise ValueError("单进程终止状态已变化。")
        return {**current, "standalone_shutdown_observation": {
            "schema": "gateway_standalone_shutdown_observation.v1",
            "process_identity": identity,
            "process_started_at": context.process_started_at,
            "worker_count": 0,
            "gateway_loops_joined": cleanup.get("drain_complete") is True,
            "complete": False,
            "unobserved_responsibilities": [
                "foreground", "compression", "control", "subagent", "background",
                "executor_tree", "external_producers",
            ],
        }}

    return update_json_file_atomic(context.paths.state, update, require_existing=True)
