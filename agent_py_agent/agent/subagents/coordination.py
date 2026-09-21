# LLM: 子代理结构和代次变化共用原创建文件锁；只允许当前线程/进程重入同一 canonical 路径，不保存生命周期状态。
# 模块用途: 为创建、换轮和控制的嵌套短事务提供同一把锁，模型调用和进程等待必须在锁外。
from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from pathlib import Path

from ..common.json_io import locked_json_path

_HELD_GUARDS = threading.local()


# LLM: 线程局部持有集合仅消除同一原锁的递归获取；PID 防止 fork 后借用父进程的持锁事实，finally 清理不留下 owner 缓存。
# 函数用途: 在原创建锁上执行短事务，允许同一调用链经过多个正式服务入口而不重复锁住自己。
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
    with locked_json_path(path):
        held.add(key)
        try:
            yield
        finally:
            held.remove(key)
