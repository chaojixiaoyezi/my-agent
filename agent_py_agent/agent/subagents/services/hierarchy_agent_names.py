# LLM: Hierarchy agent-name helpers keep user-facing lineage labels deterministic.
# 模块用途: 根据层级 depth 统一生成“小傻妞/小小傻妞/...”前缀，避免模型手写名字漂移。

from __future__ import annotations

from typing import Any

from ..models import SubAgentTask


# LLM: scheduled_child_agent_name applies the depth prefix and sibling id contract.
# 函数用途: 按 parent.depth + 1 生成子节点展示名；默认名会变成 小小傻妞-role-index，避免模型手写漂移。
def scheduled_child_agent_name(parent: SubAgentTask, spec: Any, *, sibling_index: int = 1) -> str:
    depth = max(1, int(parent.depth or 0) + 1, _parent_visible_lineage_depth(parent) + 1)
    raw_name = str(getattr(spec, "agent_name", "") or "").strip().strip("-")
    source = _agent_name_source(spec)
    suffix = _agent_name_suffix(source, fallback=getattr(spec, "role", "worker"))
    if _needs_role_index_repair(raw_name) and not _has_trailing_identifier(suffix):
        suffix = f"{suffix}-{max(1, int(sibling_index or 1))}"
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


# LLM: _agent_name_source prefers role when the model only supplied a default placeholder name.
# 函数用途: HierarchyChildSpec.agent_name 默认是 worker；role=tester 时不能因此生成 worker 名字。
def _agent_name_source(spec: Any) -> str:
    name = str(getattr(spec, "agent_name", "") or "").strip().strip("-")
    role = str(getattr(spec, "role", "") or "worker").strip().strip("-") or "worker"
    if _needs_role_index_repair(name):
        return role
    return name


# LLM: _needs_role_index_repair marks generated or prefix-only names that need role/index repair.
# 函数用途: 空名、worker/general、小小傻妞 这种半截名都由系统补成 role-index。
def _needs_role_index_repair(value: str) -> bool:
    text = str(value or "").strip().strip("-")
    return text in {"", "worker", "general", "subagent", "agent", "child"} or _is_prefix_only_name(text)


# LLM: _is_prefix_only_name distinguishes 小小傻妞 from 小小傻妞-product-worker.
# 函数用途: 只有没有后缀的层级前缀需要回退 role；已有语义后缀不能被吞掉。
def _is_prefix_only_name(value: str) -> bool:
    text = str(value or "").strip().strip("-")
    return "-" not in text and _has_lineage_prefix(text)


# LLM: _has_trailing_identifier keeps repeated scheduling from growing name-1-1 chains.
# 函数用途: 识别已有数字编号，避免同一 sibling 名字被重复追加编号。
def _has_trailing_identifier(value: str) -> bool:
    return str(value or "").strip().rsplit("-", 1)[-1].isdigit()


# LLM: _has_lineage_prefix recognizes 小傻妞 / 小小傻妞 style prefixes.
# 函数用途: 判断 agent_name 是否已经带层级中文前缀，用于去重和改层级。
def _has_lineage_prefix(value: str) -> bool:
    prefix = value.split("-", 1)[0]
    return len(prefix) >= 2 and prefix.endswith("傻妞") and set(prefix[:-2]) == {"小"}


# LLM: _parent_visible_lineage_depth keeps display names monotonic when root already has a lineage prefix.
# 函数用途: 从父节点 agent_name 读取“小傻妞/小小傻妞”层数；没有前缀时返回 0，让 depth 继续兜底。
def _parent_visible_lineage_depth(parent: SubAgentTask) -> int:
    prefix = str(getattr(parent, "agent_name", "") or "").split("-", 1)[0]
    if not _has_lineage_prefix(prefix):
        return 0
    return max(1, len(prefix) - len("傻妞"))


# LLM: _is_placeholder_suffix repairs literal template markers before they become user-facing agent names.
# 函数用途: 判断模型是否把“小傻妞-*”里的星号占位符原样当成名字；命中时改用 role 兜底。
def _is_placeholder_suffix(value: str) -> bool:
    text = str(value or "").strip().strip("-")
    return not text or "*" in text
