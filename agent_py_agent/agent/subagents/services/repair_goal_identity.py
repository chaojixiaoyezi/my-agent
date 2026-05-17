# LLM: Repair identity no longer falls back to natural-language goal parsing.
# 模块用途: 保留旧 API，但修复任务复用必须依赖 repair_contract/context_packs 等结构化字段。

from __future__ import annotations

from typing import Any


# LLM: repair_goal_targets intentionally returns no natural-language fallback targets.
# 函数用途: 防止代码层从“修复/补齐/fix”等自然语言里猜 repair owner；调用方应使用 repair_contract。
def repair_goal_targets(value: Any) -> tuple[str, ...]:
    return ()


# LLM: repair_goal_targets_overlap compares structured fallback tuples when legacy callers still pass them.
# 函数用途: 兼容旧调用方；由于 repair_goal_targets 不再生成目标，正常情况下返回 False。
def repair_goal_targets_overlap(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    return bool(set(left) & set(right))


__all__ = ["repair_goal_targets", "repair_goal_targets_overlap"]
