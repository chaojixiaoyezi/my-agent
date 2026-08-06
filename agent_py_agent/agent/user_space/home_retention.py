from __future__ import annotations

"""旧 home-retention import 路径对统一 Memory v2 retention Service 的只读导出。"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..memory_store.retention import (
        MemoryRetentionService,
        OwnerRetentionPlan,
        RetentionAction,
        apply_owner_retention,
        plan_owner_retention,
    )

# LLM: 本模块不得恢复旧 raw_days/hooks_days scanner；所有调用都由 memory_store.retention 的唯一 Service 执行。
# 模块用途: 保持既有 Gateway maintenance 和 CLI import 可用，同时消除重复 retention 实现。
# 循环导入根修: user_space/__init__ 顶层 import 本模块，而 memory_store.daily 又 import
#   user_space.owner_quota → 包加载期 memory_store ↔ user_space 互相触发。用 PEP 562 延迟重导出：
#   包加载期不 import memory_store，只有真正访问 retention 符号时才解析，打破回环且接口不变。

__all__ = [
    "MemoryRetentionService",
    "OwnerRetentionPlan",
    "RetentionAction",
    "apply_owner_retention",
    "plan_owner_retention",
]


def __getattr__(name: str):
    if name in __all__:
        from ..memory_store.retention import (
            MemoryRetentionService,
            OwnerRetentionPlan,
            RetentionAction,
            apply_owner_retention,
            plan_owner_retention,
        )

        return {
            "MemoryRetentionService": MemoryRetentionService,
            "OwnerRetentionPlan": OwnerRetentionPlan,
            "RetentionAction": RetentionAction,
            "apply_owner_retention": apply_owner_retention,
            "plan_owner_retention": plan_owner_retention,
        }[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
