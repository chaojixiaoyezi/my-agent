
from __future__ import annotations

from typing import Any

from ...models import SubAgentTask


def scheduled_child_agent_name(parent: SubAgentTask, spec: Any, *, sibling_index: int = 1) -> str:
    depth = max(1, int(parent.depth or 0) + 1, _parent_visible_lineage_depth(parent) + 1)
    raw_name = str(getattr(spec, "agent_name", "") or "").strip().strip("-")
    source = _agent_name_source(spec)
    suffix = _agent_name_suffix(source, default=getattr(spec, "role", "worker"))
    if _needs_role_index_repair(raw_name) and not _has_trailing_identifier(suffix):
        suffix = f"{suffix}-{max(1, int(sibling_index or 1))}"
    return f"{_lineage_prefix(depth)}-{suffix}"


def _lineage_prefix(depth: int) -> str:
    return f"{'小' * max(1, depth)}傻妞"


def _agent_name_suffix(value: str, default: str = "worker") -> str:
    text = str(value or "").strip().strip("-") or "worker"
    default_text = str(default or "").strip().strip("-") or "worker"
    while _has_lineage_prefix(text):
        if "-" not in text:
            return "worker" if _has_lineage_prefix(default_text) else default_text
        text = text.split("-", 1)[1].strip().strip("-") or "worker"
    if _is_placeholder_suffix(text):
        return "worker" if _has_lineage_prefix(default_text) else default_text
    return text


def _agent_name_source(spec: Any) -> str:
    name = str(getattr(spec, "agent_name", "") or "").strip().strip("-")
    role = str(getattr(spec, "role", "") or "worker").strip().strip("-") or "worker"
    if _needs_role_index_repair(name):
        return role
    return name


def _needs_role_index_repair(value: str) -> bool:
    text = str(value or "").strip().strip("-")
    return text in {"", "worker", "general", "subagent", "agent", "child"} or _is_prefix_only_name(text)


def _is_prefix_only_name(value: str) -> bool:
    text = str(value or "").strip().strip("-")
    return "-" not in text and _has_lineage_prefix(text)


def _has_trailing_identifier(value: str) -> bool:
    return str(value or "").strip().rsplit("-", 1)[-1].isdigit()


def _has_lineage_prefix(value: str) -> bool:
    prefix = value.split("-", 1)[0]
    return len(prefix) >= 2 and prefix.endswith("傻妞") and set(prefix[:-2]) == {"小"}


def _parent_visible_lineage_depth(parent: SubAgentTask) -> int:
    prefix = str(getattr(parent, "agent_name", "") or "").split("-", 1)[0]
    if not _has_lineage_prefix(prefix):
        return 0
    return max(1, len(prefix) - len("傻妞"))


def _is_placeholder_suffix(value: str) -> bool:
    text = str(value or "").strip().strip("-")
    return not text or "*" in text
