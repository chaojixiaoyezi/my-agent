"""创建子代理的约束解析与幂等复用判定（原 create_constraints.py / create_idempotency.py 并入）。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...common.value_parsing import TOOL_TEXT_LIST_OPTIONS, string_list
from ...common.value_parsing import text_value as _text
from ...contracts.state_machine import RunStateFacts, can_dispatch
from ...subagents.models import SUBAGENT_REUSABLE_STATUSES, task_status_in
from ...subagents.role_templates import role_template_snapshot_for_role
from ...subagents.services.base import CreateRunParams
from ...subagents.services.contract_identity import (
    idempotency_contract_identity_from_context_packs,
    repair_contract_identity_from_context_packs,
)
from ..runner.ref_fields import params_output_refs
from .create_context import (
    agent_workspace_roots,
    context_target_write_roots,
    is_relative_to,
    normalized_write_root,
    structured_output_write_roots,
    structured_task_output_write_roots,
)


def role_allows_direct_product_work(role: str, role_template_dirs: object = None) -> bool:
    normalized = str(role or "worker").strip().lower().replace("-", "_")
    if not normalized:
        return True
    snapshot = role_template_snapshot_for_role(normalized, role_template_dirs)
    if not snapshot:
        return True
    if bool(snapshot.get("can_spawn_children")):
        return False
    if bool(snapshot.get("depends_on_outputs")) or bool(snapshot.get("can_run_tests")):
        return False
    return bool(snapshot.get("can_write"))


def merged_extra_write_roots(params: dict[str, object], goal: str) -> list[str]:
    roots: list[str] = []
    del goal
    for item in string_list(params.get("extra_write_roots"), TOOL_TEXT_LIST_OPTIONS):
        text = normalized_write_root(item)
        if text and text not in roots:
            roots.append(text)
    return roots


def resolved_extra_write_roots(agent: object, params: dict[str, object], goal: str) -> list[str]:
    explicit = merged_extra_write_roots(params, goal)
    if explicit:
        return explicit
    target_roots = []
    if _has_structured_write_intent(params, goal):
        target_roots.extend(structured_output_write_roots(agent, params))
        target_roots.extend(_current_task_output_write_roots(agent, params))
        if _has_repair_write_intent(params):
            target_roots.extend(context_target_write_roots(agent, params))
        target_roots.extend(structured_task_output_write_roots(agent, params))
    if target_roots:
        return _unique_roots(target_roots)
    default_root = _default_workspace_product_root(agent, params, goal)
    return [default_root] if default_root else []


def explicit_root_missing_write_root_error(agent: object, params: dict[str, object], goal: str) -> str:
    del agent, params, goal
    return ""


def _structured_output_refs(params: dict[str, object]) -> list[str]:
    return params_output_refs(params)


def _has_structured_write_intent(params: dict[str, object], goal: str) -> bool:
    if _structured_output_refs(params):
        return True
    if isinstance(params.get("repair_contract"), dict):
        return True
    packs = params.get("context_packs")
    if not isinstance(packs, list):
        return False
    return any(
        isinstance(pack, dict) and (pack.get("kind") == "repair_contract" or isinstance(pack.get("contract"), dict))
        for pack in packs
    )


def _has_repair_write_intent(params: dict[str, object]) -> bool:
    if isinstance(params.get("repair_contract"), dict):
        return True
    packs = params.get("context_packs")
    if not isinstance(packs, list):
        return False
    return any(isinstance(pack, dict) and pack.get("kind") == "repair_contract" for pack in packs)


def _default_workspace_product_root(agent: object, params: dict[str, object], goal: str) -> str:
    if not _structured_output_refs(params):
        return ""
    raw = getattr(getattr(agent, "subagents", None), "workspace_root", None)
    if not isinstance(raw, str | Path):
        return ""
    root = Path(raw).expanduser().resolve(strict=False)
    workspace = getattr(getattr(agent, "subagents", None), "workspace", None)
    if isinstance(workspace, str | Path) and root == Path(workspace).expanduser().resolve(strict=False):
        return ""
    roots = agent_workspace_roots(agent, root)
    if not any(is_relative_to(root, item) for item in roots):
        return ""
    return str(root) if _has_output_ref_inside_workspace(params, root, roots) else ""


def _has_output_ref_inside_workspace(params: dict[str, object], root: Path, roots: list[Path]) -> bool:
    for ref in params_output_refs(params):
        path = _output_ref_path(ref, root)
        if path is not None and any(is_relative_to(path, workspace_root) for workspace_root in roots):
            return True
    return False


def _current_task_output_write_roots(agent: object, params: dict[str, object]) -> list[str]:
    task_root = _current_task_root(agent)
    if not task_root:
        return []
    task_output = (Path(task_root).expanduser() / "output").resolve(strict=False)
    roots: list[str] = []
    for ref in params_output_refs(params):
        root = _task_output_root_for_ref(ref, task_output)
        if root and root not in roots:
            roots.append(root)
    return roots


def _task_output_root_for_ref(ref: str, task_output: Path) -> str:
    path = _output_ref_path(ref, task_output)
    if path is None or not is_relative_to(path, task_output):
        return ""
    return str(path.parent)


def _current_task_root(agent: object) -> str:
    raw = getattr(agent, "_current_run_task_workspace", "")
    if not isinstance(raw, str | Path):
        return ""
    return str(raw).strip()


def _output_ref_path(ref: str, root: Path) -> Path | None:
    text = str(ref or "").strip()
    if not text or "://" in text:
        return None
    try:
        path = Path(text).expanduser()
    except OSError:
        return None
    if not path.is_absolute():
        path = root / path
    return path.resolve(strict=False)


def _manager_has_real_workspace(agent: object) -> bool:
    raw = getattr(getattr(agent, "subagents", None), "workspace_root", None)
    return isinstance(raw, str | Path)


def _unique_roots(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result


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
    return _text(getattr(task, "status", ""))


def _verification(task: Any) -> str:
    return _text(getattr(task, "verification_status", ""))
