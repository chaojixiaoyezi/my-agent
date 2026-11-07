# LLM: 子代理结构和代次变化共用原创建文件锁；只允许当前线程/进程重入同一 canonical 路径，不保存生命周期状态。
# 模块用途: 为创建、换轮和控制的嵌套短事务提供同一把锁，模型调用和进程等待必须在锁外。
from __future__ import annotations

import os
import threading
from contextlib import ExitStack, contextmanager
from pathlib import Path

from ..common.json_io import locked_json_path
from ..common.runtime_gate_registry import (
    SubagentCreationGuardBusy,
    current_thread_holds_runtime_gate,
)
from ..concurrency.lock_order import LockLevel

_HELD_GUARDS = threading.local()


# LLM: 同线程/PID 重入不变；首次获取若已持任一 M/L 则 CG 只 try，竞争异常在 yield 前转换，业务体异常原样传播。
# 函数用途: 在原创建锁上执行短事务，避免协调者持门等待 CG 形成 AB-BA 环。
@contextmanager
def subagent_creation_guard(workspace: str | Path):
    path = (Path(workspace) / ".create-subagents.guard").resolve()
    key = (os.getpid(), str(path))
    held = getattr(_HELD_GUARDS, "paths", None)
    if held is None:
        held = _HELD_GUARDS.paths = set()
    if key in held:
        yield
        return

    with ExitStack() as stack:
        if current_thread_holds_runtime_gate():
            try:
                stack.enter_context(locked_json_path(path, blocking=False, category=LockLevel.CG))
            except BlockingIOError as error:
                raise SubagentCreationGuardBusy(str(path)) from error
        else:
            stack.enter_context(locked_json_path(path, category=LockLevel.CG))
        held.add(key)
        try:
            yield
        finally:
            held.remove(key)
