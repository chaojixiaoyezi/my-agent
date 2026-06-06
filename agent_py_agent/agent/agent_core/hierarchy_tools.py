
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..action_protocol import subagent_schedule_envelope_from_payload
from ..common.value_parsing import TOOL_TEXT_LIST_OPTIONS, string_list
from ..runtime_errors import runtime_error_report
from ..subagents.services.hierarchy.qa_scheduler import quality_advice_payload
from ..subagents.services.hierarchy.scheduler import (
    HierarchyChildSpec,
    HierarchyScheduleRequest,
    HierarchyScheduleResult,
)
from ..tooling.models import BaseTool, ToolExecutionResult
from .orchestration.create_context import create_context_manifest, create_context_packs
from .orchestration.dispatch.state_contract import dispatch_state_contract_payload
from .orchestration.lifecycle import (
    CreatedSubagentLifecycleRequest,
    publish_created_subagents,
)
from .orchestration.tool_specs import build_schedule_child_subagents_spec
from .orchestration.write_guard import ExternalWriteTargetRequest, external_write_target_error
from .parameters import _bool_param, _non_negative_int
from .runner.context import current_subagent_run_id

if TYPE_CHECKING:
    from ..core import SimpleAgent

_MAX_CHILDREN_PER_TOOL_CALL = 0


@dataclass(frozen=True)
class ScheduleRequestBuildParams:
    agent: object
    parent_run_id: str
    child_specs: list[HierarchyChildSpec]
    raw_params: dict[str, object]


@dataclass(frozen=True)
class ScheduleLifecyclePayload:
    auto_start: dict[str, object] | None
    load_errors: list[dict[str, object]]
    conversation_bind_errors: list[dict[str, object]]


class ScheduleChildSubagentsTool(BaseTool):

    def __init__(self, agent: SimpleAgent):
        self.agent = agent
        self.spec = build_schedule_child_subagents_spec()

    def execute(self, params: dict[str, object]) -> ToolExecutionResult:
        params = _schedule_tool_params(params)
        parent_run_id = current_subagent_run_id(self.agent)
        if not parent_run_id:
            return _schedule_error("缺少当前 subagent runner 上下文；顶层派工请使用 create_subagents。")
        child_specs = _hierarchy_child_specs(params)
        if isinstance(child_specs, ToolExecutionResult):
            return child_specs
        bulk_error = _bulk_schedule_error(child_specs)
        if bulk_error:
            return _schedule_error(bulk_error)
        target_error = _hierarchy_target_error(self.agent, child_specs)
        if target_error:
            return _schedule_error(target_error)
        try:
            result = self.agent.subagents.hierarchy.schedule_child_runs(
                params=_schedule_request(
                    ScheduleRequestBuildParams(self.agent, parent_run_id, child_specs, params)
                )
            )
        except (IndexError, TypeError, ValueError) as exc:
            return _schedule_error(_schedule_validation_error_message(exc))
        lifecycle = _schedule_lifecycle(self.agent, result, params)
        return ToolExecutionResult(
            "schedule_child_subagents",
            True,
            _schedule_payload_json(
                result,
                {
                    "auto_start": _schedule_auto_start_payload(lifecycle.auto_start),
                    "schedule_load_errors": lifecycle.load_errors,
                    "conversation_bind_errors": lifecycle.conversation_bind_errors,
                    **dispatch_state_contract_payload(self.agent),
                },
            ),
        )


def _schedule_error(message: str) -> ToolExecutionResult:
    return ToolExecutionResult("schedule_child_subagents", False, message)


def _schedule_validation_error_message(exc: Exception) -> str:
    return (
        f"schedule_child_subagents 参数无效: {exc.__class__.__name__}。"
        "请检查 agent_name 是否带可识别后缀、children 是否为对象列表、"
        "role/goal/allowed_tools/output_files 是否放在每个 child 对象里。"
    )


def _schedule_tool_params(params: dict[str, object]) -> dict[str, object]:
    return dict(params)


def _schedule_request(request: ScheduleRequestBuildParams) -> HierarchyScheduleRequest:
    return HierarchyScheduleRequest(
        parent_run_id=request.parent_run_id,
        child_specs=request.child_specs,
        apply=_schedule_apply_default(request.raw_params),
        requested_by=request.parent_run_id,
        max_children=_non_negative_int(request.raw_params.get("max_children"), default=0),
        max_depth=_schedule_max_depth(request.raw_params),
    )


def _schedule_apply_default(params: dict[str, object]) -> bool:
    if "apply" in params:
        raise ValueError("schedule_child_subagents 只接受 dry_run；请移除 apply。")
    if "dry_run" in params:
        return not _bool_param(params.get("dry_run"), default=True)
    return True


def _schedule_payload_json(result: HierarchyScheduleResult, state_payload: dict[str, object] | None = None) -> str:
    payload = {
        "parent_run_id": result.parent_run_id,
        "root_id": result.root_id,
        "dry_run": result.dry_run,
        "blocked": result.blocked,
        "reason": result.reason,
        "created_run_ids": result.created_run_ids,
        "reused_run_ids": result.reused_run_ids,
        "dispatch_run_ids": result.dispatch_run_ids,
        "planned_count": result.planned_count,
        "items": [_schedule_item_payload(item) for item in result.items],
    }
    payload.update(state_payload or {})
    if result.quality_advice is not None:
        payload["quality_advice"] = quality_advice_payload(result.quality_advice)
    payload["typed_envelope"] = subagent_schedule_envelope_from_payload(
        payload,
        tool="schedule_child_subagents",
    ).to_dict()
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _schedule_auto_start_payload(auto_start: dict[str, object] | None) -> dict[str, object]:
    if not isinstance(auto_start, dict):
        return {"status": "not_attempted"}
    allowed = {
        "status",
        "dispatch_mode",
        "run_ids",
        "deferred_run_ids",
        "started_run_ids",
        "failed_run_ids",
        "warnings",
    }
    payload = {key: auto_start[key] for key in allowed if key in auto_start}
    return payload or {"status": str(auto_start.get("status") or "unknown")}


def _schedule_lifecycle(agent: object, result: HierarchyScheduleResult, params: dict[str, object]):
    if result.dry_run or result.blocked:
        tasks, load_errors = _load_schedule_dispatch_tasks(agent, [*result.created_run_ids, *result.reused_run_ids])
        lifecycle = publish_created_subagents(
            CreatedSubagentLifecycleRequest(agent, tasks, {**params, "defer_start": True})
        )
        return ScheduleLifecyclePayload(
            auto_start=lifecycle.auto_start,
            load_errors=load_errors,
            conversation_bind_errors=lifecycle.conversation_bind_errors,
        )
    tasks, load_errors = _load_schedule_dispatch_tasks(agent, result.dispatch_run_ids)
    lifecycle = publish_created_subagents(CreatedSubagentLifecycleRequest(agent, tasks, params))
    return ScheduleLifecyclePayload(
        auto_start=lifecycle.auto_start,
        load_errors=load_errors,
        conversation_bind_errors=lifecycle.conversation_bind_errors,
    )


def _load_schedule_dispatch_tasks(agent: object, run_ids: list[str]) -> tuple[list[object], list[dict[str, object]]]:
    manager = getattr(agent, "subagents", None)
    tasks: list[object] = []
    load_errors: list[dict[str, object]] = []
    for run_id in run_ids:
        try:
            task = manager.load(run_id)
        except Exception as exc:
            load_errors.append({
                "run_id": str(run_id or ""),
                **runtime_error_report(exc, context="schedule_child_subagents.load"),
            })
            continue
        if getattr(task, "id", "") == run_id:
            tasks.append(task)
    return tasks, load_errors


def _schedule_item_payload(item) -> dict[str, object]:
    return {
        "run_id": item.run_id,
        "parent_id": item.parent_id,
        "root_id": item.root_id,
        "depth": item.depth,
        "role": item.role,
        "agent_name": item.agent_name,
        "goal": item.goal,
        "created": item.created,
        "reason": item.reason,
    }


def _hierarchy_child_specs(params: dict[str, object]) -> list[HierarchyChildSpec] | ToolExecutionResult:
    if "child_specs" in params:
        return _schedule_error("schedule_child_subagents 只接受 children；请移除 child_specs。")
    raw_children = params.get("children")
    if raw_children is None:
        return []
    children = _json_list_param(raw_children)
    if not children:
        return []
    specs: list[HierarchyChildSpec] = []
    for raw in children:
        spec = _hierarchy_child_spec(raw)
        if isinstance(spec, ToolExecutionResult):
            return spec
        specs.append(spec)
    return specs


def _bulk_schedule_error(child_specs: list[HierarchyChildSpec]) -> str:
    if _MAX_CHILDREN_PER_TOOL_CALL <= 0:
        return ""
    if len(child_specs) <= _MAX_CHILDREN_PER_TOOL_CALL:
        return ""
    return (
        "schedule_child_subagents 单次最多创建 2 个 child；"
        "请拆成多次调用，每次 1-2 个 child，并保持每个 goal 短小完整。"
    )


def _hierarchy_child_spec(raw: object) -> HierarchyChildSpec | ToolExecutionResult:
    if not isinstance(raw, dict):
        return _schedule_error("children 每一项必须是对象。")
    goal = str(raw.get("goal") or "").strip()
    if not goal:
        return _schedule_error("children 每一项必须包含 goal。")
    return HierarchyChildSpec(
        goal=goal,
        agent_name=str(raw.get("agent_name") or raw.get("role") or "worker").strip(),
        role=str(raw.get("role") or "worker").strip(),
        thought=str(raw.get("thought") or "").strip(),
        plan=string_list(raw.get("plan"), TOOL_TEXT_LIST_OPTIONS),
        allowed_skills=string_list(raw.get("allowed_skills"), TOOL_TEXT_LIST_OPTIONS),
        allowed_tools=string_list(raw.get("allowed_tools"), TOOL_TEXT_LIST_OPTIONS),
        acceptance_checks=string_list(raw.get("acceptance_checks"), TOOL_TEXT_LIST_OPTIONS),
        extra_write_roots=string_list(raw.get("extra_write_roots"), TOOL_TEXT_LIST_OPTIONS),
        context_manifest=create_context_manifest(raw),
        context_packs=create_context_packs(raw),
    )


def _json_list_param(value: object) -> list[object]:
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


def _hierarchy_target_error(agent, specs: list[HierarchyChildSpec]) -> str:
    for spec in specs:
        target_error = external_write_target_error(
            ExternalWriteTargetRequest(
                agent=agent,
                allowed_tools=spec.allowed_tools,
                params={**spec.attributes, "extra_write_roots": list(spec.extra_write_roots)},
            )
        )
        if target_error:
            return target_error
    return ""


def _schedule_max_depth(params: dict[str, object]) -> int:
    return _non_negative_int(params.get("max_depth"), default=0)
