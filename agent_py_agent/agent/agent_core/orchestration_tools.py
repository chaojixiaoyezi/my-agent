
from __future__ import annotations

"""exposes model-callable orchestration tools backed by SimpleAgent subagent workflows.

这些不是普通文件工具，而是'主代理让模型触发子代理流程'的工具。
创建子代理、查看看板、执行 dispatch 都在这里，真实业务再转给 SimpleAgent 和 SubAgentManager。
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ..settings.defaults import default_config_int
from ..subagents.services.base import CreateRunParams
from ..subagents.services.hierarchy.scheduled_role import (
    agent_name_has_trailing_identifier,
    is_placeholder_agent_name,
)
from ..tooling.models import BaseTool, ToolExecutionResult
from .hierarchy_tools import ScheduleChildSubagentsTool as ScheduleChildSubagentsTool
from .orchestration.create_constraints import (
    CreateTaskResolution,
    explicit_root_missing_write_root_error,
    resolve_create_run,
)
from .orchestration.create_payload import (
    CreateSubagentItem,
    CreateSubagentsPayloadInput,
    create_items_from_params,
    create_subagents_payload,
)
from .orchestration.create_policy import (
    create_run_params,
)
from .orchestration.dispatch.tool import DispatchSubagentsTool
from .orchestration.lifecycle import (
    CreatedSubagentLifecycleRequest,
    publish_created_subagents,
)
from .orchestration.replacements import record_create_replacements
from .orchestration.shared_context import append_parent_shared_context
from .orchestration.sibling_roster import attach_sibling_roster
from .orchestration.tool_grants import (
    CODING_SUBAGENT_TOOLS,
    READ_ONLY_SUBAGENT_TOOLS,
    subagent_allowed_tools,
)
from .orchestration.tool_specs import build_create_subagents_spec
from .orchestration.tools.cancel import CancelSubagentsTool as CancelSubagentsTool
from .orchestration.tools.event import RaiseEventTool as RaiseEventTool
from .orchestration.tools.status import InspectAgentTreeTool as InspectAgentTreeTool
from .orchestration.work_scope import add_work_scope_key
from .orchestration.write_guard import (
    ExternalWriteTargetRequest,
    external_write_target_error,
)
from .parameters import _positive_int
from .runtime.guidance_tool import SendGuidanceTool as SendGuidanceTool
from .task_progress_tool import TaskProgressTool as TaskProgressTool

if TYPE_CHECKING:
    from ..core import SimpleAgent

_DEFAULT_MAX_SUBAGENTS = default_config_int("max_subagents")
_DEFAULT_DEPTH = 1


@dataclass(frozen=True)
class CreatedItemsResultRequest:
    agent: SimpleAgent
    resolutions: list[CreateTaskResolution]
    allowed_tool_values: list[list[str] | None]
    request_params: dict[str, object]
    capped_items: list[CreateSubagentItem]


@dataclass(frozen=True)
class ValidateSingleGoalRequest:
    agent: SimpleAgent
    params: dict[str, object]
    goal: str
    allowed_tools: list[str] | None


def _indexed_count_params(run_params: CreateRunParams, *, index: int, count: int) -> CreateRunParams:
    task_goal = f"{run_params.goal} / 子任务{index}" if count > 1 else run_params.goal
    task_name = _indexed_agent_name(
        run_params.agent_name,
        role=run_params.role,
        index=index,
        require_index=count > 1,
    )
    return CreateRunParams(**{**run_params.__dict__, "goal": task_goal, "agent_name": task_name})


def _indexed_item_params(run_params: CreateRunParams, *, index: int, total: int) -> CreateRunParams:
    task_name = _indexed_agent_name(
        run_params.agent_name,
        role=run_params.role,
        index=index,
        require_index=total > 1,
    )
    return CreateRunParams(**{**run_params.__dict__, "agent_name": task_name})


def _indexed_agent_name(agent_name: str, *, role: str, index: int, require_index: bool = False) -> str:
    name = str(agent_name or "").strip()
    if _needs_system_lineage_name(name):
        return f"agent-d{_DEFAULT_DEPTH}-{_role_suffix(role)}-{index}"
    if require_index and not agent_name_has_trailing_identifier(name):
        return f"{name}-{index}"
    return name


def _needs_system_lineage_name(agent_name: str) -> bool:
    return is_placeholder_agent_name(agent_name)


def _role_suffix(role: str) -> str:
    suffix = str(role or "worker").strip().replace("_", "-").strip("-") or "worker"
    if suffix in {"general", "child", "subagent", "agent"}:
        return "worker"
    return suffix


class CreateSubagentsTool(BaseTool):

    def __init__(self, agent: SimpleAgent):
        self.agent = agent
        self.spec = build_create_subagents_spec()

    def execute(self, params: dict[str, object]) -> ToolExecutionResult:
        return _execute_create_subagents(self.agent, params)

    def _cap_items(self, items: list[CreateSubagentItem]) -> list[CreateSubagentItem]:
        return _cap_items_for_agent(self.agent, items)


def _execute_create_subagents(agent: SimpleAgent, params: dict[str, object]) -> ToolExecutionResult:
    if not agent.config.enable_subagents:
        return ToolExecutionResult("create_subagents", False, "配置已禁用 subagent。")
    items_result = _items_result(agent, params)
    if items_result is not None:
        return items_result
    prepared = _prepare_count_mode(agent, params)
    if isinstance(prepared, ToolExecutionResult):
        return prepared
    count, allowed_tools, run_params = prepared
    task_params = _count_run_params(agent, count, run_params)
    return _created_tasks_result(agent, _resolve_task_params(agent, task_params), allowed_tools, params)


def _items_result(agent: SimpleAgent, params: dict[str, object]) -> ToolExecutionResult | None:
    items = create_items_from_params(params)
    if isinstance(items, str):
        return ToolExecutionResult("create_subagents", False, items)
    if items:
        return _execute_items(agent, items, params)
    return None


def _prepare_count_mode(
    agent: SimpleAgent,
    params: dict[str, object],
) -> tuple[int, list[str] | None, CreateRunParams] | ToolExecutionResult:
    params = append_parent_shared_context(agent, params)
    goal = str(params.get("goal") or "").strip()
    if not goal:
        return ToolExecutionResult("create_subagents", False, "缺少必填参数 goal。")
    count = _requested_count(agent, params)
    if isinstance(count, ToolExecutionResult):
        return count
    allowed_tools = subagent_allowed_tools(params)
    validation = _validate_single_goal(ValidateSingleGoalRequest(agent, params, goal, allowed_tools))
    if validation:
        return ToolExecutionResult("create_subagents", False, validation)
    return count, allowed_tools, create_run_params(agent, params, goal, allowed_tools)


def _created_tasks_result(
    agent: SimpleAgent,
    resolutions: list[CreateTaskResolution],
    allowed_tools: list[str] | str | None,
    request_params: dict[str, object],
) -> ToolExecutionResult:
    tasks = [item.task for item in resolutions]
    attach_sibling_roster(agent.subagents, tasks)
    replacement_records = record_create_replacements(agent, tasks)
    lifecycle = publish_created_subagents(CreatedSubagentLifecycleRequest(agent, tasks, request_params))
    payload = create_subagents_payload(
        CreateSubagentsPayloadInput(
            agent=agent,
            resolutions=resolutions,
            allowed_tools=allowed_tools,
            request_params=request_params,
            auto_start=lifecycle.auto_start,
            replacement_records=replacement_records,
            conversation_bind_errors=lifecycle.conversation_bind_errors,
        )
    )
    return ToolExecutionResult("create_subagents", True, json.dumps(payload, ensure_ascii=False, indent=2))


def _execute_items(
    agent: SimpleAgent,
    items: list[CreateSubagentItem],
    request_params: dict[str, object],
) -> ToolExecutionResult:
    capped = _items_with_parent_context(agent, _cap_items_for_agent(agent, items))
    allowed_tool_values = [subagent_allowed_tools(item.params) for item in capped]
    validation = _validate_items(agent, capped, allowed_tool_values)
    if validation:
        return ToolExecutionResult("create_subagents", False, validation)
    resolutions = _resolve_task_params(agent, _indexed_item_run_params(agent, capped))
    return _created_items_result(CreatedItemsResultRequest(
        agent=agent,
        resolutions=resolutions,
        allowed_tool_values=allowed_tool_values,
        request_params=request_params,
        capped_items=capped,
    ))


def _created_items_result(request: CreatedItemsResultRequest) -> ToolExecutionResult:
    tasks = [item.task for item in request.resolutions]
    attach_sibling_roster(request.agent.subagents, tasks, save=False)
    for task in tasks:
        request.agent.subagents.save(task)
    replacement_records = record_create_replacements(request.agent, tasks)
    payload_request = _items_payload_request(request.request_params, request.capped_items)
    lifecycle = publish_created_subagents(CreatedSubagentLifecycleRequest(request.agent, tasks, payload_request))
    payload = create_subagents_payload(
        CreateSubagentsPayloadInput(
            agent=request.agent,
            resolutions=request.resolutions,
            allowed_tools=_payload_allowed_tools(request.allowed_tool_values),
            request_params=payload_request,
            auto_start=lifecycle.auto_start,
            replacement_records=replacement_records,
            conversation_bind_errors=lifecycle.conversation_bind_errors,
        )
    )
    payload["batch_mode"] = "items"
    return ToolExecutionResult("create_subagents", True, json.dumps(payload, ensure_ascii=False, indent=2))


def _items_with_parent_context(agent: SimpleAgent, items: list[CreateSubagentItem]) -> list[CreateSubagentItem]:
    return [
        CreateSubagentItem(goal=item.goal, params=append_parent_shared_context(agent, item.params))
        for item in items
    ]


def _validate_items(
    agent: SimpleAgent,
    items: list[CreateSubagentItem],
    allowed_tool_values: list[list[str] | None],
) -> str:
    for item, allowed_tools in zip(items, allowed_tool_values, strict=True):
        validation = _validate_single_goal(ValidateSingleGoalRequest(agent, item.params, item.goal, allowed_tools))
        if validation:
            return validation
    return ""


def _indexed_item_run_params(agent: SimpleAgent, items: list[CreateSubagentItem]) -> list[CreateRunParams]:
    run_params_by_item: list[CreateRunParams] = []
    for index, item in enumerate(items, start=1):
        run_params = create_run_params(agent, item.params, item.goal, subagent_allowed_tools(item.params))
        indexed = _indexed_item_params(run_params, index=index, total=len(items))
        run_params_by_item.append(_with_default_child_output_ref(agent, indexed, index=index))
    return run_params_by_item


def _items_payload_request(request_params: dict[str, object], items: list[CreateSubagentItem]) -> dict[str, object]:
    payload_request = dict(request_params)
    payload_request["items"] = [item.params for item in items]
    return payload_request


def _cap_items_for_agent(agent: SimpleAgent, items: list[CreateSubagentItem]) -> list[CreateSubagentItem]:
    max_subagents = _configured_max_subagents(agent)
    return items[:max_subagents] if max_subagents > 0 else items


def _validate_single_goal(request: ValidateSingleGoalRequest) -> str:
    missing_write_root = explicit_root_missing_write_root_error(request.agent, request.params, request.goal)
    if missing_write_root:
        return missing_write_root
    target_error = external_write_target_error(
        ExternalWriteTargetRequest(
            agent=request.agent,
            allowed_tools=request.allowed_tools or CODING_SUBAGENT_TOOLS,
            params=request.params,
        )
    )
    return target_error or ""


def _requested_count(agent: SimpleAgent, params: dict[str, object]) -> int | ToolExecutionResult:
    count = _positive_int(params.get("count"), default=0) if _has_count_param(params) else _default_requested_count(params)
    if count <= 0:
        return ToolExecutionResult("create_subagents", False, "count 必须大于 0。")
    max_subagents = _configured_max_subagents(agent)
    return min(count, max_subagents) if max_subagents > 0 else count


def _default_requested_count(params: dict[str, object]) -> int:
    replacement_count = len(_string_list(params.get("replacement_for_run_ids")))
    return replacement_count or 1


def _count_run_params(agent: SimpleAgent, count: int, run_params: CreateRunParams) -> list[CreateRunParams]:
    return [
        _with_count_child_output_ref(
            agent,
            _indexed_count_params(run_params, index=index, count=count),
            index=index,
            count=count,
        )
        for index in range(1, count + 1)
    ]


def _resolve_task_params(agent: SimpleAgent, task_params: list[CreateRunParams]) -> list[CreateTaskResolution]:
    return [resolve_create_run(agent.subagents, item) for item in task_params]


def _payload_allowed_tools(values: list[list[str] | None]) -> list[str] | str | None:
    if not values:
        return None
    first = values[0]
    if all(value == first for value in values):
        return first
    return "per_item"


def _configured_max_subagents(agent) -> int:
    raw_value = getattr(getattr(agent, "config", None), "max_subagents", _DEFAULT_MAX_SUBAGENTS)
    try:
        return max(0, int(raw_value))
    except (TypeError, ValueError):
        return _DEFAULT_MAX_SUBAGENTS


def _has_count_param(params: dict[str, object]) -> bool:
    if "count" not in params:
        return False
    value = params.get("count")
    return str(value or "").strip() != ""


def _string_list(value: object) -> list[str]:
    if isinstance(value, str):
        raw_items = value.split(",")
    elif isinstance(value, list | tuple | set):
        raw_items = value
    else:
        return []
    return [str(item).strip() for item in raw_items if str(item).strip()]


def _with_default_child_output_ref(agent: object, run_params: CreateRunParams, *, index: int) -> CreateRunParams:
    attrs = dict(run_params.attributes or {})
    if _has_structured_output_ref(attrs):
        return run_params
    task_root = _current_task_root(agent)
    if not task_root:
        return run_params
    default_ref = str(Path(task_root) / "work" / "child_outputs" / f"{index:02d}-{_output_slug(run_params)}.md")
    attrs["output_files"] = [default_ref]
    attrs["system_default_output_ref"] = True
    add_work_scope_key(attrs)
    return CreateRunParams(**{**run_params.__dict__, "attributes": attrs})


def _with_count_child_output_ref(
    agent: object,
    run_params: CreateRunParams,
    *,
    index: int,
    count: int,
) -> CreateRunParams:
    if count <= 1:
        return _with_default_child_output_ref(agent, run_params, index=index)
    attrs = dict(run_params.attributes or {})
    shared_outputs = _structured_shared_output_refs(attrs)
    if not shared_outputs:
        return _with_default_child_output_ref(agent, run_params, index=index)
    for key, values in shared_outputs.items():
        attrs[f"shared_requested_{key}"] = values
        attrs.pop(key, None)
    attrs["shared_output_split_policy"] = "count_mode_task_local_child_outputs"
    add_work_scope_key(attrs)
    split_params = CreateRunParams(**{**run_params.__dict__, "attributes": attrs})
    return _with_default_child_output_ref(agent, split_params, index=index)


def _has_structured_output_ref(attrs: dict[str, object]) -> bool:
    for key in ("output_files", "output_refs", "artifact_refs"):
        value = attrs.get(key)
        if isinstance(value, list) and any(str(item or "").strip() for item in value):
            return True
    return False


def _structured_shared_output_refs(attrs: dict[str, object]) -> dict[str, list[str]]:
    shared: dict[str, list[str]] = {}
    for key in ("output_files", "output_refs"):
        values = _string_list(attrs.get(key))
        if values:
            shared[key] = values
    return shared


def _current_task_root(agent: object) -> str:
    raw = getattr(agent, "_current_run_task_workspace", "")
    if not isinstance(raw, str | Path):
        return ""
    return str(raw).strip()


def _output_slug(run_params: CreateRunParams) -> str:
    base = str(run_params.agent_name or run_params.role or "child").strip()
    chars: list[str] = []
    last_dash = False
    for char in base:
        replacement, last_dash = _slug_char(char, last_dash)
        if replacement:
            chars.append(replacement)
    slug = "".join(chars).strip("-_").lower()
    return (slug or "child")[:80]


def _slug_char(char: str, last_dash: bool) -> tuple[str, bool]:
    if char.isalnum() or char in {"_", "-"}:
        return char, False
    return ("", True) if last_dash else ("-", True)
