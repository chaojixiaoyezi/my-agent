
from __future__ import annotations

"""exposes model-callable orchestration tools backed by SimpleAgent subagent workflows.

这些不是普通文件工具，而是'主代理让模型触发子代理流程'的工具。
创建子代理、查看看板、执行 dispatch 都在这里，真实业务再转给 SimpleAgent 和 SubAgentManager。
"""

import json
from typing import TYPE_CHECKING

from ..settings.defaults import default_config_int
from ..subagents.services.base import CreateRunParams
from ..tools import BaseTool, ToolExecutionResult
from .hierarchy_tools import ScheduleChildSubagentsTool as ScheduleChildSubagentsTool
from .orchestration.create_constraints import (
    delegation_constraint_conflict_error,
    explicit_root_missing_write_root_error,
)
from .orchestration.create_idempotency import (
    CreateTaskResolution,
    resolve_create_run,
)
from .orchestration.create_items import (
    CreateSubagentItem,
    create_items_from_params,
)
from .orchestration.create_payload import CreateSubagentsPayloadInput, create_subagents_payload
from .orchestration.create_policy import (
    create_run_params,
)
from .orchestration.dispatch.tool import DispatchSubagentsTool
from .orchestration.lifecycle import (
    CreatedSubagentLifecycleRequest,
    publish_created_subagents,
)
from .orchestration.lineage_names import indexed_count_params, indexed_item_params
from .orchestration.replacements import record_create_replacements
from .orchestration.shared_context import append_parent_shared_context
from .orchestration.sibling_roster import attach_sibling_roster
from .orchestration.tool_grants import (
    CODING_SUBAGENT_TOOLS,
    READ_ONLY_SUBAGENT_TOOLS,
    subagent_allowed_tools,
)
from .orchestration.tool_specs import build_create_subagents_spec
from .orchestration.tools.event import RaiseEventTool as RaiseEventTool
from .orchestration.tools.status import InspectAgentTreeTool as InspectAgentTreeTool
from .orchestration.write_guard import ExternalWriteTargetRequest, external_write_target_error
from .parameters import _positive_int
from .runtime.guidance_tool import SendGuidanceTool as SendGuidanceTool
from .task_progress_tool import TaskProgressTool as TaskProgressTool

if TYPE_CHECKING:
    from ..core import SimpleAgent

_DEFAULT_MAX_SUBAGENTS = default_config_int("max_subagents")


class CreateSubagentsTool(BaseTool):

    def __init__(self, agent: SimpleAgent):
        self.agent = agent
        self.spec = build_create_subagents_spec()

    def execute(self, params: dict[str, object]) -> ToolExecutionResult:
        if not self.agent.config.enable_subagents:
            return ToolExecutionResult("create_subagents", False, "配置已禁用 subagent。")

        items_result = self._items_result(params)
        if items_result is not None:
            return items_result

        prepared = self._prepare_count_mode(params)
        if isinstance(prepared, ToolExecutionResult):
            return prepared
        goal, count, allowed_tools, run_params = prepared
        task_params = self._count_run_params(count, run_params)
        resolutions = self._resolve_task_params(task_params)
        tasks = [item.task for item in resolutions]
        attach_sibling_roster(self.agent.subagents, tasks)
        replacement_records = record_create_replacements(self.agent, tasks)
        lifecycle = publish_created_subagents(
            CreatedSubagentLifecycleRequest(self.agent, tasks, params)
        )
        payload = create_subagents_payload(
            CreateSubagentsPayloadInput(
                agent=self.agent,
                resolutions=resolutions,
                allowed_tools=allowed_tools,
                request_params=params,
                auto_start=lifecycle.auto_start,
                replacement_records=replacement_records,
                conversation_bind_errors=lifecycle.conversation_bind_errors,
            )
        )
        return ToolExecutionResult(
            "create_subagents",
            True,
            json.dumps(payload, ensure_ascii=False, indent=2),
        )

    def _items_result(self, params: dict[str, object]) -> ToolExecutionResult | None:
        items = create_items_from_params(params)
        if isinstance(items, str):
            return ToolExecutionResult("create_subagents", False, items)
        if items:
            return self._execute_items(items, params)
        return None

    def _prepare_count_mode(
        self, params: dict[str, object]
    ) -> tuple[str, int, list[str] | None, CreateRunParams] | ToolExecutionResult:
        params = append_parent_shared_context(self.agent, params)
        goal = str(params.get("goal") or "").strip()
        if not goal:
            return ToolExecutionResult("create_subagents", False, "缺少必填参数 goal。")
        count = self._requested_count(params)
        if isinstance(count, ToolExecutionResult):
            return count
        allowed_tools = subagent_allowed_tools(params)
        validation = self._validate_single_goal(params, goal, allowed_tools)
        if validation:
            return ToolExecutionResult("create_subagents", False, validation)
        run_params = create_run_params(self.agent, params, goal, allowed_tools)
        return goal, count, allowed_tools, run_params

    def _execute_items(
        self,
        items: list[CreateSubagentItem],
        request_params: dict[str, object],
    ) -> ToolExecutionResult:
        capped = self._items_with_parent_context(self._cap_items(items))
        allowed_tool_values = [subagent_allowed_tools(item.params) for item in capped]
        validation = self._validate_items(capped, allowed_tool_values)
        if validation:
            return ToolExecutionResult("create_subagents", False, validation)
        run_params_by_item = self._indexed_item_run_params(capped)
        resolutions = self._resolve_task_params(run_params_by_item)
        tasks = [item.task for item in resolutions]
        attach_sibling_roster(self.agent.subagents, tasks, save=False)
        for task in tasks:
            self.agent.subagents.save(task)
        replacement_records = record_create_replacements(self.agent, tasks)
        payload_request = self._items_payload_request(request_params, capped)
        lifecycle = publish_created_subagents(
            CreatedSubagentLifecycleRequest(self.agent, tasks, payload_request)
        )
        payload = create_subagents_payload(
            CreateSubagentsPayloadInput(
                agent=self.agent,
                resolutions=resolutions,
                allowed_tools=_payload_allowed_tools(allowed_tool_values),
                request_params=payload_request,
                auto_start=lifecycle.auto_start,
                replacement_records=replacement_records,
                conversation_bind_errors=lifecycle.conversation_bind_errors,
            )
        )
        payload["batch_mode"] = "items"
        return ToolExecutionResult(
            "create_subagents",
            True,
            json.dumps(payload, ensure_ascii=False, indent=2),
        )

    def _items_with_parent_context(self, items: list[CreateSubagentItem]) -> list[CreateSubagentItem]:
        return [
            CreateSubagentItem(
                goal=item.goal,
                params=append_parent_shared_context(self.agent, item.params),
            )
            for item in items
        ]

    def _validate_items(self, items: list[CreateSubagentItem], allowed_tool_values: list[list[str] | None]) -> str:
        for item, allowed_tools in zip(items, allowed_tool_values, strict=True):
            validation = self._validate_single_goal(item.params, item.goal, allowed_tools)
            if validation:
                return validation
        return ""

    def _indexed_item_run_params(self, items: list[CreateSubagentItem]) -> list[CreateRunParams]:
        run_params_by_item: list[CreateRunParams] = []
        for index, item in enumerate(items, start=1):
            run_params = create_run_params(
                self.agent,
                item.params,
                item.goal,
                subagent_allowed_tools(item.params),
            )
            run_params_by_item.append(indexed_item_params(run_params, index=index, total=len(items)))
        return run_params_by_item

    def _items_payload_request(self, request_params: dict[str, object], items: list[CreateSubagentItem]) -> dict[str, object]:
        payload_request = dict(request_params)
        payload_request["items"] = [item.params for item in items]
        return payload_request

    def _cap_items(self, items: list[CreateSubagentItem]) -> list[CreateSubagentItem]:
        max_subagents = _configured_max_subagents(self.agent)
        if max_subagents > 0:
            return items[:max_subagents]
        return items

    def _validate_single_goal(
        self,
        params: dict[str, object],
        goal: str,
        allowed_tools: list[str] | None,
    ) -> str:
        missing_write_root = explicit_root_missing_write_root_error(self.agent, params, goal)
        if missing_write_root:
            return missing_write_root
        target_error = external_write_target_error(
            ExternalWriteTargetRequest(
                agent=self.agent,
                allowed_tools=allowed_tools or CODING_SUBAGENT_TOOLS,
                params=params,
            )
        )
        if target_error:
            return target_error
        return delegation_constraint_conflict_error(params)

    def _requested_count(self, params: dict[str, object]) -> int | ToolExecutionResult:
        count = _positive_int(params.get("count"), default=1)
        if count <= 0:
            return ToolExecutionResult("create_subagents", False, "count 必须大于 0。")
        max_subagents = _configured_max_subagents(self.agent)
        if max_subagents > 0:
            count = min(count, max_subagents)
        return count

    def _count_run_params(self, count: int, run_params: CreateRunParams) -> list[CreateRunParams]:
        return [
            indexed_count_params(run_params, index=index, count=count)
            for index in range(1, count + 1)
        ]

    def _resolve_task_params(self, task_params: list[CreateRunParams]) -> list[CreateTaskResolution]:
        resolutions: list[CreateTaskResolution] = []
        for item in task_params:
            resolutions.append(resolve_create_run(self.agent.subagents, item))
        return resolutions


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
