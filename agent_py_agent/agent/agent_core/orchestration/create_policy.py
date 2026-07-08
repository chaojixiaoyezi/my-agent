"""创建子代理的策略入口：参数构建、角色模板、工具授权、会话属性（原 create_policy.py / create_conversation.py 并入）。"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from ...common.value_parsing import TOOL_TEXT_LIST_OPTIONS, bool_value, string_list
from ...runtime_errors import runtime_error_report
from ...settings.defaults import DEFAULT_COMMAND_ACCESS_MODE
from ...subagents.role_templates import COORDINATOR_TOOLS, role_template_snapshot_for_role
from ...subagents.services.base import CreateRunParams
from ...subagents.services.workflow import tool_workflow_mode
from ..parameters import _bool_param, _positive_int
from ..runner.prompts import SUBAGENT_DEFAULT_PLAN, SUBAGENT_DEFAULT_THOUGHT
from ..runner.ref_fields import params_input_refs, params_output_refs
from ..spawn_role_seed import is_explicit_root_role
from .create_constraints import (
    resolved_extra_write_roots,
    role_allows_direct_product_work,
)
from .create_context import create_context_manifest, create_context_packs
from .work_scope import add_work_scope_key


@dataclass(frozen=True)
class WorkflowDisableRequest:
    raw_params: dict[str, object]
    role: str
    workflow_mode: str
    role_template_dirs: object = None


@dataclass(frozen=True)
class RolePolicy:
    role: str
    allowed_tools: list[str] | None
    workflow_mode: str


@dataclass(frozen=True)
class CreateRunBuildRequest:
    agent: object
    raw_params: dict[str, object]
    goal: str
    role_policy: RolePolicy


def create_run_params(
    agent,
    raw_params: dict[str, object],
    goal: str,
    allowed_tools: list[str] | None,
):
    raw_params = _params_with_task_output_defaults(raw_params, agent)
    role_policy = _role_policy(agent, raw_params, goal, allowed_tools)
    return _create_run_params_from_build(CreateRunBuildRequest(agent, raw_params, goal, role_policy))


def _role_policy(
    agent,
    raw_params: dict[str, object],
    goal: str,
    allowed_tools: list[str] | None,
) -> RolePolicy:
    workflow_mode = tool_workflow_mode(raw_params.get("workflow_mode"), agent.config.subagent_workflow_mode)
    role = _role_from_create_intent(raw_params, goal, agent)
    role_template_dirs = _role_template_dirs(agent)
    is_explicit_root = is_explicit_root_role(role, role_template_dirs)
    if is_explicit_root:
        workflow_mode = "off"
        allowed_tools = explicit_root_allowed_tools(allowed_tools)
    elif _should_disable_generic_workflow_for_concrete_worker(WorkflowDisableRequest(
        raw_params=raw_params,
        role=role,
        workflow_mode=workflow_mode,
        role_template_dirs=role_template_dirs,
    )):
        workflow_mode = "off"
        role = _direct_worker_role(role)
    return RolePolicy(role=role, allowed_tools=allowed_tools, workflow_mode=workflow_mode)


def _direct_worker_role(role: str) -> str:
    return "worker" if role != "worker" else role


def _create_run_params_from_build(request: CreateRunBuildRequest) -> CreateRunParams:
    raw_params = request.raw_params
    role_policy = request.role_policy
    return CreateRunParams(
        goal=request.goal,
        thought=str(raw_params.get("thought") or SUBAGENT_DEFAULT_THOUGHT).strip(),
        plan=_create_plan(raw_params),
        agent_name=_root_agent_name(raw_params, role_policy.role),
        role=role_policy.role,
        allowed_tools=role_policy.allowed_tools,
        owner=str(raw_params.get("owner") or _default_owner_id(request.agent)).strip(),
        supervisor=str(raw_params.get("supervisor") or "parent").strip(),
        final_owner=str(raw_params.get("final_owner") or "").strip(),
        acceptance_checks=string_list(raw_params.get("acceptance_checks"), TOOL_TEXT_LIST_OPTIONS),
        extra_write_roots=resolved_extra_write_roots(request.agent, raw_params, request.goal),
        context_manifest=create_context_manifest(raw_params),
        context_packs=create_context_packs(raw_params),
        workflow_mode=role_policy.workflow_mode,
        attributes=_create_attributes(raw_params, request.agent),
        **_lineage_fields(raw_params, request.agent),
        parent_access_mode=_config_access_mode(request.agent),
        memory_retention_policy=_config_string(
            request.agent,
            "subagent_memory_retention_policy",
            "parent_review_or_cleanup",
        ),
        memory_delete_after_days=_config_int(request.agent, "subagent_memory_delete_after_days", 0),
        destroy_summary_required=_config_bool(request.agent, "subagent_destroy_summary_required", True),
    )


def _lineage_fields(raw_params: dict[str, object], agent) -> dict[str, object]:
    explicit_parent = str(raw_params.get("parent_id") or "").strip()
    explicit_root = str(raw_params.get("root_id") or "").strip()
    explicit_depth = raw_params.get("depth")
    conversation_root = _current_conversation_task_id(agent)
    parent_id = explicit_parent or conversation_root or _current_run_id(agent)
    root_id = explicit_root or parent_id
    depth = _lineage_depth(explicit_depth, default=1 if parent_id else 0)
    return {
        "parent_id": parent_id,
        "root_id": root_id,
        "depth": depth,
    }


def _current_run_id(agent) -> str:
    current = getattr(agent, "_current_run_params", None)
    if current is None:
        return ""
    return str(getattr(current, "run_id", "") or getattr(current, "task_id", "") or "").strip()


def _current_conversation_task_id(agent) -> str:
    current = getattr(agent, "_current_run_params", None)
    attrs = getattr(current, "task_attributes", None) if current is not None else None
    if not isinstance(attrs, dict):
        return ""
    return str(attrs.get("conversation_task_id") or "").strip()


def explicit_root_allowed_tools(allowed_tools: list[str] | None) -> list[str] | None:
    if allowed_tools is None:
        return None
    return list(dict.fromkeys([*allowed_tools, *COORDINATOR_TOOLS]))


def _lineage_depth(value: object, *, default: int) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def _role_from_create_intent(raw_params: dict[str, object], goal: str, agent) -> str:
    role = str(raw_params.get("role") or "worker").strip() or "worker"
    if is_explicit_root_role(role, _role_template_dirs(agent)):
        return role
    if _json_child_items(raw_params.get("children")):
        return "coordinator"
    if role == "worker" and _has_child_dispatch_tool(raw_params) and not _role_depends_on_outputs(raw_params, agent):
        return "coordinator"
    return role


def _has_child_dispatch_tool(raw_params: dict[str, object]) -> bool:
    if raw_params.get("_item_allowed_tools_explicit") is False:
        return False
    tools = {str(item or "").strip().lower() for item in string_list(raw_params.get("allowed_tools"), TOOL_TEXT_LIST_OPTIONS)}
    return bool({"schedule_child_subagents", "dispatch_subagents"}.intersection(tools))


def _role_depends_on_outputs(raw_params: dict[str, object], agent) -> bool:
    snapshot = role_template_snapshot_for_role(str(raw_params.get("role") or ""), _role_template_dirs(agent))
    return bool(snapshot.get("depends_on_outputs"))


def _role_template_dirs(agent) -> object:
    subagents = getattr(agent, "subagents", None)
    return getattr(subagents, "role_template_dirs", None)


def _config_access_mode(agent) -> str:
    value = getattr(getattr(agent, "config", None), "access_mode", DEFAULT_COMMAND_ACCESS_MODE)
    text = str(value).strip() if isinstance(value, str) else ""
    return text or DEFAULT_COMMAND_ACCESS_MODE


def _config_string(agent, key: str, default: str) -> str:
    value = getattr(getattr(agent, "config", None), key, default)
    text = str(value).strip() if isinstance(value, str) else ""
    return text or default


def _config_int(agent, key: str, default: int) -> int:
    value = getattr(getattr(agent, "config", None), key, default)
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(0, parsed)


def _config_bool(agent, key: str, default: bool) -> bool:
    value = getattr(getattr(agent, "config", None), key, default)
    return bool_value(value, default=default)


def _should_disable_generic_workflow_for_concrete_worker(request: WorkflowDisableRequest) -> bool:
    if request.workflow_mode != "auto":
        return False
    if not role_allows_direct_product_work(request.role, request.role_template_dirs):
        return False
    if _positive_int(request.raw_params.get("count"), default=1) <= 0:
        return False
    return bool(params_output_refs(request.raw_params))


def _create_attributes(raw_params: dict[str, object], agent=None) -> dict[str, object]:
    attrs = dict(raw_params.get("attributes") or {}) if isinstance(raw_params.get("attributes"), dict) else {}
    for key in _LIST_ATTRIBUTE_FIELDS:
        values = _list_attribute_values(key, raw_params)
        if values and key not in attrs:
            attrs[key] = values
    for key in _SCALAR_ATTRIBUTE_FIELDS:
        value = str(raw_params.get(key) or "").strip()
        if value and key not in attrs:
            attrs[key] = value
    for key in _MAPPING_ATTRIBUTE_FIELDS:
        value = raw_params.get(key)
        if isinstance(value, dict) and key not in attrs:
            attrs[key] = dict(value)
    for key in _BOOL_ATTRIBUTE_FIELDS:
        if key in raw_params and key not in attrs:
            attrs[key] = _bool_param(raw_params.get(key), default=False)
    for key in _POSITIVE_INT_ATTRIBUTE_FIELDS:
        if key in raw_params and key not in attrs and _positive_int(raw_params.get(key), default=0) > 0:
            attrs[key] = _positive_int(raw_params.get(key), default=0)
    _add_derived_output_refs(attrs, raw_params)
    add_work_scope_key(attrs)
    add_current_conversation_attrs(attrs, agent)
    _add_current_task_workspace(attrs, agent)
    _inherit_audit_guarantee(attrs, agent)
    return attrs


def _inherit_audit_guarantee(attrs: dict[str, object], agent) -> None:
    """/audit 契约沿 spawn 树结构化下传:父任务(主代理或上层子代理)处于保证档时,派出的
    每个子代理 task.attributes 也带上保证档标志——委派做盯守的判读子代理/孙代理照样在保证档
    (用户明确要"命令一加子代理也一样")。继承靠结构化 attributes、不靠 goal 文本是否恰好带
    /audit 词元(真机缺口:子代理 goal 空、词元在 runner_prompt 里,靠文本必漏)。"""
    from ...common.audit_activation import AUDIT_ATTR, attributes_request_audit

    if attributes_request_audit(attrs):
        return  # 显式在 item 里给了(create_subagents 直传),不覆盖
    current = getattr(agent, "_current_run_params", None)
    if attributes_request_audit(getattr(current, "task_attributes", None)):
        attrs[AUDIT_ATTR] = True
        return
    try:
        from ..runner.context import current_task_attributes

        if attributes_request_audit(current_task_attributes(agent)):
            attrs[AUDIT_ATTR] = True
    except Exception:
        pass


def _params_with_task_output_defaults(raw_params: dict[str, object], agent=None) -> dict[str, object]:
    task_root = _current_task_root_path(agent)
    task_output_dir = _current_task_output_dir(agent)
    workspace_output_dir = _primary_workspace_output_dir(agent)
    workspace_root = _primary_workspace_root(agent)
    if not task_output_dir or _has_user_requested_output_dir(raw_params, agent):
        return raw_params
    updated = dict(raw_params)
    changed = False
    if task_root:
        changed = _normalize_current_task_workspace_refs(updated, task_root) or changed
    if workspace_root:
        changed = _normalize_current_task_output_refs(updated, workspace_root, task_output_dir) or changed
    changed = _normalize_relative_task_output_refs(updated, task_output_dir) or changed
    if not workspace_output_dir:
        return updated if changed else raw_params
    for key in _OUTPUT_REF_ATTRIBUTE_FIELDS:
        changed = _rebase_output_ref_field(updated, key, workspace_output_dir, task_output_dir) or changed
    attrs_changed = _rebase_attribute_output_refs(updated, workspace_output_dir, task_output_dir)
    changed = changed or attrs_changed
    return updated if changed else raw_params


def _normalize_current_task_workspace_refs(
    updated: dict[str, object],
    task_root: Path,
) -> bool:
    changed = False
    for key in _OUTPUT_REF_ATTRIBUTE_FIELDS:
        changed = _normalize_current_task_workspace_ref_field(updated, key, task_root) or changed
    attrs = updated.get("attributes")
    if isinstance(attrs, dict):
        next_attrs = dict(attrs)
        attrs_changed = False
        for key in _OUTPUT_REF_ATTRIBUTE_FIELDS:
            attrs_changed = _normalize_current_task_workspace_ref_field(next_attrs, key, task_root) or attrs_changed
        if attrs_changed:
            updated["attributes"] = next_attrs
            changed = True
    return changed


def _normalize_current_task_workspace_ref_field(
    values: dict[str, object],
    key: str,
    task_root: Path,
) -> bool:
    if key not in values:
        return False
    value, changed = _normalize_current_task_workspace_ref_value(values.get(key), task_root)
    if changed:
        values[key] = value
    return changed


def _normalize_current_task_workspace_ref_value(value: object, task_root: Path) -> tuple[object, bool]:
    return _map_output_ref_value(value, lambda text: _current_task_workspace_ref_text(text, task_root))


def _current_task_workspace_ref_text(text: str, task_root: Path) -> str | None:
    candidate = _current_task_workspace_ref(text, task_root)
    return str(candidate) if candidate is not None else None


def _current_task_workspace_ref(text: str, task_root: Path) -> Path | None:
    if not text or "://" in text:
        return None
    try:
        resolved_task_root = task_root.expanduser().resolve(strict=False)
    except OSError:
        return None
    try:
        path = Path(text).expanduser()
    except OSError:
        return None
    if path.is_absolute():
        return None
    normalized_parts = _slash_path_parts(text)
    if not normalized_parts:
        return None
    root_task_parts = _task_suffix_parts(resolved_task_root)
    if not root_task_parts:
        return None
    for index, part in enumerate(normalized_parts):
        if part != "tasks":
            continue
        task_parts = normalized_parts[index + 1 :]
        if len(task_parts) <= len(root_task_parts):
            continue
        if task_parts[: len(root_task_parts)] != root_task_parts:
            continue
        suffix_parts = task_parts[len(root_task_parts) :]
        if suffix_parts[0] not in {"output", "work"}:
            continue
        try:
            candidate = (resolved_task_root / Path(*suffix_parts)).resolve(strict=False)
        except OSError:
            continue
        if _same_or_inside(candidate, resolved_task_root):
            return candidate
    return None


def _task_suffix_parts(task_root: Path) -> list[str]:
    parts = _slash_path_parts(str(task_root))
    for index in range(len(parts) - 1, -1, -1):
        if parts[index] == "tasks":
            return parts[index + 1 :]
    return []


def _slash_path_parts(text: str) -> list[str]:
    normalized = text.strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return [part for part in normalized.split("/") if part not in {"", "."}]


def _normalize_current_task_output_refs(
    updated: dict[str, object],
    workspace_root: Path,
    task_output_dir: Path,
) -> bool:
    changed = False
    for key in _OUTPUT_REF_ATTRIBUTE_FIELDS:
        changed = _normalize_current_task_output_ref_field(updated, key, workspace_root, task_output_dir) or changed
    attrs = updated.get("attributes")
    if isinstance(attrs, dict):
        next_attrs = dict(attrs)
        attrs_changed = False
        for key in _OUTPUT_REF_ATTRIBUTE_FIELDS:
            attrs_changed = (
                _normalize_current_task_output_ref_field(next_attrs, key, workspace_root, task_output_dir)
                or attrs_changed
            )
        if attrs_changed:
            updated["attributes"] = next_attrs
            changed = True
    return changed


def _normalize_relative_task_output_refs(
    updated: dict[str, object],
    task_output_dir: Path,
) -> bool:
    changed = False
    for key in _OUTPUT_REF_ATTRIBUTE_FIELDS:
        changed = _normalize_relative_task_output_ref_field(updated, key, task_output_dir) or changed
    attrs = updated.get("attributes")
    if isinstance(attrs, dict):
        next_attrs = dict(attrs)
        attrs_changed = False
        for key in _OUTPUT_REF_ATTRIBUTE_FIELDS:
            attrs_changed = _normalize_relative_task_output_ref_field(next_attrs, key, task_output_dir) or attrs_changed
        if attrs_changed:
            updated["attributes"] = next_attrs
            changed = True
    return changed


def _normalize_relative_task_output_ref_field(
    values: dict[str, object],
    key: str,
    task_output_dir: Path,
) -> bool:
    if key not in values:
        return False
    value, changed = _normalize_relative_task_output_ref_value(values.get(key), task_output_dir)
    if changed:
        values[key] = value
    return changed


def _normalize_relative_task_output_ref_value(value: object, task_output_dir: Path) -> tuple[object, bool]:
    return _map_output_ref_value(value, lambda text: _relative_task_output_ref(text, task_output_dir))


def _relative_task_output_ref(text: str, task_output_dir: Path) -> str | None:
    if not text or "://" in text:
        return None
    try:
        path = _task_output_relative_path(text)
    except OSError:
        return None
    if path.is_absolute():
        return None
    try:
        candidate = (task_output_dir / path).resolve(strict=False)
    except OSError:
        return None
    if not _same_or_inside(candidate, task_output_dir):
        return None
    return str(candidate)


def _task_output_relative_path(text: str) -> Path:
    path = Path(text).expanduser()
    normalized = text.strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    if normalized == "output":
        return Path()
    if normalized.startswith("output/"):
        return Path(normalized[len("output/") :])
    return path


def _normalize_current_task_output_ref_field(
    values: dict[str, object],
    key: str,
    workspace_root: Path,
    task_output_dir: Path,
) -> bool:
    if key not in values:
        return False
    value, changed = _normalize_current_task_output_ref_value(values.get(key), workspace_root, task_output_dir)
    if changed:
        values[key] = value
    return changed


def _normalize_current_task_output_ref_value(
    value: object,
    workspace_root: Path,
    task_output_dir: Path,
) -> tuple[object, bool]:
    return _map_output_ref_value(
        value,
        lambda text: _workspace_relative_task_output_ref_text(text, workspace_root, task_output_dir),
    )


def _workspace_relative_task_output_ref_text(
    text: str,
    workspace_root: Path,
    task_output_dir: Path,
) -> str | None:
    if not text or "://" in text:
        return None
    try:
        path = Path(text).expanduser()
    except OSError:
        return None
    if path.is_absolute():
        return None
    candidate = _workspace_relative_task_output_ref(text, workspace_root, task_output_dir)
    if candidate is None:
        return None
    return str(candidate)


def _workspace_relative_task_output_ref(text: str, workspace_root: Path, task_output_dir: Path) -> Path | None:
    for relative in _workspace_relative_candidates(text):
        try:
            candidate = (workspace_root / relative).expanduser().resolve(strict=False)
        except OSError:
            continue
        if _same_or_inside(candidate, task_output_dir):
            return candidate
    return None


def _workspace_relative_candidates(text: str) -> list[Path]:
    path = Path(text)
    candidates = [path]
    parts = path.parts
    if parts and parts[0] == "my_agent":
        candidates.append(Path(".my_agent", *parts[1:]))
    return candidates


def _rebase_attribute_output_refs(
    updated: dict[str, object],
    workspace_output_dir: Path,
    task_output_dir: Path,
) -> bool:
    attrs = updated.get("attributes")
    if not isinstance(attrs, dict):
        return False
    next_attrs = dict(attrs)
    changed = False
    for key in _OUTPUT_REF_ATTRIBUTE_FIELDS:
        changed = _rebase_output_ref_field(next_attrs, key, workspace_output_dir, task_output_dir) or changed
    if changed:
        updated["attributes"] = next_attrs
    return changed


def _rebase_output_ref_field(
    values: dict[str, object],
    key: str,
    workspace_output_dir: Path,
    task_output_dir: Path,
) -> bool:
    if key not in values:
        return False
    value, changed = _rebase_output_ref_value(values.get(key), workspace_output_dir, task_output_dir)
    if changed:
        values[key] = value
    return changed


def _rebase_output_ref_value(value: object, workspace_output_dir: Path, task_output_dir: Path) -> tuple[object, bool]:
    return _map_output_ref_value(
        value,
        lambda text: _rebased_task_output_ref(text, workspace_output_dir, task_output_dir),
    )


def _rebased_task_output_ref(text: str, workspace_output_dir: Path, task_output_dir: Path) -> str | None:
    if not text:
        return None
    try:
        path = Path(text).expanduser()
    except OSError:
        return None
    if not path.is_absolute():
        return None
    resolved = path.resolve(strict=False)
    if not _same_or_inside(resolved, workspace_output_dir):
        return None
    suffix = resolved.relative_to(workspace_output_dir)
    return str((task_output_dir / suffix).resolve(strict=False))


def _map_output_ref_value(
    value: object,
    map_text: Callable[[str], str | None],
) -> tuple[object, bool]:
    if isinstance(value, list):
        changed = False
        items: list[object] = []
        for item in value:
            next_item, item_changed = _map_output_ref_value(item, map_text)
            items.append(next_item)
            changed = changed or item_changed
        return items, changed
    if isinstance(value, tuple):
        items, changed = _map_output_ref_value(list(value), map_text)
        return tuple(items) if isinstance(items, list) else items, changed
    if isinstance(value, Mapping):
        changed = False
        items: dict[object, object] = {}
        for item_key, item_value in value.items():
            next_value, item_changed = _map_output_ref_value(item_value, map_text)
            items[item_key] = next_value
            changed = changed or item_changed
        return items, changed
    if not isinstance(value, str):
        return value, False
    text = value.strip()
    mapped = map_text(text)
    return (mapped, True) if mapped is not None else (value, False)


def _current_task_output_dir(agent) -> Path | None:
    task_root = _current_task_root_path(agent)
    if task_root is None:
        return None
    return (task_root / "output").resolve(strict=False)


def _current_task_root_path(agent) -> Path | None:
    task_root = _current_task_root(agent)
    if not task_root:
        return None
    try:
        return Path(task_root).expanduser().resolve(strict=False)
    except OSError:
        return None


def _primary_workspace_output_dir(agent) -> Path | None:
    root = _primary_workspace_root(agent)
    if root is None:
        return None
    return (root / "output").resolve(strict=False)


def _primary_workspace_root(agent) -> Path | None:
    root = getattr(getattr(agent, "tools", None), "workspace_root", None) or getattr(agent, "root", "")
    if not isinstance(root, (str, Path)) or not str(root).strip():
        return None
    try:
        return Path(root).expanduser().resolve(strict=False)
    except OSError:
        return None


def _has_user_requested_output_dir(raw_params: dict[str, object], agent=None) -> bool:
    if str(raw_params.get("user_requested_output_dir") or "").strip():
        return True
    attrs = raw_params.get("attributes")
    if isinstance(attrs, dict) and _mapping_has_user_requested_output_dir(attrs):
        return True
    current = getattr(agent, "_current_run_params", None) if agent is not None else None
    current_attrs = getattr(current, "task_attributes", None)
    return isinstance(current_attrs, dict) and _mapping_has_user_requested_output_dir(current_attrs)


def _mapping_has_user_requested_output_dir(value: dict[str, object]) -> bool:
    if str(value.get("user_requested_output_dir") or "").strip():
        return True
    workspace = value.get("run_workspace")
    if isinstance(workspace, dict) and str(workspace.get("user_requested_output_dir") or "").strip():
        return True
    workspace = value.get("task_workspace")
    return isinstance(workspace, dict) and bool(str(workspace.get("user_requested_output_dir") or "").strip())


def _same_or_inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _add_current_task_workspace(attrs: dict[str, object], agent=None) -> None:
    if "run_workspace" in attrs:
        return
    task_root = _current_task_root(agent)
    if not task_root:
        return
    attrs["run_workspace"] = {
        "task_root": task_root,
        "work_dir": f"{task_root}/work",
        "output_dir": f"{task_root}/output",
    }


def _current_task_root(agent) -> str:
    raw = getattr(agent, "_current_run_task_workspace", "") if agent is not None else ""
    if not isinstance(raw, (str, Path)):
        return ""
    return str(raw).strip()


def _add_derived_output_refs(attrs: dict[str, object], raw_params: dict[str, object]) -> None:
    refs = params_output_refs(raw_params)
    if not refs:
        return
    existing = _list_attribute_values("output_refs", {"output_refs": attrs.get("output_refs")})
    merged = [*existing]
    for ref in refs:
        if ref not in merged:
            merged.append(ref)
    if merged:
        attrs["output_refs"] = merged


def _list_attribute_values(key: str, raw_params: dict[str, object]) -> list[str]:
    value = raw_params.get(key)
    if key in _OUTPUT_REF_ATTRIBUTE_FIELDS:
        return params_output_refs({key: value})
    if key in _INPUT_REF_ATTRIBUTE_FIELDS:
        return params_input_refs({key: value})
    return string_list(value, TOOL_TEXT_LIST_OPTIONS)


_LIST_ATTRIBUTE_FIELDS = (
    "artifact_refs",
    # covers(P1 派工账本绑定):派工方声明"这个子代理负责父 coverage 清单里哪些项 id"。
    #   子代理 DONE 后 dispatch_coverage_reconcile 按 id 把对应项标 done——语义绑定在派工时由
    #   模型完成,代码只载运 id(铁律:零自然语言匹配)。
    "covers",
    "forbidden_files",
    "input_files",
    "input_refs",
    "output_files",
    "output_refs",
    "qa_roles",
    "hierarchy_contracts",
    "capability_contracts",
    "required_content_lines",
    "required_dom_ids",
    "required_files",
    "required_qa_roles",
    "required_read_paths",
    "replacement_for_run_ids",
    "workflow_risk_tags",
)


def _default_owner_id(agent) -> str:
    home_paths = getattr(agent, "home_paths", None)
    owner_id = str(getattr(home_paths, "owner_id", "") or "").strip()
    if owner_id:
        return owner_id
    policy = getattr(agent, "owner_policy", None)
    return str(getattr(policy, "owner_id", "") or "").strip()


_OUTPUT_REF_ATTRIBUTE_FIELDS = frozenset({"artifact_refs", "output_files", "output_refs"})
_INPUT_REF_ATTRIBUTE_FIELDS = frozenset({"input_files", "input_refs", "required_read_paths"})
_MAPPING_ATTRIBUTE_FIELDS = ("required_content_files",)
# long_running: 派工方结构化声明"这个子代理是故意长期运行的守望/常驻任务"——runner 侧据此
#   放开 compact 自动续跑的固定深度硬顶(无进展活性软顶仍在,见 finalization_compact_auto)。
_BOOL_ATTRIBUTE_FIELDS = ("defer_start", "long_running")
# service_window_seconds(A4 持续型委派语义):持续型任务的最短值守窗口(秒)。子代理收口
#   层据此抑制"落一次产物即 DONE"的提前收工;父代理 wake 消费据此判断"窗口未走完就退了"。
_POSITIVE_INT_ATTRIBUTE_FIELDS = ("service_window_seconds",)
_SCALAR_ATTRIBUTE_FIELDS = (
    "preferred_workflow_template",
    "subagent_workflow_template",
    "workflow_task_type",
    "workflow_template_id",
)


def _root_agent_name(raw_params: dict[str, object], role: str) -> str:
    explicit = str(raw_params.get("agent_name") or "").strip().strip("-")
    if explicit:
        return explicit
    role_name = _agent_name_from_role_field(raw_params)
    if role_name:
        return role_name
    suffix = str(role or "worker").strip().replace("_", "-").strip("-") or "worker"
    if suffix in {"general", "child"}:
        suffix = "worker"
    return f"agent-d1-{suffix}"


def _agent_name_from_role_field(raw_params: dict[str, object]) -> str:
    del raw_params
    return ""


def _create_plan(raw_params: dict[str, object]) -> list[str]:
    explicit = string_list(raw_params.get("plan"), TOOL_TEXT_LIST_OPTIONS)
    if explicit:
        return explicit
    task_hints = _child_task_hint_plan(raw_params.get("children"))
    if task_hints:
        return [
            "理解父级目标和可用资料",
            *task_hints,
            "汇总下级结果、证据 refs 和阻塞项",
            "交回真实结果和证据",
        ]
    return list(SUBAGENT_DEFAULT_PLAN)


def _child_task_hint_plan(value: object) -> list[str]:
    items = _json_child_items(value)
    lines: list[str] = []
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            continue
        goal = str(item.get("goal") or "").strip()
        if not goal:
            continue
        role = str(item.get("role") or "worker").strip() or "worker"
        name = str(item.get("agent_name") or "").strip()
        suffix = f"（role={role}{', agent_name=' + name if name else ''}）"
        lines.append(f"按需创建/调度下级任务 {index}: {goal}{suffix}")
    return lines


def _json_child_items(value: object) -> list[object]:
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return [value]
    text = str(value or "").strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return []
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        return [parsed]
    return []


# 后续子/孙代理可用自己的 run_id 反查会话，不要求模型手填 thread_id。
def add_current_conversation_attrs(attrs: dict[str, object], agent) -> None:
    if agent is None:
        return
    current = getattr(agent, "_current_run_params", None)
    raw_task_id = getattr(current, "task_id", "") if current is not None else ""
    if not isinstance(raw_task_id, str):
        return
    task_id = raw_task_id.strip()
    if not task_id:
        return
    lookup_error = None
    try:
        thread = agent.conversation_store.thread_for_task(task_id)
    except Exception as exc:
        lookup_error = exc
        thread = None
    materialize_error = None
    if thread is None:
        thread, materialize_error = _materialize_internal_thread(agent, current, task_id)
    if thread is None:
        _attach_conversation_errors(attrs, lookup_error, materialize_error)
    _attach_thread_attrs(attrs, thread, task_id)


def _materialize_internal_thread(agent, current, task_id: str) -> tuple[object | None, BaseException | None]:
    store = getattr(agent, "conversation_store", None)
    if store is None:
        return None, None
    goal = str(getattr(current, "root_user_prompt", "") or getattr(current, "prompt", "") or task_id).strip()
    try:
        thread = store.get_or_create_thread(
            {
                "canonical_user_id": "local-agent",
                "channel": "internal",
                "channel_conversation_id": f"task:{task_id}",
                "channel_user_id": "local-main-agent",
                "title": goal[:80] or task_id,
            }
        )
        store.bind_task(
            {
                "thread_id": thread.thread_id,
                "task_id": task_id,
                "goal": goal or task_id,
                "status": "active",
            }
        )
        return thread, None
    except Exception as exc:
        return None, exc


def _attach_conversation_errors(
    attrs: dict[str, object],
    lookup_error: BaseException | None,
    materialize_error: BaseException | None,
) -> None:
    if lookup_error is not None:
        attrs.setdefault(
            "conversation_thread_lookup_error",
            runtime_error_report(lookup_error, context="conversation.thread_for_task"),
        )
    if materialize_error is not None:
        attrs.setdefault(
            "conversation_thread_materialize_error",
            runtime_error_report(materialize_error, context="conversation.materialize_internal_thread"),
        )


def _attach_thread_attrs(attrs: dict[str, object], thread: object, task_id: str) -> None:
    thread_id = getattr(thread, "thread_id", "") if thread is not None else ""
    if not isinstance(thread_id, str) or not thread_id.strip():
        return
    attrs.setdefault("conversation_thread_id", thread_id.strip())
    attrs.setdefault("conversation_task_id", task_id)


__all__ = ["add_current_conversation_attrs"]
