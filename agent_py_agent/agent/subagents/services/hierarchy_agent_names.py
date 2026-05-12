# LLM: Hierarchy agent-name helpers keep user-facing lineage labels deterministic.
# 模块用途: 根据层级 depth 统一生成“小傻妞/小小傻妞/...”前缀，避免模型手写名字漂移。

from __future__ import annotations

from typing import Any

from ..models import SubAgentTask


# LLM: scheduled_child_agent_name applies the depth prefix while preserving the semantic suffix.
# 函数用途: 按 parent.depth + 1 生成子节点展示名；会去掉已有同类前缀，避免重复叠前缀。
def scheduled_child_agent_name(parent: SubAgentTask, spec: Any) -> str:
    depth = max(1, int(parent.depth or 0) + 1)
    suffix = _agent_name_suffix(spec.agent_name or spec.role or "worker", fallback=spec.role)
    return f"{_lineage_prefix(depth)}-{suffix}"


# LLM: _lineage_prefix renders the Chinese lineage marker for arbitrary depth.
# 函数用途: depth=1 生成 小傻妞，depth=2 生成 小小傻妞，更深继续追加“小”。
def _lineage_prefix(depth: int) -> str:
    return f"{'小' * max(1, depth)}傻妞"


# LLM: _agent_name_suffix removes old lineage prefixes and repairs bare prefix-only model names.
# 函数用途: 保留 catalog-lead 这类专业辨识后缀；模型只写“小小傻妞”无后缀时，回退到 role 作为后缀。
def _agent_name_suffix(value: str, fallback: str = "worker") -> str:
    text = str(value or "").strip().strip("-") or "worker"
    fallback_text = str(fallback or "").strip().strip("-") or "worker"
    while _has_lineage_prefix(text):
        if "-" not in text:
            return "worker" if _has_lineage_prefix(fallback_text) else fallback_text
        text = text.split("-", 1)[1].strip().strip("-") or "worker"
    if _is_placeholder_suffix(text):
        return "worker" if _has_lineage_prefix(fallback_text) else fallback_text
    return text


# LLM: _has_lineage_prefix recognizes 小傻妞 / 小小傻妞 style prefixes.
# 函数用途: 判断 agent_name 是否已经带层级中文前缀，用于去重和改层级。
def _has_lineage_prefix(value: str) -> bool:
    prefix = value.split("-", 1)[0]
    return len(prefix) >= 2 and prefix.endswith("傻妞") and set(prefix[:-2]) == {"小"}


# LLM: _is_placeholder_suffix repairs literal template markers before they become user-facing agent names.
# 函数用途: 判断模型是否把“小傻妞-*”里的星号占位符原样当成名字；命中时改用 role 兜底。
def _is_placeholder_suffix(value: str) -> bool:
    text = str(value or "").strip().strip("-")
    return not text or "*" in text
