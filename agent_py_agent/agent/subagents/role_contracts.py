# LLM: Role contracts normalize subagent roles before task persistence.
# 模块用途: 固化 reporter/checker 角色别名、默认工具和验收边界，避免角色继续散落成自由字符串。

from __future__ import annotations

from dataclasses import replace
from typing import Any

from .quality_models import QualityContract
from .role_templates import RoleTemplate, template_for_role

REPORTER_ROLE = "reporter"
CHECKER_ROLE = "checker"
WRITE_TOOL_NAMES = {"write", "write_file", "append_file", "replace_in_file", "edit_file"}

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

CHECKER_READ_ONLY_TOOLS = ["list_files", "read_file", "search_text", "read_artifact"]
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
    stored_role = contract_role if bool(getattr(params, "normalize_role", True)) else original_role
    template = template_for_role(contract_role, role_template_dirs)
    checks = _acceptance_checks_for_role(contract_role, getattr(params, "acceptance_checks", None), template)
    tools = _allowed_tools_for_role(contract_role, getattr(params, "allowed_tools", None), template)
    quality_contract = _quality_contract_for_role(contract_role, getattr(params, "quality_contract", None), template)
    return replace(params, role=stored_role, acceptance_checks=checks, allowed_tools=tools, quality_contract=quality_contract)


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


# LLM: _allowed_tools_for_role applies template defaults while preserving explicit grants.
# 函数用途: 按角色模板补默认工具；只读角色会剔除写工具，避免找茬/测试/验收子代理改业务产物。
def _allowed_tools_for_role(
    role: str,
    tools: object,
    template: RoleTemplate | None,
) -> list[str] | None:
    if tools not in (None, []):
        explicit = [str(item) for item in _list_value(tools) if item not in (None, "")]
        if template and not template.can_write:
            return _without_write_tools(explicit)
        return explicit
    if template:
        return list(template.default_tools)
    if role == CHECKER_ROLE:
        return list(CHECKER_READ_ONLY_TOOLS)
    return tools


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


# LLM: _without_write_tools enforces read-only role boundaries after explicit caller grants.
# 函数用途: 过滤写文件类工具，避免检查型角色因为显式工具列表获得写权限。
def _without_write_tools(values: list[str]) -> list[str]:
    return [item for item in values if item.strip().lower() not in WRITE_TOOL_NAMES]
