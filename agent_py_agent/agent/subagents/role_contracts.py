# LLM: Role contracts normalize subagent roles before task persistence.
# 模块用途: 固化 reporter/checker 角色别名、默认工具和验收边界，避免角色继续散落成自由字符串。

from __future__ import annotations

from dataclasses import replace
from typing import Any

from .quality_models import QualityContract
from .role_templates import (
    ROLE_BASE_TOOLS,
    RoleTemplate,
    role_template_id_for_role,
    template_for_role,
)

REPORTER_ROLE = "reporter"
CHECKER_ROLE = "checker"

ROLE_ALIASES = {
    "analyst": REPORTER_ROLE,
    "analysis": REPORTER_ROLE,
    "report": REPORTER_ROLE,
    "reporter": REPORTER_ROLE,
    "reviewer": CHECKER_ROLE,
    "verifier": CHECKER_ROLE,
    "checker": CHECKER_ROLE,
    "bug-finder": "bug_finder",
    "bugfinder": "bug_finder",
    "bug_finder": "bug_finder",
    "tester": "tester",
    "test": "tester",
    "acceptor": "acceptor",
    "acceptance": "acceptor",
    "coordinator": "coordinator",
    "worker": "worker",
    "researcher": "researcher",
    "writer": "writer",
}

REPORTER_ACCEPTANCE_CHECK = "Reporter output must cite evidence_refs or artifact_refs for each user-visible claim."
CHECKER_ACCEPTANCE_CHECK = "Checker must verify reporter evidence refs and cannot self-accept final work."


# LLM: normalize_subagent_role is the single public role-name normalization helper.
# 函数用途: 把 analyst/reviewer 等旧角色名映射到 reporter/checker，新自定义角色保持原样。
def normalize_subagent_role(role: str) -> str:
    cleaned = str(role or "").strip().lower()
    if not cleaned:
        return "general"
    return ROLE_ALIASES.get(cleaned, cleaned)


# LLM: apply_role_contract_to_create_params rewrites create-run bundles without changing call shape.
# 函数用途: 在任务落盘前补齐角色名、角色模板默认工具、验收要求和父级验收质量门。
def apply_role_contract_to_create_params(params: Any, role_template_dirs: object = None):
    original_role = str(getattr(params, "role", "") or "general")
    contract_role = normalize_subagent_role(original_role)
    template_role = role_template_id_for_role(
        contract_role,
        role_template_dirs,
        fallback=_fallback_template_role(contract_role),
    )
    stored_role = _stored_role(original_role, contract_role, template_role, getattr(params, "normalize_role", True))
    effective_role = template_role or contract_role
    template = template_for_role(template_role, role_template_dirs) if template_role else None
    checks = _acceptance_checks_for_role(effective_role, getattr(params, "acceptance_checks", None), template)
    tools = _allowed_tools_for_role(effective_role, getattr(params, "allowed_tools", None), template)
    quality_contract = _quality_contract_for_role(effective_role, getattr(params, "quality_contract", None), template)
    return replace(params, role=stored_role, acceptance_checks=checks, allowed_tools=tools, quality_contract=quality_contract)


# LLM: _fallback_template_role prevents unknown LLM-created roles from becoming empty agents.
# 函数用途: reporter/checker 用专属旧契约；其他未命中模板的自由角色至少套 worker 模板获得基础读写能力。
def _fallback_template_role(role: str) -> str | None:
    if role in {"", "general", REPORTER_ROLE, CHECKER_ROLE}:
        return None
    return "worker"


# LLM: _stored_role preserves hierarchy subtype names while applying their matched template contracts.
# 函数用途: child_coordinator/leaf_worker 这类角色仍保留在任务树里，但工具和验收按匹配到的模板生成。
def _stored_role(original_role: str, contract_role: str, template_role: str, normalize_role: object) -> str:
    if not bool(normalize_role):
        return original_role
    if template_role and contract_role != template_role:
        return contract_role
    return contract_role


# LLM: _acceptance_checks_for_role appends stable role-specific gates exactly once.
# 函数用途: 根据角色和模板补默认验收项，并保留调用方已有验收要求。
def _acceptance_checks_for_role(role: str, checks: object, template: RoleTemplate | None) -> list[str]:
    normalized = [str(item) for item in _list_value(checks) if item not in (None, "")]
    if role == REPORTER_ROLE:
        normalized = _append_once(normalized, REPORTER_ACCEPTANCE_CHECK)
    if role == CHECKER_ROLE:
        normalized = _append_once(normalized, CHECKER_ACCEPTANCE_CHECK)
    if template and template.output_contract_zh:
        normalized = _append_once(
            normalized,
            f"{template.name_zh} output contract: {template.output_contract_zh}",
        )
    return normalized


# LLM: _allowed_tools_for_role applies template defaults without turning roles into zero-hand agents.
# 函数用途: 按角色模板补默认工具；角色只追加职责，不拿掉基础读写、汇报和任务目录工作能力。
def _allowed_tools_for_role(
    role: str,
    tools: object,
    template: RoleTemplate | None,
) -> list[str] | None:
    if tools not in (None, []):
        explicit = [str(item) for item in _list_value(tools) if item not in (None, "")]
        return _stable_tools(explicit)
    if template:
        return _stable_tools([*template.default_tools, *ROLE_BASE_TOOLS])
    if role in {REPORTER_ROLE, CHECKER_ROLE}:
        return list(ROLE_BASE_TOOLS)
    return list(ROLE_BASE_TOOLS) if role else tools


# LLM: _quality_contract_for_role hardens parent-final-gate fields for quality roles.
# 函数用途: 让 reporter/checker/找茬/测试/验收等角色默认不能自验收，最终由父级门判断。
def _quality_contract_for_role(role: str, value: object, template: RoleTemplate | None) -> object:
    if role not in {REPORTER_ROLE, CHECKER_ROLE} and template is None:
        return value
    if isinstance(value, QualityContract):
        value.cannot_self_accept = True
        value.parent_final_gate = True
        if role in {CHECKER_ROLE, "acceptor"}:
            value.final_judge = "parent_final_gate"
        return value
    if isinstance(value, dict):
        payload = dict(value)
        payload["cannot_self_accept"] = True
        payload["parent_final_gate"] = True
        if role in {CHECKER_ROLE, "acceptor"}:
            payload["final_judge"] = "parent_final_gate"
        return payload
    return QualityContract()


# LLM: _list_value mirrors persistence normalization for local role-contract inputs.
# 函数用途: 把 None、单值、tuple/list 统一成 list，便于追加默认项。
def _list_value(value: object) -> list[object]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


# LLM: _append_once keeps generated acceptance checks stable across repeated saves.
# 函数用途: 向列表追加默认项但避免重复。
def _append_once(values: list[str], item: str) -> list[str]:
    if item not in values:
        values.append(item)
    return values


# LLM: _stable_tools preserves caller/template order while removing duplicate tool grants.
# 函数用途: 合并显式工具、模板工具和基础读写汇报工具，保持顺序稳定且不重复。
def _stable_tools(values: list[str]) -> list[str]:
    return list(dict.fromkeys(item for item in values if item))
