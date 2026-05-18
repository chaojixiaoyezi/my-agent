# LLM: Hierarchy capability contracts keep tool/shell safety terms reusable across delegation layers.
# 模块用途: 提取 controlled_exec、capability_request、trash 等不能在父子代理转述中丢失的能力合同。

from __future__ import annotations

import re


# LLM: capability_contract_segments extracts non-droppable tool/shell safety requirements.
# 函数用途: 把 controlled_exec 授权、命令范围、输出外置和 trash 行为作为硬继承合同传给下层。
def capability_contract_segments(text: str) -> list[str]:
    keywords = _capability_contract_keywords()
    return [
        _clip_contract(segment, limit=500)
        for segment in _contract_segments(text)
        if any(keyword in segment.lower() for keyword in keywords)
    ]


# LLM: capability_contract_terms returns stable anchors for goal completeness checks.
# 函数用途: 提取父级能力合同关键词，判断 child goal 是否已经完整携带。
def capability_contract_terms(text: str) -> list[str]:
    lowered = str(text or "").lower()
    return [keyword for keyword in _capability_contract_keywords() if keyword in lowered]


# LLM: _capability_contract_keywords centralizes hard delegation terms for future tool gateways.
# 函数用途: 集中维护不能在层级转述中丢失的能力/工具/安全字段名。
def _capability_contract_keywords() -> tuple[str, ...]:
    return (
        "controlled_exec",
        "capability_request",
        "requested_tools",
        "requested_commands",
        "path_scope",
        "output_budget",
        "task_trash",
        "move_to_task_trash",
        "stdout_ref",
        "audit_ref",
        "trash_manifest_ref",
        "grant",
    )


# LLM: _contract_segments keeps capability extraction bounded to short clauses.
# 函数用途: 按常见中英文标点切开父级目标，避免一条工具合同拖进整段 sibling 目标。
def _contract_segments(text: str) -> list[str]:
    return [item.strip() for item in re.split(r"[\n。；;]+", str(text or "")) if item.strip()]


# LLM: _clip_contract bounds inherited capability text in deep trees.
# 函数用途: 保留能力合同开头，防止多层继承时 prompt 无限膨胀。
def _clip_contract(text: str, *, limit: int) -> str:
    compact = str(text or "").strip()
    if len(compact) <= limit:
        return compact
    return compact[:limit].rstrip() + "...[truncated]"
