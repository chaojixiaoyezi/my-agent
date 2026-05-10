# LLM: Hierarchy scope guards keep child planning from drifting into sibling domains.
# 模块用途: 判断层级调度是否越界、超过数量/深度或创建了错误领域的 child。

from __future__ import annotations

import re
from typing import Any

from ..models import SubAgentTask


# LLM: schedule_block_reason keeps guard checks deterministic and side-effect free.
# 函数用途: 判断本轮层级调度是否因深度、数量、空计划或领域越界被阻断。
def schedule_block_reason(parent: SubAgentTask, request: Any) -> str:
    if not request.child_specs:
        return "no_child_specs"
    if parent.depth + 1 > request.max_depth:
        return f"max_depth_exceeded:{request.max_depth}"
    if request.max_children > 0 and len(parent.child_ids) + len(request.child_specs) > request.max_children:
        return f"max_children_exceeded:{request.max_children}"
    return _forbidden_child_scope_reason(parent, request) or _domain_mismatch_reason(parent, request)


# LLM: _forbidden_child_scope_reason enforces explicit sibling exclusions before bad children are persisted.
# 函数用途: parent goal 写明“不得创建 X”时，阻断包含 X 的下一层任务，避免错误领域 leaf 落盘。
def _forbidden_child_scope_reason(parent: SubAgentTask, request: Any) -> str:
    for term in _forbidden_scope_terms(parent):
        if _request_contains_scope_term(request.child_specs, term):
            return f"forbidden_child_scope:{term}"
    return ""


# LLM: _request_contains_scope_term keeps matching scoped to each requested child spec.
# 函数用途: 检查 child spec 自身文本是否包含被禁止的 sibling 领域词。
def _request_contains_scope_term(child_specs: list[Any], term: str) -> bool:
    return bool(term) and any(term in _child_scope_text(spec) for spec in child_specs)


# LLM: _forbidden_scope_terms extracts short machine-readable domain words from parent instructions.
# 函数用途: 从 parent goal 中提取不得创建的英文/标识符领域名，如 arithmetic/text。
def _forbidden_scope_terms(parent: SubAgentTask) -> list[str]:
    if int(parent.depth or 0) <= 0:
        return []
    text = str(parent.goal or "").lower()
    terms = re.findall(r"(?:不得|不能|不要)\s*创建\s*([a-zA-Z0-9_-]+)", text)
    return list(dict.fromkeys(term.strip("_-") for term in terms if term.strip("_-")))


# LLM: _child_scope_text keeps forbidden-scope matching limited to the requested child spec.
# 函数用途: 合并 child 的 goal/agent_name/role，避免拿补全后的父级上下文误判。
def _child_scope_text(spec: Any) -> str:
    return f"{spec.goal} {spec.agent_name} {spec.role}".lower()


# LLM: _domain_mismatch_reason blocks child coordinators from drifting into sibling domains.
# 函数用途: parent 已经是 text/arithmetic 等单一领域时，阻断创建其它领域 child。
def _domain_mismatch_reason(parent: SubAgentTask, request: Any) -> str:
    parent_domains = _domain_terms(f"{parent.goal} {parent.agent_name} {parent.role}")
    if int(parent.depth or 0) <= 0 or not parent_domains:
        return ""
    return _first_domain_mismatch(parent_domains, request.child_specs)


# LLM: _first_domain_mismatch returns the first blocked sibling drift message.
# 函数用途: 按 child specs 顺序找第一个领域串线问题，保持错误信息稳定。
def _first_domain_mismatch(parent_domains: set[str], child_specs: list[Any]) -> str:
    for spec in child_specs:
        child_domains = _domain_terms(_child_scope_text(spec))
        if child_domains and parent_domains.isdisjoint(child_domains):
            return f"domain_mismatch:{','.join(sorted(parent_domains))}->{','.join(sorted(child_domains))}"
    return ""


# LLM: _domain_terms extracts stable task-domain words without treating every filename as a domain.
# 函数用途: 从 agent_name、leaf_worker_x、normalize_text 等命名里提取短领域词。
def _domain_terms(text: str) -> set[str]:
    lowered = str(text or "").lower()
    terms = set(re.findall(r"(?:leaf_worker|leaf-worker|leaf|lead)[_-]([a-z][a-z0-9_-]*)", lowered))
    if "arithmetic" in lowered:
        terms.add("arithmetic")
    if re.search(r"\btext\b|normalize_text", lowered):
        terms.add("text")
    generic = {"worker", "workers", "output", "outputs"}
    return {term.split("_")[0].split("-")[0] for term in terms if term and term not in generic}
