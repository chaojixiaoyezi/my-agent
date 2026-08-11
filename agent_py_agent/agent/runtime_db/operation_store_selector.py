"""B 切片：OperationStore 选择 seam（seq 241 定版 + seq 245 P1 修正）。

- 签名 select_operation_store(agent)：authority 字段挂 agent.subagents
  （SubAgentManager 形态：runtime_db + execution_mode；SimpleAgent.subagents
  恒存在，execution_mode 由 _attach_runtime_db 落枚举）。
- 顺序（seq 241 问题 2）：先严格校验 ExecutionMode（非枚举 → TypeError，
  fail-closed，字符串拼错不许被静默吞掉）→ MANAGED → ManagedOperationStore
  （runtime.db adapter）；LOCAL_UNMANAGED → agent.local_store。
- 绝不双写：MANAGED 绝不落 local 账本；LOCAL 绝不碰 runtime.db。
- 幂等（seq 245 P1）：store 作为 agent 实例字段一次性构造注入（composition
  root）——`_operation_store` 属性有则直接返回（同一执行只选一次，b2 断言），
  无则构造并 setattr。绝不使用模块级 {id(agent): store} 缓存：旧 agent 回收后
  id() 可被新对象复用，全局缓存会把旧 owner 的 store 串给新 agent
  （跨 owner 污染）。
"""

from __future__ import annotations

from typing import Any

from .execution_mode import ExecutionMode
from .managed_operation_store import ManagedOperationStore

_STORE_ATTR = "_operation_store"


def select_operation_store(agent: object) -> object:
    """按 agent 的显式执行模式选一次 OperationStore（幂等）。

    非 ExecutionMode 枚举 → TypeError（fail-closed，seq 238 问题 2）。
    store 挂在 agent 实例字段（`_operation_store`）：agent 存活期内幂等复用，
    agent 回收后随对象一起释放，天然不跨 owner。
    """
    subagents = getattr(agent, "subagents", None)
    mode = getattr(subagents, "execution_mode", None)
    if not isinstance(mode, ExecutionMode):
        raise TypeError(f"execution_mode 必须是 ExecutionMode 枚举成员，got {mode!r}")
    existing = getattr(agent, _STORE_ATTR, None)
    if existing is not None:
        return existing
    if mode is ExecutionMode.MANAGED:
        repo = getattr(subagents, "runtime_db", None)
        store: Any = ManagedOperationStore(repo)
    else:
        store = getattr(agent, "local_store", None)
    setattr(agent, _STORE_ATTR, store)
    return store


__all__ = ["select_operation_store"]
