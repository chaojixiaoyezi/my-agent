# LLM: Memory routing module; keep context selection and read-receipt records stable.
# 模块用途: 根据任务上下文选择可注入记忆，并记录读取路径。

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .models import MemoryRoute

VALID_INJECT_MODES = {"always", "on_hit", "never"}


# LLM: memory routing 读取项目规则、路径和上下文片段来决定注入范围；修改 _TermValidationContext 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 _TermValidationContext 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass(frozen=True)
class _TermValidationContext:
    label: str
    seen_terms: set[str]
    keyword_owners: dict[str, str]
    findings: list[str]


# LLM: memory routing 读取项目规则、路径和上下文片段来决定注入范围；修改 _RouteIdentityContext 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 _RouteIdentityContext 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass(frozen=True)
class _RouteIdentityContext:
    idx: int
    label: str
    seen_route_ids: dict[str, int]
    findings: list[str]


# LLM: memory routing 读取项目规则、路径和上下文片段来决定注入范围；修改 validate_routes 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 校验 validate routes 的输入、状态或路径，提前暴露无效数据和越界条件。
def validate_routes(routes: list[MemoryRoute], root: str | Path) -> list[str]:
    findings: list[str] = []
    seen_route_ids: dict[str, int] = {}
    keyword_owners: dict[str, str] = {}
    try:
        resolved_root = Path(root).resolve()
    except OSError as exc:
        return [f"root path cannot be resolved: {root} ({exc})"]

    for idx, route in enumerate(routes):
        label = route.route_id or f"<empty route_id at #{idx}>"
        _validate_route_identity(route, _RouteIdentityContext(idx, label, seen_route_ids, findings))
        _validate_route_shape(route, label, findings)
        _validate_route_terms(route, label, keyword_owners, findings)
        if not route.authority_file():
            findings.append(f"route '{label}': authority_path/source_file is empty")
            continue
        _validate_authority_path(route, resolved_root, findings)
    return findings


# LLM: memory routing 读取项目规则、路径和上下文片段来决定注入范围；修改 _validate_route_identity 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 校验 validate route identity 的输入、状态或路径，提前暴露无效数据和越界条件。
def _validate_route_identity(
    route: MemoryRoute,
    context: _RouteIdentityContext,
) -> None:
    route_id = route.route_id.strip()
    if not route_id:
        context.findings.append(f"route #{context.idx}: route_id is empty")
        return
    if route_id in context.seen_route_ids:
        first_seen = context.seen_route_ids[route_id]
        context.findings.append(f"route '{route_id}': duplicate route_id, first seen at #{first_seen}")
        return
    context.seen_route_ids[route_id] = context.idx


# LLM: memory routing 读取项目规则、路径和上下文片段来决定注入范围；修改 _validate_route_shape 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 校验 validate route shape 的输入、状态或路径，提前暴露无效数据和越界条件。
def _validate_route_shape(route: MemoryRoute, label: str, findings: list[str]) -> None:
    if not route.topic.strip():
        findings.append(f"route '{label}': topic is empty")
    if not route.trigger_keywords and not route.aliases:
        findings.append(f"route '{label}': trigger_keywords and aliases are both empty")
    if route.inject_mode not in VALID_INJECT_MODES:
        findings.append(f"route '{label}': inject_mode must be one of {sorted(VALID_INJECT_MODES)}")


# LLM: memory routing 读取项目规则、路径和上下文片段来决定注入范围；修改 _validate_route_terms 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 校验 validate route terms 的输入、状态或路径，提前暴露无效数据和越界条件。
def _validate_route_terms(
    route: MemoryRoute,
    label: str,
    keyword_owners: dict[str, str],
    findings: list[str],
) -> None:
    seen_terms: set[str] = set()
    context = _TermValidationContext(label, seen_terms, keyword_owners, findings)
    for term in [*route.trigger_keywords, *route.aliases]:
        normalized = term.strip().lower()
        if not normalized:
            continue
        _validate_route_term(term, normalized, context)


# LLM: memory routing 读取项目规则、路径和上下文片段来决定注入范围；修改 _validate_route_term 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 校验 validate route term 的输入、状态或路径，提前暴露无效数据和越界条件。
def _validate_route_term(
    term: str,
    normalized: str,
    context: _TermValidationContext,
) -> None:
    if normalized in context.seen_terms:
        context.findings.append(f"route '{context.label}': duplicate keyword '{term}'")
        return
    context.seen_terms.add(normalized)
    owner = context.keyword_owners.get(normalized)
    if owner and owner != context.label:
        context.findings.append(f"route keyword conflict '{term}': routes '{owner}' and '{context.label}'")
        return
    context.keyword_owners[normalized] = context.label


# LLM: memory routing 读取项目规则、路径和上下文片段来决定注入范围；修改 _validate_authority_path 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 校验 validate authority path 的输入、状态或路径，提前暴露无效数据和越界条件。
def _validate_authority_path(
    route: MemoryRoute,
    resolved_root: Path,
    findings: list[str],
) -> None:
    raw_path = route.authority_file()
    route_label = route.route_id or "<empty route_id>"
    candidate = Path(raw_path)
    if candidate.is_absolute() or PurePosixPath(raw_path).is_absolute():
        findings.append(f"route '{route_label}': source_file must be relative, got {raw_path}")
        return
    try:
        resolved_candidate = (resolved_root / candidate).resolve()
    except OSError as exc:
        findings.append(f"route '{route_label}': source_file cannot be resolved ({exc})")
        return
    if not resolved_candidate.is_relative_to(resolved_root):
        findings.append(f"route '{route_label}': source_file escapes root: {raw_path}")
        return
    if not resolved_candidate.exists():
        findings.append(f"route '{route_label}': authority_path does not exist: {raw_path}")
    elif not resolved_candidate.is_file():
        findings.append(f"route '{route_label}': authority_path is not a file: {raw_path}")
