
from __future__ import annotations

import re

from ..role_contracts import normalize_subagent_role
from ..role_templates import role_template_id_for_role, role_template_snapshot_for_task

QA_ROLE_ORDER = ("tester", "bug_finder")
_QA_ROLE_FIELDS = frozenset({"required_qa_roles", "qa_roles"})


def qa_roles_required_by_task(task) -> list[str]:
    if qa_role_task_is_leaf(task):
        return []
    return qa_roles_from_attributes(_task_attributes(task))


def qa_role_contract_text(task) -> str:
    roles = qa_roles_from_attributes(_task_attributes(task))
    return "required_qa_roles: " + ", ".join(roles) if roles else ""

def qa_roles_from_attributes(attributes: dict[str, object]) -> list[str]:
    requested: set[str] = set()
    for field in _QA_ROLE_FIELDS:
        requested.update(_role_items_from_object(attributes.get(field)))
    return [role for role in QA_ROLE_ORDER if role in requested]


def _task_attributes(task) -> dict[str, object]:
    attrs = getattr(task, "attributes", {}) or {}
    return attrs if isinstance(attrs, dict) else {}


def qa_role_identity_roles(*, role: str) -> set[str]:
    roles: set[str] = set()
    normalized = _role_template_identity(role)
    if normalized in QA_ROLE_ORDER:
        roles.add(normalized)
    return roles


def qa_role_task_is_leaf(task) -> bool:
    return bool(role_template_snapshot_for_task(task).get("depends_on_outputs"))


def _role_template_identity(value: object) -> str:
    normalized = normalize_subagent_role(str(value or ""))
    if normalized in QA_ROLE_ORDER:
        return normalized
    return role_template_id_for_role(normalized, default_id="")


def _role_items(value: object) -> set[str]:
    roles: set[str] = set()
    for raw in re.split(r"[\s,，、|/]+", str(value or "")):
        item = _role_template_identity(raw)
        if item in QA_ROLE_ORDER:
            roles.add(item)
    return roles


def _role_items_from_object(value: object) -> set[str]:
    if isinstance(value, (list, tuple, set)):
        roles: set[str] = set()
        for item in value:
            roles.update(_role_items_from_object(item))
        return roles
    return _role_items(value)
