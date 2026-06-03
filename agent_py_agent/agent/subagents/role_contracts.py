
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
    "coordinator": "coordinator",
    "worker": "worker",
    "researcher": "researcher",
    "writer": "writer",
}

REPORTER_ACCEPTANCE_CHECK = "Reporter output must cite evidence_refs or artifact_refs for each user-visible claim."
CHECKER_ACCEPTANCE_CHECK = "Checker must verify reporter evidence refs and write concrete findings."


def normalize_subagent_role(role: str) -> str:
    cleaned = str(role or "").strip().lower()
    if not cleaned:
        return "general"
    return ROLE_ALIASES.get(cleaned, cleaned)


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


def _fallback_template_role(role: str) -> str | None:
    if role in {"", "general", REPORTER_ROLE, CHECKER_ROLE}:
        return None
    return "worker"


def _stored_role(original_role: str, contract_role: str, template_role: str, normalize_role: object) -> str:
    if not bool(normalize_role):
        return original_role
    if template_role and contract_role != template_role:
        return contract_role
    return contract_role


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


def _quality_contract_for_role(role: str, value: object, template: RoleTemplate | None) -> object:
    if role not in {REPORTER_ROLE, CHECKER_ROLE} and template is None:
        return value
    if isinstance(value, QualityContract):
        return value
    if isinstance(value, dict):
        return dict(value)
    return QualityContract()


def _list_value(value: object) -> list[object]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _append_once(values: list[str], item: str) -> list[str]:
    if item not in values:
        values.append(item)
    return values


def _stable_tools(values: list[str]) -> list[str]:
    return list(dict.fromkeys(item for item in values if item))
