
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ...common.value_parsing import text_value as _text
from ...contracts.state_machine import RunStateFacts, can_dispatch
from ...subagents.models import SUBAGENT_REUSABLE_STATUSES, task_status_in
from ...subagents.services.base import CreateRunParams
from ...subagents.services.idempotency_contract_identity import (
    idempotency_contract_identity_from_context_packs,
)
from ...subagents.services.repair_contract_identity import (
    repair_contract_identity_from_context_packs,
)

_GENERIC_AGENT_NAMES = {
    "",
    "general",
    "worker",
    "subagent",
    "agent",
    "child",
    "agent-d1-worker",
    "agent-d1-general",
    "agent-d1-researcher",
    "agent-d1-writer",
    "agent-d1-tester",
}
_GENERIC_LINEAGE_ROLES = {
    "worker",
    "general",
    "researcher",
    "writer",
    "tester",
    "bug-finder",
    "coordinator",
    "leaf-worker",
}
_INDEXED_SYSTEM_AGENT_RE = re.compile(r"^agent-d\d+-(?P<role>[a-z0-9_-]+)-(?P<index>\d+)$")


@dataclass(frozen=True)
class CreateTaskResolution:
    task: Any
    reused: bool = False


def resolve_create_run(manager: Any, params: CreateRunParams) -> CreateTaskResolution:
    existing = find_reusable_named_child(manager, params)
    if existing is not None:
        return CreateTaskResolution(task=existing, reused=True)
    return CreateTaskResolution(task=manager.create_run(params=params), reused=False)


def find_reusable_named_child(manager: Any, params: CreateRunParams):
    repair_identity = repair_contract_identity_from_context_packs(params.context_packs)
    if repair_identity:
        return find_reusable_repair_child(manager, params, repair_identity)
    idempotency_identity = idempotency_contract_identity_from_context_packs(params.context_packs)
    if idempotency_identity:
        return find_reusable_idempotency_child(manager, params, idempotency_identity)
    work_scope_key = _work_scope_key(params)
    if work_scope_key:
        return find_reusable_work_scope_child(manager, params, work_scope_key)
    name = _normalized_name(params.agent_name)
    if _is_generic_agent_name(name):
        return None
    if _is_indexed_generic_agent_name(name):
        return None
    return None


def find_reusable_work_scope_child(manager: Any, params: CreateRunParams, work_scope_key: str):
    for task in reversed(_safe_list_runs(manager)):
        if _same_work_scope(task, params, work_scope_key):
            return task
    return None


def find_reusable_repair_child(manager: Any, params: CreateRunParams, repair_identity: tuple[object, ...]):
    for task in reversed(_safe_list_runs(manager)):
        if _same_repair_scope(task, params, repair_identity):
            return task
    return None


def find_reusable_idempotency_child(manager: Any, params: CreateRunParams, idempotency_identity: tuple[object, ...]):
    for task in reversed(_safe_list_runs(manager)):
        if _same_idempotency_scope(task, params, idempotency_identity):
            return task
    return None


def created_tasks(resolutions: list[CreateTaskResolution]) -> list[Any]:
    return [item.task for item in resolutions if not item.reused]


def reused_tasks(resolutions: list[CreateTaskResolution]) -> list[Any]:
    return [item.task for item in resolutions if item.reused]


def dispatchable_tasks(tasks: list[Any]) -> list[Any]:
    return [
        task
        for task in tasks
        if can_dispatch(RunStateFacts(status=_status(task), verification_status=_verification(task)))
    ]


def _same_repair_scope(task: Any, params: CreateRunParams, repair_identity: tuple[object, ...]) -> bool:
    if not task_status_in(_status(task), SUBAGENT_REUSABLE_STATUSES):
        return False
    if _text(getattr(task, "parent_id", "")) != _text(params.parent_id):
        return False
    if _requested_root_id(params) and _text(getattr(task, "root_id", "")) != _requested_root_id(params):
        return False
    if not _matching_role(getattr(task, "role", ""), params.role):
        return False
    if _external_write_roots(task) != _params_extra_write_roots(params):
        return False
    return repair_contract_identity_from_context_packs(getattr(task, "context_packs", [])) == repair_identity


def _same_idempotency_scope(task: Any, params: CreateRunParams, idempotency_identity: tuple[object, ...]) -> bool:
    if not task_status_in(_status(task), SUBAGENT_REUSABLE_STATUSES):
        return False
    if _text(getattr(task, "parent_id", "")) != _text(params.parent_id):
        return False
    if _requested_root_id(params) and _text(getattr(task, "root_id", "")) != _requested_root_id(params):
        return False
    if not _matching_role(getattr(task, "role", ""), params.role):
        return False
    if _normalized_name(getattr(task, "agent_name", "")) != _normalized_name(params.agent_name):
        return False
    if _external_write_roots(task) != _params_extra_write_roots(params):
        return False
    return idempotency_contract_identity_from_context_packs(getattr(task, "context_packs", [])) == idempotency_identity


def _same_work_scope(task: Any, params: CreateRunParams, work_scope_key: str) -> bool:
    if not task_status_in(_status(task), SUBAGENT_REUSABLE_STATUSES):
        return False
    if _text(getattr(task, "parent_id", "")) != _text(params.parent_id):
        return False
    if _requested_root_id(params) and _text(getattr(task, "root_id", "")) != _requested_root_id(params):
        return False
    if not _matching_role(getattr(task, "role", ""), params.role):
        return False
    if _external_write_roots(task) != _params_extra_write_roots(params):
        return False
    attrs = getattr(task, "attributes", {}) or {}
    return isinstance(attrs, dict) and _text(attrs.get("work_scope_key")) == work_scope_key


def _work_scope_key(params: CreateRunParams) -> str:
    attrs = params.attributes if isinstance(params.attributes, dict) else {}
    return _text(attrs.get("work_scope_key"))


def _requested_root_id(params: CreateRunParams) -> str:
    return _text(params.root_id)


def _matching_role(existing: object, requested: object) -> bool:
    existing_text = _text(existing)
    requested_text = _text(requested)
    if not existing_text or not requested_text:
        return True
    return existing_text == requested_text


def _external_write_roots(task: Any) -> tuple[str, ...]:
    task_dir = _text(getattr(task, "task_dir", ""))
    roots = []
    for raw in getattr(task, "allowed_write_roots", []) or []:
        root = _normalized_path(raw)
        if root and root != _normalized_path(task_dir):
            roots.append(root)
    return tuple(sorted(dict.fromkeys(roots)))


def _params_extra_write_roots(params: CreateRunParams) -> tuple[str, ...]:
    return tuple(sorted(dict.fromkeys(_normalized_path(item) for item in params.extra_write_roots or [] if _text(item))))


def _normalized_path(value: object) -> str:
    text = _text(value)
    if text == "/":
        return text
    return text.rstrip("/")


def _safe_list_runs(manager: Any) -> list[Any]:
    try:
        runs = manager.list_runs()
    except (AttributeError, OSError, TypeError, ValueError):
        return []
    return list(runs or [])


def _normalized_name(value: object) -> str:
    return _text(value).casefold()


def _is_generic_agent_name(value: object) -> bool:
    name = _normalized_name(value)
    if name in _GENERIC_AGENT_NAMES:
        return True
    return False


def _is_indexed_generic_agent_name(value: object) -> bool:
    name = _normalized_name(value)
    match = _INDEXED_SYSTEM_AGENT_RE.match(name)
    if not match:
        return False
    return match.group("role") in _GENERIC_LINEAGE_ROLES


def _status(task: Any) -> str:
    return _text(getattr(task, "status", "")).upper()


def _verification(task: Any) -> str:
    return _text(getattr(task, "verification_status", "")).upper()
