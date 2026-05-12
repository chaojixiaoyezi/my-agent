# LLM: Subagent orchestration helper; keep pending capability status semantics shared across parser, policy, and acceptance.
# 模块用途: 统一判断子代理结构化状态是否还在等待工具、skill、shell、MCP 或其他能力授权。

from __future__ import annotations

"""Shared pending-capability status helpers."""


# LLM: is_pending_capability_status catches model-written "needs tool" states before routing or acceptance.
# 函数用途: 判断结构化状态是否还在等待工具/skill/shell/MCP 等能力；这种状态必须阻塞，不能进入验收。
def is_pending_capability_status(status: str) -> bool:
    normalized = str(status or "").upper().strip()
    if normalized in _EXPLICIT_PENDING_CAPABILITY_STATUSES:
        return True
    return _contains_waiting_and_capability_terms(normalized)


# LLM: _contains_waiting_and_capability_terms handles model variants without widening to all PENDING states.
# 函数用途: 只在状态同时包含“等待/需要”和“能力/工具”两类词时判断为等待授权。
def _contains_waiting_and_capability_terms(normalized: str) -> bool:
    waiting_terms = ("PENDING", "NEED", "NEEDS", "WAIT", "WAITING", "REQUEST", "MISSING")
    capability_terms = ("CAPABILITY", "TOOL", "SKILL", "SHELL", "MCP")
    return any(term in normalized for term in waiting_terms) and any(
        term in normalized for term in capability_terms
    )


_EXPLICIT_PENDING_CAPABILITY_STATUSES = {
    "PENDING_CAPABILITY_REQUEST",
    "PENDING_CAPABILITY",
    "NEEDS_CAPABILITY",
    "NEED_CAPABILITY",
    "WAITING_FOR_CAPABILITY",
    "AWAITING_CAPABILITY",
    "CAPABILITY_REQUEST",
    "CAPABILITY_REQUESTED",
    "NEEDS_TOOL",
    "NEED_TOOL",
    "WAITING_FOR_TOOL",
    "NEEDS_SKILL",
    "NEEDS_SHELL",
    "NEEDS_MCP",
}
