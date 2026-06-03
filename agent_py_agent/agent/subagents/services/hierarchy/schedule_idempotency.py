
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ....common.value_parsing import text_value
from ..base import CreateRunParams
from ..idempotency_contract_identity import idempotency_contract_identity_from_context_packs
from ..repair_contract_identity import repair_contract_identity_from_context_packs

_REUSABLE_STATUSES = {"PLANNING", "PENDING", "RUNNING", "DONE", "COMPLETED", "BLOCKED", "PAUSED"}
_DISPATCHABLE_STATUSES = {"PLANNING", "PENDING"}


@dataclass(frozen=True)
class ScheduledChildResolution:
    task: Any
    reused: bool = False


def resolve_scheduled_child(manager: Any, params: CreateRunParams) -> ScheduledChildResolution:
    existing = find_reusable_scheduled_child(manager, params)
    if existing is not None:
        return ScheduledChildResolution(task=existing, reused=True)
    return ScheduledChildResolution(task=manager.create_run(params=params), reused=False)


def find_reusable_scheduled_child(manager: Any, params: CreateRunParams):
    parent_id = text_value(params.parent_id)
    if not parent_id:
        return None
    for child in reversed(_direct_children(manager, parent_id)):
        if _same_schedule_contract(child, params):
            return child
    return None


def created_scheduled_children(resolutions: list[ScheduledChildResolution]) -> list[Any]:
    return [item.task for item in resolutions if not item.reused]


def reused_scheduled_children(resolutions: list[ScheduledChildResolution]) -> list[Any]:
    return [item.task for item in resolutions if item.reused]


def dispatchable_scheduled_children(resolutions: list[ScheduledChildResolution]) -> list[Any]:
    return [item.task for item in resolutions if _status(item.task) in _DISPATCHABLE_STATUSES]


def _same_schedule_contract(task: Any, params: CreateRunParams) -> bool:
    if _status(task) not in _REUSABLE_STATUSES:
        return False
    if text_value(getattr(task, "parent_id", "")) != text_value(params.parent_id):
        return False
    if text_value(params.root_id) and text_value(getattr(task, "root_id", "")) != text_value(params.root_id):
        return False
    if _normalized_role(getattr(task, "role", "")) != _normalized_role(params.role):
        return False
    if _normalized_name(getattr(task, "agent_name", "")) != _normalized_name(params.agent_name):
        return False
    if _external_write_roots(task) != _params_extra_write_roots(params):
        return False
    repair_identity = repair_contract_identity_from_context_packs(params.context_packs)
    if repair_identity:
        return repair_contract_identity_from_context_packs(getattr(task, "context_packs", [])) == repair_identity
    idempotency_identity = idempotency_contract_identity_from_context_packs(params.context_packs)
    if idempotency_identity:
        return idempotency_contract_identity_from_context_packs(getattr(task, "context_packs", [])) == idempotency_identity
    return False


def _direct_children(manager: Any, parent_id: str) -> list[Any]:
    try:
        parent = manager.load(parent_id)
    except (FileNotFoundError, OSError, TypeError, ValueError):
        return []
    children: list[Any] = []
    for child_id in getattr(parent, "child_ids", []) or []:
        try:
            children.append(manager.load(str(child_id)))
        except (FileNotFoundError, OSError, TypeError, ValueError):
            continue
    return children


def _external_write_roots(task: Any) -> tuple[str, ...]:
    task_dir = _normalized_path(getattr(task, "task_dir", ""))
    roots = []
    for raw in getattr(task, "allowed_write_roots", []) or []:
        root = _normalized_path(raw)
        if root and root != task_dir:
            roots.append(root)
    return tuple(sorted(dict.fromkeys(roots)))


def _params_extra_write_roots(params: CreateRunParams) -> tuple[str, ...]:
    return tuple(sorted(dict.fromkeys(_normalized_path(item) for item in params.extra_write_roots or [] if text_value(item))))


def _normalized_role(value: object) -> str:
    return text_value(value).replace("_", "-").casefold()


def _normalized_name(value: object) -> str:
    return text_value(value).replace("_", "-").casefold()


def _normalized_path(value: object) -> str:
    text = text_value(value)
    if text == "/":
        return text
    return text.rstrip("/")


def _status(task: Any) -> str:
    return text_value(getattr(task, "status", "")).upper()
