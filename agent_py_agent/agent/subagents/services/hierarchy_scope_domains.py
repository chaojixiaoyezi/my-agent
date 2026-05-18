# LLM: Hierarchy scope-domain helpers isolate sibling-domain checks from lifecycle guards.
# 模块用途: 处理 forbidden_child_scopes 和 parent/child 领域匹配，避免 scope guard 主文件继续变厚。

from __future__ import annotations

import re
from typing import Any

from ..models import SubAgentTask
from .hierarchy_domain_terms import DOMAIN_STOPWORDS, FORBIDDEN_SCOPE_GENERIC_TERMS


# LLM: forbidden_child_scope_reason enforces explicit sibling exclusions before children are persisted.
# 函数用途: parent goal 只有写入 forbidden_child_scopes 机器字段时才阻断下一层任务。
def forbidden_child_scope_reason(parent: SubAgentTask, request: Any) -> str:
    for term in _forbidden_scope_terms(parent):
        if _request_contains_scope_term(request.child_specs, term):
            return f"forbidden_child_scope:{term}"
    return ""


# LLM: domain_mismatch_reason enforces parent-owned domain scopes without relying on prose memory.
# 函数用途: text-lead 只能创建 text 子任务；如果 child 明确变成 arithmetic，则阻断错误 sibling 漂移。
def domain_mismatch_reason(parent: SubAgentTask, request: Any) -> str:
    if int(parent.depth or 0) <= 0:
        return ""
    parent_domains = _parent_domain_terms(parent)
    if not parent_domains:
        return ""
    for spec in request.child_specs:
        child_domains = _child_domain_terms(spec)
        mismatch = sorted(child_domains - parent_domains)
        if mismatch:
            parent_domain = sorted(parent_domains)[0]
            return f"domain_mismatch:{parent_domain}->{mismatch[0]}"
    return ""


# LLM: _request_contains_scope_term keeps matching scoped to each requested child spec.
# 函数用途: 检查 child spec 自身文本是否包含被禁止的 sibling 领域词。
def _request_contains_scope_term(child_specs: list[Any], term: str) -> bool:
    return bool(term) and any(term in _child_scope_terms(spec) for spec in child_specs)


# LLM: _forbidden_scope_terms extracts machine-readable forbidden child scopes.
# 函数用途: 只从 attributes.forbidden_child_scopes 提取领域名，不读 goal/thought/debug note。
def _forbidden_scope_terms(parent: SubAgentTask) -> list[str]:
    if int(parent.depth or 0) <= 0:
        return []
    terms = _scope_attribute_terms(parent, "forbidden_child_scopes")
    return list(
        dict.fromkeys(
            term
            for raw in terms
            if (term := raw.strip("_-")) and term not in FORBIDDEN_SCOPE_GENERIC_TERMS
        )
    )


# LLM: _parent_domain_terms extracts explicit coordinator domain labels from role/name and attributes.
# 函数用途: 从 role/agent_name 和 attributes.domain_scopes 提取父级负责领域。
def _parent_domain_terms(parent: SubAgentTask) -> set[str]:
    label_text = f"{parent.agent_name} {parent.role}".lower()
    return _domain_refs(label_text) | set(_scope_attribute_terms(parent, "domain_scopes"))


# LLM: _child_domain_terms extracts requested child domains from labels first, then attributes.
# 函数用途: 判断 child spec 是否声明了其它领域，避免共享路径词造成误判。
def _child_domain_terms(spec: Any) -> set[str]:
    label_domains = _domain_refs(f"{getattr(spec, 'agent_name', '')} {getattr(spec, 'role', '')}".lower())
    if label_domains:
        return label_domains
    return set(_scope_attribute_terms(spec, "domain_scopes"))


# LLM: _child_scope_terms combines role/name scopes with explicit attribute scopes only.
# 函数用途: 供 forbidden_child_scopes 检查使用；普通 child goal 句子不会产生 scope。
def _child_scope_terms(spec: Any) -> set[str]:
    return _child_domain_terms(spec)


# LLM: _domain_refs pulls domain tokens from leaf_worker_<domain> and <domain>-lead patterns.
# 函数用途: 读取常见层级任务命名，不用整句自然语言做业务判断。
def _domain_refs(text: str) -> set[str]:
    refs: set[str] = set()
    for pattern in (r"leaf[_-]worker[_-]([a-z][a-z0-9_-]+)", r"\b([a-z][a-z0-9_-]+)[_-]lead\b"):
        refs.update(match.group(1) for match in re.finditer(pattern, text))
    return {term for term in refs if term not in DOMAIN_STOPWORDS}


# LLM: _scope_attribute_terms reads scope lists from attributes only.
# 函数用途: 支持 task/spec attributes 中的字符串、列表或元组 scope；不解析 goal 句子。
def _scope_attribute_terms(item: Any, field_name: str) -> list[str]:
    attrs = getattr(item, "attributes", {}) or {}
    if not isinstance(attrs, dict):
        return []
    return _scope_items(attrs.get(field_name))


# LLM: _scope_items tokenizes one structured scope value.
# 函数用途: 读取英文/标识符 scope，不接受普通中文句子。
def _scope_items(value: object) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        return [term for item in value for term in _scope_items(item)]
    return [
        item
        for item in re.split(r"[\s,，、|/]+", str(value or ""))
        if re.fullmatch(r"[a-zA-Z0-9_-]+", item or "")
    ]
