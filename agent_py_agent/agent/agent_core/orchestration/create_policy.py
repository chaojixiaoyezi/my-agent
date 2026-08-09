"""创建子代理的策略入口：参数构建、角色模板、工具授权、会话属性（原 create_policy.py / create_conversation.py 并入）。"""

from __future__ import annotations

import json
import math
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from ...common.value_parsing import TOOL_TEXT_LIST_OPTIONS, bool_value, string_list
from ...conversation.authority import CONVERSATION_REQUEST_ID_ATTR
from ...runtime_errors import runtime_error_report
from ...settings.defaults import DEFAULT_COMMAND_ACCESS_MODE
from ...subagents.role_templates import COORDINATOR_TOOLS, role_template_snapshot_for_role
from ...subagents.services.base import CreateRunParams
from ..parameters import _bool_param, _positive_int
from ..runner.prompts import SUBAGENT_DEFAULT_PLAN, SUBAGENT_DEFAULT_THOUGHT
from ..runner.ref_fields import params_input_refs, params_output_refs
from ..spawn_role_seed import is_explicit_root_role
from .create_constraints import (
    resolved_extra_write_roots,
)
from .create_context import create_context_manifest, create_context_packs
from .work_scope import add_work_scope_key


@dataclass(frozen=True)
class RolePolicy:
    role: str
    allowed_tools: list[str] | None


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
    if not str(raw_params.get("goal") or "").strip() and str(goal or "").strip():
        raw_params = {**raw_params, "goal": goal}
    raw_params = _params_with_task_output_defaults(raw_params, agent)
    goal = str(raw_params.get("goal") or goal).strip()
    role_policy = _role_policy(agent, raw_params, goal, allowed_tools)
    return _create_run_params_from_build(
        CreateRunBuildRequest(agent, raw_params, goal, role_policy)
    )


def prepare_audit_child_creation_scope(
    agent: object,
    raw_params: dict[str, object],
) -> tuple[dict[str, object], str]:
    """Narrow a direct Audit leaf before its first source binding.

    会话运行时 applies an agent role before the child turn starts and 长期助手 builds
    the child's inherited toolset before construction.  my-agent follows that
    same lifecycle boundary here: an ordinary Audit leaf starts as a
    least-privilege source-binding run, then ``watch_stream(open)`` atomically
    adopts that same run as the durable source worker.  A validated finding
    investigation and a real coordinator keep their ordinary task scope.
    """

    from ...common.audit_activation import (
        AUDIT_ATTR,
        AUDIT_OBJECTIVE_ATTR,
        AUDIT_RUN_PROMPT_ATTR,
        AUDIT_SOURCE_BINDING_PENDING_ATTR,
        AUDIT_SOURCE_BINDING_TOOLS,
        AUDIT_SOURCE_OPEN_ATTR,
        attributes_request_audit,
        audit_worker_slice_seconds,
        current_audit_attributes,
        structured_audit_source_worker_attributes,
    )
    from .finding_relation import structured_audit_finding_relation

    attrs = (
        dict(raw_params.get("attributes") or {})
        if isinstance(raw_params.get("attributes"), dict)
        else {}
    )
    # A model-proposed attributes object never owns a source binding.  The
    # exact row below is resolved only from the current named Audit's published
    # structured facts.
    attrs.pop(AUDIT_SOURCE_OPEN_ATTR, None)
    if structured_audit_source_worker_attributes(attrs) or structured_audit_finding_relation(attrs):
        return raw_params, ""
    current_attrs = current_audit_attributes(agent)
    if not attributes_request_audit(current_attrs):
        return raw_params, ""
    role = str(raw_params.get("role") or "worker").strip() or "worker"
    role_snapshot = role_template_snapshot_for_role(
        role,
        _role_template_dirs(agent),
    )
    if bool(role_snapshot.get("can_spawn_children")):
        return raw_params, ""
    audit_id = (
        str(current_attrs.get(CONVERSATION_REQUEST_ID_ATTR) or "").strip()
        if isinstance(current_attrs, dict)
        else ""
    )
    if not audit_id:
        return raw_params, "当前 Audit 缺少结构化任务编号，不能安全创建来源工作者。"

    updated = dict(raw_params)
    attrs[AUDIT_ATTR] = True
    attrs[AUDIT_OBJECTIVE_ATTR] = str((current_attrs or {}).get(AUDIT_OBJECTIVE_ATTR) or "")
    attrs[AUDIT_RUN_PROMPT_ATTR] = str((current_attrs or {}).get(AUDIT_RUN_PROMPT_ATTR) or "")
    attrs[AUDIT_SOURCE_BINDING_PENDING_ATTR] = True
    attrs[CONVERSATION_REQUEST_ID_ATTR] = audit_id
    try:
        published_bindings = _current_published_audit_bindings(
            agent,
            audit_id,
            current_attrs,
        )
    except ValueError as exc:
        return raw_params, str(exc)
    binding_error = _apply_published_audit_binding(
        agent,
        raw_params,
        current_attrs,
        attrs,
        audit_id,
        published_bindings,
    )
    if binding_error:
        return raw_params, binding_error
    attrs["dynamic_timeout_seconds"] = audit_worker_slice_seconds(agent)
    attrs.pop("skill_snapshot_refs", None)
    for key in (*_OUTPUT_REF_ATTRIBUTE_FIELDS, "system_default_output_ref"):
        attrs.pop(key, None)
        updated.pop(key, None)
    updated.update(
        {
            "attributes": attrs,
            "role": "worker",
            "allowed_tools": list(AUDIT_SOURCE_BINDING_TOOLS),
            "allowed_skills": [],
            "context_packs": [],
            "extra_write_roots": [],
            "acceptance_checks": [],
            "long_running": True,
            "_exact_allowed_tools": True,
            "_include_goal_file_hints": False,
        }
    )
    return updated, ""


def _apply_published_audit_binding(
    agent: object,
    raw_params: dict[str, object],
    current_attrs: dict[str, object],
    attrs: dict[str, object],
    audit_id: str,
    published_bindings: list[dict[str, object]],
) -> str:
    """Attach one exact published source to a least-privilege Audit leaf."""

    if not published_bindings:
        return ""
    from ...common.audit_activation import (
        AUDIT_RUN_EPOCH_ATTR,
        AUDIT_SOURCE_ID_ATTR,
        AUDIT_SOURCE_OPEN_ATTR,
        audit_source_work_scope_key,
        audit_source_worker_key,
        audit_watch_scope_id,
    )
    from ...ingestion.source_binding import audit_source_binding_by_id
    from ...ingestion.watch_state import watch_id_for

    source_id = str(raw_params.get("audit_source_id") or "").strip()
    if not source_id:
        available = [
            str(item.get("source_id") or "").strip()
            for item in published_bindings
            if isinstance(item, dict) and str(item.get("source_id") or "").strip()
        ]
        return (
            "当前 Audit 已发布结构化来源；每个叶子 item 必须用 audit_source_id 选择恰好一条。"
            f" 可用 source_id: {available}"
        )
    binding = audit_source_binding_by_id(published_bindings, source_id)
    if binding is None:
        return f"audit_source_id 不属于当前 Audit: {source_id}"
    owner_home = str(
        getattr(getattr(agent, "home_paths", None), "owner_home_dir", "") or ""
    ).strip()
    if not owner_home:
        return "当前 owner home 不可用，不能建立来源工作者。"
    run_epoch = max(0, int(current_attrs.get(AUDIT_RUN_EPOCH_ATTR) or 0))
    attrs[AUDIT_RUN_EPOCH_ATTR] = run_epoch
    watch_id = watch_id_for(
        Path(owner_home),
        str(binding.get("url") or ""),
        audit_watch_scope_id(audit_id, run_epoch),
    )
    worker_key = audit_source_worker_key(audit_id, watch_id)
    attrs[AUDIT_SOURCE_ID_ATTR] = source_id
    attrs[AUDIT_SOURCE_OPEN_ATTR] = binding
    attrs["work_scope_key"] = audit_source_work_scope_key(worker_key, run_epoch)
    return ""


def _current_published_audit_bindings(
    agent: object,
    audit_id: str,
    current_attrs: dict[str, object],
) -> list[dict[str, object]]:
    """Load the same typed bindings for foreground and background Audit turns.

    Background wake turns can carry only the stable Audit id.  Falling back to
    an unconstrained child in that case lets a coordinator recreate ordinary
    workers from prose.  The durable named-task link is therefore the second
    authoritative source after the current run snapshot; model text is never
    consulted.
    """

    from ...common.audit_activation import AUDIT_SOURCE_BINDINGS_ATTR
    from ...ingestion.source_binding import normalize_audit_source_bindings

    raw = current_attrs.get(AUDIT_SOURCE_BINDINGS_ATTR)
    if raw is None:
        store = getattr(agent, "conversation_store", None)
        loader = getattr(store, "load_task_link", None)
        if callable(loader):
            try:
                link = loader(audit_id)
            except Exception as exc:
                raise ValueError(
                    "当前 Audit 的结构化来源状态不可读，不能安全创建来源工作者。"
                ) from exc
            if (
                link is not None
                and str(getattr(link, "task_id", "") or "").strip() == audit_id
                and str(getattr(link, "work_kind", "") or "").strip().lower() == "audit"
            ):
                raw = list(getattr(link, "effective_source_bindings", ()) or ())
    if raw is None:
        return []
    try:
        normalized = normalize_audit_source_bindings(
            list(raw) if isinstance(raw, (list, tuple)) else raw
        )
    except ValueError as exc:
        raise ValueError("当前 Audit 的结构化来源状态无效，不能安全创建来源工作者。") from exc
    return [dict(item) for item in normalized]


def _role_policy(
    agent,
    raw_params: dict[str, object],
    goal: str,
    allowed_tools: list[str] | None,
) -> RolePolicy:
    role = _role_from_create_intent(raw_params, goal, agent)
    role_template_dirs = _role_template_dirs(agent)
    is_explicit_root = is_explicit_root_role(role, role_template_dirs)
    if is_explicit_root:
        allowed_tools = explicit_root_allowed_tools(allowed_tools)
    return RolePolicy(role=role, allowed_tools=allowed_tools)


def _create_run_params_from_build(request: CreateRunBuildRequest) -> CreateRunParams:
    raw_params = request.raw_params
    role_policy = request.role_policy
    return CreateRunParams(
        goal=request.goal,
        thought=str(raw_params.get("thought") or SUBAGENT_DEFAULT_THOUGHT).strip(),
        plan=_create_plan(raw_params),
        agent_name=_root_agent_name(raw_params, role_policy.role),
        role=role_policy.role,
        allowed_skills=string_list(raw_params.get("allowed_skills"), TOOL_TEXT_LIST_OPTIONS),
        allowed_tools=role_policy.allowed_tools,
        owner=str(raw_params.get("owner") or _default_owner_id(request.agent)).strip(),
        supervisor=str(raw_params.get("supervisor") or "parent").strip(),
        final_owner=str(raw_params.get("final_owner") or "").strip(),
        acceptance_checks=string_list(raw_params.get("acceptance_checks"), TOOL_TEXT_LIST_OPTIONS),
        extra_write_roots=resolved_extra_write_roots(request.agent, raw_params, request.goal),
        context_manifest=create_context_manifest(raw_params),
        context_packs=create_context_packs(raw_params),
        attributes=_create_attributes(raw_params, request.agent),
        **_lineage_fields(raw_params, request.agent),
        parent_access_mode=_config_access_mode(request.agent),
        memory_retention_policy=_config_string(
            request.agent,
            "subagent_memory_retention_policy",
            "parent_review_or_cleanup",
        ),
        memory_delete_after_days=_config_int(request.agent, "subagent_memory_delete_after_days", 0),
        destroy_summary_required=_config_bool(
            request.agent, "subagent_destroy_summary_required", True
        ),
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
    # request_id 兜底:gateway 普通轮无 run_id/task_id(身份在 promote 时才绑定),
    # 缺此兜底子代理 parent_id 为空,子代理成为自己的 root,主任务与子代理
    # 的 task_id 对不上(lineage 断裂,问题2)。request_id 是请求实例 id,语义
    # 与 run_id 同级,仅作兜底不抢 run_id/task_id 的优先级。
    return str(
        getattr(current, "run_id", "")
        or getattr(current, "task_id", "")
        or getattr(current, "request_id", "")
        or ""
    ).strip()


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
    if (
        role == "worker"
        and _has_child_dispatch_tool(raw_params)
        and not _role_depends_on_outputs(raw_params, agent)
    ):
        return "coordinator"
    return role


def _has_child_dispatch_tool(raw_params: dict[str, object]) -> bool:
    if raw_params.get("_item_allowed_tools_explicit") is False:
        return False
    tools = {
        str(item or "").strip().lower()
        for item in string_list(raw_params.get("allowed_tools"), TOOL_TEXT_LIST_OPTIONS)
    }
    return bool({"schedule_child_subagents", "dispatch_subagents"}.intersection(tools))


def _role_depends_on_outputs(raw_params: dict[str, object], agent) -> bool:
    snapshot = role_template_snapshot_for_role(
        str(raw_params.get("role") or ""), _role_template_dirs(agent)
    )
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


def _create_attributes(raw_params: dict[str, object], agent=None) -> dict[str, object]:
    attrs = (
        dict(raw_params.get("attributes") or {})
        if isinstance(raw_params.get("attributes"), dict)
        else {}
    )
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
        if (
            key in raw_params
            and key not in attrs
            and _positive_int(raw_params.get(key), default=0) > 0
        ):
            attrs[key] = _positive_int(raw_params.get(key), default=0)
    _add_derived_output_refs(attrs, raw_params)
    add_work_scope_key(attrs)
    add_current_conversation_attrs(attrs, agent)
    _inherit_conversation_request_id(attrs, agent)
    _add_current_task_workspace(attrs, agent)
    _inherit_audit_guarantee(attrs, agent)
    _clamp_service_window_to_audit_deadline(attrs)
    return attrs


# LLM: Every descendant keeps the originating ordinary-conversation request id as typed
# lineage, so status/stop never infer ownership from a goal or generated agent name.
# 函数用途：把当前会话请求编号结构化传给新建子代理。
def _inherit_conversation_request_id(attrs: dict[str, object], agent: object) -> None:
    if str(attrs.get(CONVERSATION_REQUEST_ID_ATTR) or "").strip():
        return
    current = getattr(agent, "_current_run_params", None)
    current_attrs = getattr(current, "task_attributes", None) if current is not None else None
    inherited = (
        str(current_attrs.get(CONVERSATION_REQUEST_ID_ATTR) or "").strip()
        if isinstance(current_attrs, dict)
        else ""
    )
    request_id = inherited or str(getattr(current, "request_id", "") or "").strip()
    if request_id:
        attrs[CONVERSATION_REQUEST_ID_ATTR] = request_id


def _inherit_audit_guarantee(attrs: dict[str, object], agent) -> None:
    """/audit 契约沿 spawn 树结构化下传:父任务(主代理或上层子代理)处于保证档时,派出的
    每个子代理 task.attributes 也带上保证档标志——委派做盯守的判读子代理/孙代理照样在保证档
    (用户明确要"命令一加子代理也一样")。继承靠结构化 attributes、不靠 goal 文本是否恰好带
    /audit 词元(真机缺口:子代理 goal 空、词元在 runner_prompt 里,靠文本必漏)。"""
    from ...common.audit_activation import (
        AUDIT_ATTR,
        AUDIT_DEADLINE_ATTR,
        AUDIT_OBJECTIVE_ATTR,
        AUDIT_RUN_EPOCH_ATTR,
        AUDIT_RUN_PROMPT_ATTR,
        AUDIT_WINDOW_ATTR,
        attributes_request_audit,
    )

    current = getattr(agent, "_current_run_params", None)
    current_attrs = getattr(current, "task_attributes", None)
    if attributes_request_audit(current_attrs):
        attrs[AUDIT_ATTR] = True
        if AUDIT_WINDOW_ATTR not in attrs:
            window = current_attrs.get(AUDIT_WINDOW_ATTR)
            if window:
                attrs[AUDIT_WINDOW_ATTR] = window
        if AUDIT_OBJECTIVE_ATTR not in attrs:
            objective = current_attrs.get(AUDIT_OBJECTIVE_ATTR)
            if isinstance(objective, str) and objective.strip():
                attrs[AUDIT_OBJECTIVE_ATTR] = objective.strip()
        if AUDIT_RUN_PROMPT_ATTR not in attrs:
            run_prompt = current_attrs.get(AUDIT_RUN_PROMPT_ATTR)
            if isinstance(run_prompt, str) and run_prompt.strip():
                attrs[AUDIT_RUN_PROMPT_ATTR] = run_prompt.strip()
        if AUDIT_DEADLINE_ATTR not in attrs:
            deadline = current_attrs.get(AUDIT_DEADLINE_ATTR)
            if deadline:
                attrs[AUDIT_DEADLINE_ATTR] = deadline
        if AUDIT_RUN_EPOCH_ATTR not in attrs:
            attrs[AUDIT_RUN_EPOCH_ATTR] = max(
                0,
                int(current_attrs.get(AUDIT_RUN_EPOCH_ATTR) or 0),
            )
        return
    try:
        from ..runner.context import current_task_attributes

        runner_attrs = current_task_attributes(agent)
        if attributes_request_audit(runner_attrs):
            attrs[AUDIT_ATTR] = True
            if AUDIT_WINDOW_ATTR not in attrs:
                window = runner_attrs.get(AUDIT_WINDOW_ATTR)
                if window:
                    attrs[AUDIT_WINDOW_ATTR] = window
            if AUDIT_OBJECTIVE_ATTR not in attrs:
                objective = runner_attrs.get(AUDIT_OBJECTIVE_ATTR)
                if isinstance(objective, str) and objective.strip():
                    attrs[AUDIT_OBJECTIVE_ATTR] = objective.strip()
            if AUDIT_RUN_PROMPT_ATTR not in attrs:
                run_prompt = runner_attrs.get(AUDIT_RUN_PROMPT_ATTR)
                if isinstance(run_prompt, str) and run_prompt.strip():
                    attrs[AUDIT_RUN_PROMPT_ATTR] = run_prompt.strip()
            if AUDIT_DEADLINE_ATTR not in attrs:
                deadline = runner_attrs.get(AUDIT_DEADLINE_ATTR)
                if deadline:
                    attrs[AUDIT_DEADLINE_ATTR] = deadline
            if AUDIT_RUN_EPOCH_ATTR not in attrs:
                attrs[AUDIT_RUN_EPOCH_ATTR] = max(
                    0,
                    int(runner_attrs.get(AUDIT_RUN_EPOCH_ATTR) or 0),
                )
    except Exception:
        pass


def _clamp_service_window_to_audit_deadline(attrs: dict[str, object]) -> None:
    """Keep a declared long-running descendant inside its inherited Audit window.

    The model may choose a shorter service slice, but it cannot extend a child
    beyond the named Audit deadline.  This is based only on typed attributes;
    ordinary children and one-shot Audit investigations are unchanged.
    """
    if not bool_value(attrs.get("long_running"), default=False):
        return
    from ...common.audit_activation import AUDIT_DEADLINE_ATTR

    try:
        deadline = float(attrs.get(AUDIT_DEADLINE_ATTR) or 0.0)
    except (TypeError, ValueError):
        return
    if deadline <= 0:
        return
    remaining = max(0, math.ceil(deadline - time.time()))
    try:
        declared = int(attrs.get("service_window_seconds") or 0)
    except (TypeError, ValueError):
        declared = 0
    attrs["service_window_seconds"] = min(declared, remaining) if declared > 0 else remaining


def _params_with_task_output_defaults(
    raw_params: dict[str, object], agent=None
) -> dict[str, object]:
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
        changed = (
            _normalize_owner_home_output_refs(
                updated,
                agent=agent,
                task_root=task_root,
                task_output_dir=task_output_dir,
            )
            or changed
        )
    if workspace_root:
        changed = (
            _normalize_current_task_output_refs(updated, workspace_root, task_output_dir) or changed
        )
    changed = _normalize_relative_task_output_refs(updated, task_output_dir) or changed
    if not workspace_output_dir:
        return updated if changed else raw_params
    for key in _OUTPUT_REF_ATTRIBUTE_FIELDS:
        changed = (
            _rebase_output_ref_field(updated, key, workspace_output_dir, task_output_dir) or changed
        )
    attrs_changed = _rebase_attribute_output_refs(updated, workspace_output_dir, task_output_dir)
    changed = changed or attrs_changed
    return updated if changed else raw_params


def _normalize_owner_home_output_refs(
    updated: dict[str, object],
    *,
    agent: object,
    task_root: Path,
    task_output_dir: Path,
) -> bool:
    """Keep model-invented owner-home deliverables inside the current task."""
    raw_owner_home = getattr(getattr(agent, "home_paths", None), "owner_home_dir", "")
    if not isinstance(raw_owner_home, str | Path) or not str(raw_owner_home).strip():
        return False
    try:
        owner_home = Path(raw_owner_home).expanduser().resolve(strict=False)
    except OSError:
        return False
    _seed_owner_task_output_refs(updated, owner_home)
    replacements: dict[str, str] = {}

    def mapper(text: str) -> str | None:
        mapped = _owner_home_task_output_ref(text, owner_home, task_root, task_output_dir)
        if mapped is not None and mapped != text:
            replacements[text] = mapped
        return mapped

    changed = False
    for key in _OUTPUT_REF_ATTRIBUTE_FIELDS:
        if key not in updated:
            continue
        value, value_changed = _map_output_ref_value(updated.get(key), mapper)
        if value_changed:
            updated[key] = value
            changed = True
    changed = _map_nested_owner_output_refs(updated, mapper) or changed
    if replacements:
        _rewrite_output_ref_mentions(updated, replacements)
    return changed


def _map_nested_owner_output_refs(
    updated: dict[str, object],
    mapper: Callable[[str], str | None],
) -> bool:
    attrs = updated.get("attributes")
    if not isinstance(attrs, dict):
        return False
    next_attrs = dict(attrs)
    changed = False
    for key in _OUTPUT_REF_ATTRIBUTE_FIELDS:
        value, value_changed = _map_output_ref_value(next_attrs.get(key), mapper)
        if key in next_attrs and value_changed:
            next_attrs[key] = value
            changed = True
    if changed:
        updated["attributes"] = next_attrs
    return changed


def _owner_home_task_output_ref(
    text: str,
    owner_home: Path,
    task_root: Path,
    task_output_dir: Path,
) -> str | None:
    if not text or "://" in text:
        return None
    try:
        path = Path(text).expanduser()
    except OSError:
        return None
    if not path.is_absolute():
        return None
    try:
        resolved = path.resolve(strict=False)
    except OSError:
        return None
    if not _same_or_inside(resolved, owner_home) or _same_or_inside(resolved, task_root):
        return None
    try:
        suffix = resolved.relative_to(owner_home)
    except ValueError:
        return None
    # A path copied from another task must never recreate an entire tasks/... tree
    # below this task's output directory. Preserve the declared tail below output/
    # (project directory + file); without an output segment preserve only basename.
    if suffix.parts and suffix.parts[0] == "tasks":
        parts = suffix.parts
        try:
            output_index = parts.index("output", 1)
        except ValueError:
            suffix = Path(resolved.name)
        else:
            tail = parts[output_index + 1 :]
            suffix = Path(*tail) if tail else Path()
    return str((task_output_dir / suffix).resolve(strict=False))


def _seed_owner_task_output_refs(updated: dict[str, object], owner_home: Path) -> None:
    explicit_refs = _owner_task_output_path_tokens(
        (updated.get("goal"), updated.get("thought"), updated.get("plan")),
        owner_home,
    )
    if explicit_refs:
        existing_refs = params_output_refs({"output_refs": updated.get("output_refs")})
        updated["output_refs"] = list(dict.fromkeys([*existing_refs, *explicit_refs]))


def _owner_task_output_path_tokens(value: object, owner_home: Path) -> list[str]:
    texts: list[str] = []
    if isinstance(value, str):
        texts.append(value)
    elif isinstance(value, (list, tuple)):
        texts.extend(item for item in value if isinstance(item, str))
    if not texts:
        return []
    prefix = re.escape(str(owner_home).rstrip("/"))
    pattern = re.compile(prefix + r"/tasks/[^\s`\"'“”‘’<>]+")
    tokens: list[str] = []
    trailing = ",.;:!?，。；：！？）)]}】》"
    for match in pattern.finditer("\n".join(texts)):
        token = match.group(0).rstrip(trailing)
        parts = Path(token).parts
        if "output" not in parts or token in tokens:
            continue
        tokens.append(token)
    return tokens


def _rewrite_output_ref_mentions(updated: dict[str, object], replacements: dict[str, str]) -> None:
    for key in ("goal", "thought", "plan"):
        if key in updated:
            updated[key] = _replace_output_ref_mentions(updated[key], replacements)


def _replace_output_ref_mentions(value: object, replacements: dict[str, str]) -> object:
    if isinstance(value, str):
        text = value
        for source, target in sorted(
            replacements.items(), key=lambda item: len(item[0]), reverse=True
        ):
            text = text.replace(source, target)
        return text
    if isinstance(value, list):
        return [_replace_output_ref_mentions(item, replacements) for item in value]
    if isinstance(value, tuple):
        return tuple(_replace_output_ref_mentions(item, replacements) for item in value)
    return value


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
            attrs_changed = (
                _normalize_current_task_workspace_ref_field(next_attrs, key, task_root)
                or attrs_changed
            )
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


def _normalize_current_task_workspace_ref_value(
    value: object, task_root: Path
) -> tuple[object, bool]:
    return _map_output_ref_value(
        value, lambda text: _current_task_workspace_ref_text(text, task_root)
    )


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
        changed = (
            _normalize_current_task_output_ref_field(updated, key, workspace_root, task_output_dir)
            or changed
        )
    attrs = updated.get("attributes")
    if isinstance(attrs, dict):
        next_attrs = dict(attrs)
        attrs_changed = False
        for key in _OUTPUT_REF_ATTRIBUTE_FIELDS:
            attrs_changed = (
                _normalize_current_task_output_ref_field(
                    next_attrs, key, workspace_root, task_output_dir
                )
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
        changed = (
            _normalize_relative_task_output_ref_field(updated, key, task_output_dir) or changed
        )
    attrs = updated.get("attributes")
    if isinstance(attrs, dict):
        next_attrs = dict(attrs)
        attrs_changed = False
        for key in _OUTPUT_REF_ATTRIBUTE_FIELDS:
            attrs_changed = (
                _normalize_relative_task_output_ref_field(next_attrs, key, task_output_dir)
                or attrs_changed
            )
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


def _normalize_relative_task_output_ref_value(
    value: object, task_output_dir: Path
) -> tuple[object, bool]:
    return _map_output_ref_value(
        value, lambda text: _relative_task_output_ref(text, task_output_dir)
    )


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
    value, changed = _normalize_current_task_output_ref_value(
        values.get(key), workspace_root, task_output_dir
    )
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
        lambda text: _workspace_relative_task_output_ref_text(
            text, workspace_root, task_output_dir
        ),
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


def _workspace_relative_task_output_ref(
    text: str, workspace_root: Path, task_output_dir: Path
) -> Path | None:
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
        changed = (
            _rebase_output_ref_field(next_attrs, key, workspace_output_dir, task_output_dir)
            or changed
        )
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
    value, changed = _rebase_output_ref_value(
        values.get(key), workspace_output_dir, task_output_dir
    )
    if changed:
        values[key] = value
    return changed


def _rebase_output_ref_value(
    value: object, workspace_output_dir: Path, task_output_dir: Path
) -> tuple[object, bool]:
    return _map_output_ref_value(
        value,
        lambda text: _rebased_task_output_ref(text, workspace_output_dir, task_output_dir),
    )


def _rebased_task_output_ref(
    text: str, workspace_output_dir: Path, task_output_dir: Path
) -> str | None:
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
    root = getattr(getattr(agent, "tools", None), "workspace_root", None) or getattr(
        agent, "root", ""
    )
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
    if (
        isinstance(workspace, dict)
        and str(workspace.get("user_requested_output_dir") or "").strip()
    ):
        return True
    workspace = value.get("task_workspace")
    return isinstance(workspace, dict) and bool(
        str(workspace.get("user_requested_output_dir") or "").strip()
    )


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
_SCALAR_ATTRIBUTE_FIELDS: tuple[str, ...] = ()


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
    # create_subagents 是结构化“开始任务”事实：此时才把自然语言会话提升为任务，
    # 不要求用户输入触发词，也不在普通聊天入站时预先绑定。
    from ...conversation.task_promotion import promote_current_conversation_task

    promoted = promote_current_conversation_task(agent)
    if promoted is not None:
        attrs.setdefault("conversation_thread_id", promoted.thread_id)
        attrs.setdefault("conversation_task_id", promoted.task_id)
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


def _materialize_internal_thread(
    agent, current, task_id: str
) -> tuple[object | None, BaseException | None]:
    store = getattr(agent, "conversation_store", None)
    if store is None:
        return None, None
    goal = str(
        getattr(current, "root_user_prompt", "") or getattr(current, "prompt", "") or task_id
    ).strip()
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
            runtime_error_report(
                materialize_error, context="conversation.materialize_internal_thread"
            ),
        )


def _attach_thread_attrs(attrs: dict[str, object], thread: object, task_id: str) -> None:
    thread_id = getattr(thread, "thread_id", "") if thread is not None else ""
    if not isinstance(thread_id, str) or not thread_id.strip():
        return
    attrs.setdefault("conversation_thread_id", thread_id.strip())
    attrs.setdefault("conversation_task_id", task_id)


__all__ = ["add_current_conversation_attrs"]
