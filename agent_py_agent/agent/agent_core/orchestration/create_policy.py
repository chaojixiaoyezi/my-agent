
from __future__ import annotations

import json
from pathlib import Path

from ...common.value_parsing import TOOL_TEXT_LIST_OPTIONS, bool_value, string_list
from ...settings.defaults import DEFAULT_COMMAND_ACCESS_MODE
from ...subagents.role_templates import COORDINATOR_TOOLS, role_template_snapshot_for_role
from ...subagents.services.base import CreateRunParams
from ...subagents.services.workflow import tool_workflow_mode
from ..parameters import _bool_param, _positive_int
from ..runner.ref_fields import params_input_refs, params_output_refs
from ..spawn_role_seed import is_explicit_root_role
from .create_constraints import (
    resolved_extra_write_roots,
    role_allows_direct_product_work,
)
from .create_context import create_context_manifest, create_context_packs
from .create_conversation import add_current_conversation_attrs
from .work_scope import add_work_scope_key


def create_run_params(
    agent,
    raw_params: dict[str, object],
    goal: str,
    allowed_tools: list[str] | None,
):
    raw_params = _params_with_task_output_defaults(raw_params, agent)
    workflow_mode = tool_workflow_mode(raw_params.get("workflow_mode"), agent.config.subagent_workflow_mode)
    role = _role_from_create_intent(raw_params, goal, agent)
    role_template_dirs = _role_template_dirs(agent)
    is_explicit_root = is_explicit_root_role(role, role_template_dirs)
    if is_explicit_root:
        workflow_mode = "off"
        allowed_tools = explicit_root_allowed_tools(allowed_tools)
    elif _should_disable_generic_workflow_for_concrete_worker(raw_params, goal, role, workflow_mode):
        workflow_mode = "off"
        if role not in {"child_worker", "leaf_worker"}:
            role = "worker"
    return CreateRunParams(
        goal=goal,
        thought=str(raw_params.get("thought") or "根据父代理派工执行，并保留可验收证据。").strip(),
        plan=_create_plan(raw_params),
        agent_name=_root_agent_name(raw_params, role),
        role=role,
        allowed_tools=allowed_tools,
        owner=str(raw_params.get("owner") or _default_owner_id(agent)).strip(),
        supervisor=str(raw_params.get("supervisor") or "parent").strip(),
        final_owner=str(raw_params.get("final_owner") or "").strip(),
        acceptance_checks=string_list(raw_params.get("acceptance_checks"), TOOL_TEXT_LIST_OPTIONS),
        extra_write_roots=resolved_extra_write_roots(agent, raw_params, goal),
        context_manifest=create_context_manifest(raw_params),
        context_packs=create_context_packs(raw_params),
        workflow_mode=workflow_mode,
        attributes=_create_attributes(raw_params, agent),
        **_lineage_fields(raw_params, agent),
        parent_access_mode=_config_access_mode(agent),
        memory_retention_policy=_config_string(
            agent,
            "subagent_memory_retention_policy",
            "parent_review_or_cleanup",
        ),
        memory_delete_after_days=_config_int(agent, "subagent_memory_delete_after_days", 0),
        destroy_summary_required=_config_bool(agent, "subagent_destroy_summary_required", True),
    )


def _lineage_fields(raw_params: dict[str, object], agent) -> dict[str, object]:
    explicit_parent = str(raw_params.get("parent_id") or "").strip()
    explicit_root = str(raw_params.get("root_id") or "").strip()
    explicit_depth = raw_params.get("depth")
    parent_id = explicit_parent or _current_run_id(agent)
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


def _should_disable_generic_workflow_for_concrete_worker(
    raw_params: dict[str, object],
    goal: str,
    role: str,
    workflow_mode: str,
) -> bool:
    if workflow_mode != "auto":
        return False
    if not role_allows_direct_product_work(role):
        return False
    if _positive_int(raw_params.get("count"), default=1) <= 0:
        return False
    return bool(params_output_refs(raw_params))


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
    _add_derived_output_refs(attrs, raw_params)
    add_work_scope_key(attrs)
    add_current_conversation_attrs(attrs, agent)
    _add_current_task_workspace(attrs, agent)
    return attrs


def _params_with_task_output_defaults(raw_params: dict[str, object], agent=None) -> dict[str, object]:
    task_output_dir = _current_task_output_dir(agent)
    workspace_output_dir = _primary_workspace_output_dir(agent)
    if not task_output_dir or not workspace_output_dir or _has_user_requested_output_dir(raw_params, agent):
        return raw_params
    updated = dict(raw_params)
    changed = False
    for key in _OUTPUT_REF_ATTRIBUTE_FIELDS:
        if key not in updated:
            continue
        value, value_changed = _rebase_output_ref_value(updated.get(key), workspace_output_dir, task_output_dir)
        if value_changed:
            updated[key] = value
            changed = True
    attrs = updated.get("attributes")
    if isinstance(attrs, dict):
        next_attrs = dict(attrs)
        attrs_changed = False
        for key in _OUTPUT_REF_ATTRIBUTE_FIELDS:
            if key not in next_attrs:
                continue
            value, value_changed = _rebase_output_ref_value(next_attrs.get(key), workspace_output_dir, task_output_dir)
            if value_changed:
                next_attrs[key] = value
                attrs_changed = True
        if attrs_changed:
            updated["attributes"] = next_attrs
            changed = True
    return updated if changed else raw_params


def _rebase_output_ref_value(value: object, workspace_output_dir: Path, task_output_dir: Path) -> tuple[object, bool]:
    if isinstance(value, list):
        changed = False
        items: list[object] = []
        for item in value:
            next_item, item_changed = _rebase_output_ref_value(item, workspace_output_dir, task_output_dir)
            items.append(next_item)
            changed = changed or item_changed
        return items, changed
    if not isinstance(value, str):
        return value, False
    text = value.strip()
    if not text:
        return value, False
    try:
        path = Path(text).expanduser()
    except OSError:
        return value, False
    if not path.is_absolute():
        return value, False
    resolved = path.resolve(strict=False)
    if not _same_or_inside(resolved, workspace_output_dir):
        return value, False
    suffix = resolved.relative_to(workspace_output_dir)
    return str((task_output_dir / suffix).resolve(strict=False)), True


def _current_task_output_dir(agent) -> Path | None:
    task_root = _current_task_root(agent)
    if not task_root:
        return None
    return (Path(task_root).expanduser() / "output").resolve(strict=False)


def _primary_workspace_output_dir(agent) -> Path | None:
    root = getattr(getattr(agent, "tools", None), "workspace_root", None) or getattr(agent, "root", "")
    if not isinstance(root, (str, Path)) or not str(root).strip():
        return None
    try:
        return (Path(root).expanduser().resolve(strict=False) / "output").resolve(strict=False)
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
_BOOL_ATTRIBUTE_FIELDS = ("defer_start",)
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
    return ["理解目标", "执行任务", "产出证据", "交回真实结果和证据"]


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
