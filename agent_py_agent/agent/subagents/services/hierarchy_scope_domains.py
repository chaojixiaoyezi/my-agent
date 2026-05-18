# LLM: Hierarchy scope-domain helpers isolate sibling-domain checks from lifecycle guards.
# 模块用途: 处理 forbidden_child_scopes 和 parent/child 领域匹配，避免 scope guard 主文件继续变厚。

from __future__ import annotations

import re
from typing import Any

from ..models import SubAgentTask
from .hierarchy_domain_terms import DOMAIN_STOPWORDS, FORBIDDEN_SCOPE_GENERIC_TERMS


# LLM: forbidden_child_scope_reason enforces explicit sibling exclusions before children are persisted.
# 函数用途: parent goal 写明“不得创建 X”时，阻断包含 X 的下一层任务，避免错误领域 leaf 落盘。
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
    return bool(term) and any(term in _child_scope_text(spec) for spec in child_specs)


# LLM: _forbidden_scope_terms extracts machine-readable forbidden child scopes.
# 函数用途: 只从 forbidden_child_scopes 机器字段和明确 scope 合同提取领域名，不读 thought/debug note。
def _forbidden_scope_terms(parent: SubAgentTask) -> list[str]:
    if int(parent.depth or 0) <= 0:
        return []
    terms = [*_structured_scope_terms(parent.goal, "forbidden_child_scopes"), *_natural_forbidden_scope_terms(parent.goal)]
    return list(
        dict.fromkeys(
            term
            for raw in terms
            if (term := raw.strip("_-")) and term not in FORBIDDEN_SCOPE_GENERIC_TERMS
        )
    )


# LLM: _natural_forbidden_scope_terms reads only explicit sibling-domain exclusions from parent goal text.
# 函数用途: 支持“不得创建 arithmetic 相关任务”这种父级 scope 合同；不读取普通业务句。
def _natural_forbidden_scope_terms(goal: object) -> list[str]:
    terms: list[str] = []
    for segment in re.split(r"[\n。；;]+", str(goal or "").lower()):
        if _is_forbidden_scope_segment(segment):
            terms.extend(_domain_tokens(segment))
    return terms


# LLM: _is_forbidden_scope_segment filters depth constraints away from sibling-domain exclusions.
# 函数用途: 只有“不得/禁止 + 相关任务/领域/leaf_worker”才作为禁止领域，避免 depth>=4 被误解。
def _is_forbidden_scope_segment(segment: str) -> bool:
    negative = any(marker in segment for marker in ("不得", "禁止", "不允许", "不要", "不能", "forbidden", "do not"))
    scoped = any(marker in segment for marker in ("相关任务", "领域", "leaf_worker", "worker", "scope"))
    return negative and scoped


# LLM: _parent_domain_terms extracts explicit coordinator domain labels from role/name and task refs.
# 函数用途: 从 text-lead、leaf_worker_text、只负责 text 领域等结构化短语提取父级负责领域。
def _parent_domain_terms(parent: SubAgentTask) -> set[str]:
    label_text = f"{parent.agent_name} {parent.role}".lower()
    goal_text = str(parent.goal or "").lower()
    return _domain_refs(f"{label_text} {goal_text}") | _explicit_responsibility_domains(goal_text)


# LLM: _child_domain_terms extracts requested child domains from labels first, then explicit leaf_worker refs.
# 函数用途: 判断 child spec 是否声明了其它领域，避免共享路径词造成误判。
def _child_domain_terms(spec: Any) -> set[str]:
    label_domains = _domain_refs(f"{getattr(spec, 'agent_name', '')} {getattr(spec, 'role', '')}".lower())
    if label_domains:
        return label_domains
    text = _child_scope_text(spec)
    return _domain_refs(text) | _explicit_leaf_domain_terms(text)


# LLM: _domain_refs pulls domain tokens from leaf_worker_<domain> and <domain>-lead patterns.
# 函数用途: 读取常见层级任务命名，不用整句自然语言做业务判断。
def _domain_refs(text: str) -> set[str]:
    refs: set[str] = set()
    for pattern in (r"leaf[_-]worker[_-]([a-z][a-z0-9_-]+)", r"\b([a-z][a-z0-9_-]+)[_-]lead\b"):
        refs.update(match.group(1) for match in re.finditer(pattern, text))
    return {term for term in refs if term not in DOMAIN_STOPWORDS}


# LLM: _explicit_responsibility_domains reads short "only owns X domain" scope contracts.
# 函数用途: 支持“只负责 text 领域任务”，但不把普通 coordinator 名字当领域。
def _explicit_responsibility_domains(text: str) -> set[str]:
    refs: set[str] = set()
    for match in re.finditer(r"(?:只负责|负责)\s+([a-z][a-z0-9_-]+)\s*(?:领域|相关)", text):
        refs.add(match.group(1))
    return {term for term in refs if term not in DOMAIN_STOPWORDS}


# LLM: _explicit_leaf_domain_terms reads "arithmetic leaf_worker" without treating function names as domains.
# 函数用途: 捕获 child 明确声明的 leaf 领域；`实现 add(a,b)` 这类算法函数名不会触发 domain mismatch。
def _explicit_leaf_domain_terms(text: str) -> set[str]:
    refs: set[str] = set()
    for match in re.finditer(r"\b([a-z][a-z0-9_-]+)\s+(?:leaf_worker|leaf)\b", text):
        refs.add(match.group(1))
    return {term for term in refs if term not in DOMAIN_STOPWORDS}


# LLM: _domain_tokens extracts small identifier-like domains and drops operational words.
# 函数用途: 从短 scope 片段中提取 arithmetic/text 这类领域词。
def _domain_tokens(text: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z][a-z0-9]+", str(text or "").lower())
        if token not in DOMAIN_STOPWORDS and token not in FORBIDDEN_SCOPE_GENERIC_TERMS and token != "solution"
    }


# LLM: _structured_scope_terms reads simple protocol scope lists.
# 函数用途: 支持 `forbidden_child_scopes: arithmetic, text` 和后续 bullet 列表。
def _structured_scope_terms(text: object, field_name: str) -> list[str]:
    terms: list[str] = []
    active = False
    pattern = re.compile(rf"^\s*(?:[-*]\s*)?{re.escape(field_name)}\s*[:=]\s*(?P<tail>.*)$", re.IGNORECASE)
    for raw in str(text or "").splitlines():
        active, items = _structured_scope_line(raw, active=active, pattern=pattern)
        terms.extend(items)
    return terms


# LLM: _structured_scope_line keeps forbidden scope parsing flat.
# 函数用途: 解析一行 forbidden_child_scopes，返回 active 状态和 scope token 列表。
def _structured_scope_line(raw: str, *, active: bool, pattern: re.Pattern[str]) -> tuple[bool, list[str]]:
    line = raw.strip()
    match = pattern.match(line)
    if match:
        return True, _scope_items(match.group("tail"))
    if not active:
        return False, []
    if not line.startswith(("-", "*")):
        return False, []
    return True, _scope_items(line.lstrip("-* "))


# LLM: _scope_items tokenizes one structured scope value.
# 函数用途: 读取英文/标识符 scope，不接受普通中文句子。
def _scope_items(value: object) -> list[str]:
    return [
        item
        for item in re.split(r"[\s,，、|/]+", str(value or ""))
        if re.fullmatch(r"[a-zA-Z0-9_-]+", item or "")
    ]


# LLM: _child_scope_text keeps forbidden-scope matching limited to the requested child spec.
# 函数用途: 合并 child 的 goal/agent_name/role，避免拿补全后的父级上下文误判。
def _child_scope_text(spec: Any) -> str:
    return f"{spec.goal} {spec.agent_name} {spec.role}".lower()
