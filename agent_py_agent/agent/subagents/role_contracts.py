
"""模块用途: 在创建子代理时应用角色模板中的工具和展示元数据。

LLM: 角色模板可以缩小工具能力并提供模型提示，但不得再生成 acceptance_checks 或
第二套完成门；普通结束统一由 turn_end 和真实运行事实表达。
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from .model_task import QualityContract
from .role_templates import (
    ROLE_BASE_TOOLS,
    ROLE_TEMPLATE_ATTRIBUTE_KEY,
    RoleTemplate,
    active_model_subagent_tools,
    role_template_id_for_role,
    role_template_snapshot,
    template_for_role,
)

REPORTER_ROLE = "reporter"
CHECKER_ROLE = "checker"

# LLM: 角色名只用于选择模板；不要从普通目标文字反推角色。
# 函数用途: 把空角色归一为通用角色名，供模板查找和持久化。
def normalize_subagent_role(role: str) -> str:
    cleaned = str(role or "").strip().lower()
    if not cleaned:
        return "general"
    return cleaned


# LLM: 当前创建链必须把 acceptance_checks 清空；历史字段仅由持久层兼容读取。
# 函数用途: 合并角色模板的工具、质量说明和展示属性，不建立机器验收清单。
def apply_role_contract_to_create_params(params: Any, role_template_dirs: object = None):
    original_role = str(getattr(params, "role", "") or "general")
    contract_role = normalize_subagent_role(original_role)
    template_role = role_template_id_for_role(
        contract_role,
        role_template_dirs,
        default_id=_default_template_role(contract_role),
    )
    stored_role = _stored_role(original_role, contract_role, template_role, getattr(params, "normalize_role", True))
    effective_role = template_role or contract_role
    template = template_for_role(template_role, role_template_dirs) if template_role else None
    tools = _allowed_tools_for_role(effective_role, getattr(params, "allowed_tools", None), template)
    quality_contract = _quality_contract_for_role(effective_role, getattr(params, "quality_contract", None), template)
    attributes = _attributes_with_role_template(getattr(params, "attributes", None), template)
    return replace(
        params,
        role=stored_role,
        acceptance_checks=[],
        allowed_tools=tools,
        quality_contract=quality_contract,
        attributes=attributes,
    )


def _default_template_role(role: str) -> str | None:
    del role
    return None


def _stored_role(original_role: str, contract_role: str, template_role: str, normalize_role: object) -> str:
    if not bool(normalize_role):
        return original_role
    if template_role and contract_role != template_role:
        return contract_role
    return contract_role


# LLM: None means "derive role defaults" while an explicit empty list means
# "no tools". Every non-None snapshot passes the canonical retirement filter.
# 函数用途: 合并角色工具默认值，并保留显式空权限与已退休工具过滤语义。
def _allowed_tools_for_role(
    role: str,
    tools: object,
    template: RoleTemplate | None,
) -> list[str] | None:
    if tools is not None:
        explicit = [str(item) for item in _list_value(tools) if item not in (None, "")]
        return active_model_subagent_tools(explicit)
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


def _attributes_with_role_template(value: object, template: RoleTemplate | None) -> dict[str, object]:
    attrs = dict(value or {}) if isinstance(value, dict) else {}
    snapshot = role_template_snapshot(template)
    if snapshot:
        attrs[ROLE_TEMPLATE_ATTRIBUTE_KEY] = snapshot
    return attrs


def _list_value(value: object) -> list[object]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


# LLM: Stable role defaults share the same retired-control filter as explicit grants.
# 函数用途: 对角色默认工具做有序去重和退休入口清理。
def _stable_tools(values: list[str]) -> list[str]:
    return active_model_subagent_tools(values)
