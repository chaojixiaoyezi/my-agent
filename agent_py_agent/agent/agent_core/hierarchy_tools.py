
from __future__ import annotations

import json
from dataclasses import dataclass

from ..action_protocol import subagent_schedule_envelope_from_payload
from ..common.value_parsing import TOOL_TEXT_LIST_OPTIONS, string_list
from ..runtime_errors import runtime_error_report
from ..subagents.services.hierarchy.qa_scheduler import quality_advice_payload
from ..subagents.services.hierarchy.scheduler import (
    HierarchyChildSpec,
    HierarchyScheduleRequest,
    HierarchyScheduleResult,
)
from ..tooling.cancellation import raise_if_cancelled
from ..tooling.models import ToolHandlerOutcome
from .orchestration.capacity import (
    checked_creation_capacity,
    subagent_quota_result,
)
from .orchestration.create_constraints import resolved_extra_write_roots
from .orchestration.create_context import create_context_manifest, create_context_packs
from .orchestration.create_policy import (
    create_task_attributes,
    normalize_create_output_params,
)
from .orchestration.dispatch.state_contract import dispatch_state_contract_payload
from .orchestration.lifecycle import (
    CreatedSubagentLifecycleRequest,
    publish_created_subagents,
)
from .orchestration.write_guard import ExternalWriteTargetRequest, external_write_target_error
from .parameters import _bool_param, _non_negative_int
from .runner.context import current_subagent_run_id


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


# LLM: Nested creation is the host service behind the sole recursive create_subagents
# surface. It persists children and requests automatic start; no second schedule tool exists.
# 函数用途: 在当前子代理名下创建并自动启动下一层，主代理和孙代理共用同一个入口。
def execute_child_creation(
    agent: object,
    params: dict[str, object],
    *,
    tool_name: str = "create_subagents",
) -> ToolHandlerOutcome:
    params = _schedule_tool_params(params)
    parent_run_id = current_subagent_run_id(agent)
    if not parent_run_id:
        return _schedule_error(
            "缺少当前 subagent runner 上下文；顶层派工请直接使用 create_subagents。",
            tool_name=tool_name,
        )
    child_specs = _hierarchy_child_specs(agent, params, tool_name=tool_name)
    if isinstance(child_specs, ToolHandlerOutcome):
        return child_specs
    bulk_error = _bulk_schedule_error(agent, child_specs)
    if bulk_error:
        return _schedule_error(bulk_error, tool_name=tool_name)
    capacity = checked_creation_capacity(agent)
    if isinstance(capacity, ToolHandlerOutcome):
        return capacity
    slots, limits = capacity
    if len(child_specs) > slots:
        return subagent_quota_result(len(child_specs), slots, limits)
    target_error = _hierarchy_target_error(agent, child_specs)
    if target_error:
        return _schedule_error(target_error, tool_name=tool_name)
    try:
        result = agent.subagents.hierarchy.schedule_child_runs(
            params=_schedule_request(
                ScheduleRequestBuildParams(agent, parent_run_id, child_specs, params)
            )
        )
    except (IndexError, TypeError, ValueError) as exc:
        return _schedule_error(
            _schedule_validation_error_message(exc),
            tool_name=tool_name,
        )
    lifecycle = _schedule_lifecycle(agent, result, params)
    auto_start = _schedule_auto_start_payload(lifecycle.auto_start)
    state_payload = dispatch_state_contract_payload(agent)
    current_state = state_payload.get("current_turn_run_state", {})
    return ToolHandlerOutcome(
        tool_name,
        True,
        _schedule_payload_json(
            result,
            {
                "pending_start_run_ids": _pending_start_run_ids(result, auto_start),
                "auto_start": auto_start,
                "schedule_load_errors": lifecycle.load_errors,
                "conversation_bind_errors": lifecycle.conversation_bind_errors,
                **state_payload,
                "schedule_lifecycle": _start_lifecycle_payload(
                    result,
                    auto_start,
                    current_state,
                ),
            },
            tool_name=tool_name,
        ),
    )


def _schedule_error(
    message: str,
    code: str = "TOOL_INVALID_ARGUMENTS",
    *,
    tool_name: str = "create_subagents",
) -> ToolHandlerOutcome:
    return ToolHandlerOutcome(tool_name, False, message, error_code=code)


def _schedule_validation_error_message(exc: Exception) -> str:
    return (
        f"create_subagents 参数无效: {exc.__class__.__name__}。"
        "请检查 agent_name 是否带可识别后缀、children 是否为对象列表、"
        "role/goal/allowed_tools/output_files 是否放在每个 child 对象里。"
    )


def _schedule_tool_params(params: dict[str, object]) -> dict[str, object]:
    return dict(params)


# LLM: Recursive scheduling receives the same request-local cancellation check as ordinary tools;
# the hierarchy domain calls it only between durable children and never owns a second stop token.
# 函数用途: 构造递归派工请求，并把当前 TUI/IM 回合的停止安全点传给逐项调度器。
def _schedule_request(request: ScheduleRequestBuildParams) -> HierarchyScheduleRequest:
    return HierarchyScheduleRequest(
        parent_run_id=request.parent_run_id,
        child_specs=request.child_specs,
        apply=_schedule_apply_default(request.raw_params),
        requested_by=request.parent_run_id,
        max_children=_non_negative_int(request.raw_params.get("max_children"), default=0),
        max_depth=_schedule_max_depth(request.raw_params),
        interrupt_check=raise_if_cancelled,
    )


def _schedule_apply_default(params: dict[str, object]) -> bool:
    if "apply" in params:
        raise ValueError("create_subagents 不接受 apply；创建后会由系统自动启动。")
    if "dry_run" in params:
        return not _bool_param(params.get("dry_run"), default=True)
    return True


def _schedule_payload_json(
    result: HierarchyScheduleResult,
    state_payload: dict[str, object] | None = None,
    *,
    tool_name: str = "create_subagents",
) -> str:
    payload = {
        "parent_run_id": result.parent_run_id,
        "root_id": result.root_id,
        "dry_run": result.dry_run,
        "blocked": result.blocked,
        "reason": result.reason,
        "created_run_ids": result.created_run_ids,
        "reused_run_ids": result.reused_run_ids,
        "planned_count": result.planned_count,
        "items": [_schedule_item_payload(item) for item in result.items],
    }
    payload.update(state_payload or {})
    if result.quality_advice is not None:
        payload["quality_advice"] = quality_advice_payload(result.quality_advice)
    payload["typed_envelope"] = subagent_schedule_envelope_from_payload(
        payload,
        tool=tool_name,
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


# LLM: Public nested-create payloads expose only work still waiting for host start;
# internal dispatch candidates remain inside the hierarchy scheduler implementation.
# 函数用途: 计算真正尚未启动的子任务，避免把内部调度候选误展示成模型操作指令。
def _pending_start_run_ids(
    result: HierarchyScheduleResult,
    auto_start: dict[str, object],
) -> list[str]:
    deferred = string_list(auto_start.get("deferred_run_ids"), TOOL_TEXT_LIST_OPTIONS)
    if deferred:
        return deferred
    if str(auto_start.get("status") or "") in {"started", "accepted", "not_needed"}:
        return []
    return list(result.dispatch_run_ids)


# LLM: Start receipt, observed RUNNING state, and terminal failure are separate
# facts. This projection never acts as a quality or delivery acceptance gate.
# 函数用途: 给孙代理创建结果补齐启动阶段事实，方便父代理看状态但不要求再次推动。
def _start_lifecycle_payload(
    result: HierarchyScheduleResult,
    auto_start: dict[str, object],
    current_state: object,
) -> dict[str, object]:
    state = current_state if isinstance(current_state, dict) else {}
    recorded = [*result.created_run_ids, *result.reused_run_ids]
    raw_status = str(auto_start.get("status") or "not_attempted")
    accepted = (
        string_list(auto_start.get("run_ids"), TOOL_TEXT_LIST_OPTIONS)
        if raw_status in {"started", "accepted"}
        else []
    )
    running = [
        run_id
        for run_id in string_list(state.get("running_run_ids"), TOOL_TEXT_LIST_OPTIONS)
        if run_id in recorded
    ]
    failed = list(
        dict.fromkeys(
            [
                *string_list(auto_start.get("failed_run_ids"), TOOL_TEXT_LIST_OPTIONS),
                *[
                    run_id
                    for run_id in string_list(state.get("blocked_run_ids"), TOOL_TEXT_LIST_OPTIONS)
                    if run_id in recorded
                ],
            ]
        )
    )
    if failed and accepted:
        start_status = "partially_started"
    elif failed:
        start_status = "failed"
    elif accepted:
        start_status = "accepted"
    elif raw_status == "deferred":
        start_status = "deferred"
    else:
        start_status = "not_started"
    return {
        "requested_count": len(recorded),
        "recorded_run_ids": recorded,
        "start_accepted_run_ids": accepted,
        "running_run_ids": running,
        "failed_run_ids": failed,
        "start_status": start_status,
        "counts": {
            "recorded": len(recorded),
            "start_accepted": len(accepted),
            "running": len(running),
            "failed": len(failed),
        },
        "authority": {
            "recorded": "subagent_store",
            "start_accepted": "background_start_receipt",
            "running": "task_state_machine",
        },
    }


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
                **runtime_error_report(exc, context="create_subagents.nested_load"),
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


def _hierarchy_child_specs(
    agent: object,
    params: dict[str, object],
    *,
    tool_name: str,
) -> list[HierarchyChildSpec] | ToolHandlerOutcome:
    if "child_specs" in params:
        return _schedule_error(
            "子代理创建只接受 children；请移除 child_specs。",
            tool_name=tool_name,
        )
    raw_children = params.get("children")
    if raw_children is None:
        return []
    children = _json_list_param(raw_children)
    if not children:
        return []
    specs: list[HierarchyChildSpec] = []
    for raw in children:
        spec = _hierarchy_child_spec(agent, raw, tool_name=tool_name)
        if isinstance(spec, ToolHandlerOutcome):
            return spec
        specs.append(spec)
    return specs


# LLM: Descendants must read the same canonical per-call limit as root creation;
# 0 keeps the repository-wide numeric convention of unlimited.
# 函数用途: 拦住单次递归创建过多下级，避免子代理绕过根代理的并发资源边界。
def _bulk_schedule_error(agent: object, child_specs: list[HierarchyChildSpec]) -> str:
    try:
        limit = max(
            0,
            int(
                getattr(
                    getattr(agent, "config", None),
                    "subagent_hierarchy_max_children_per_tool_call",
                    0,
                )
                or 0
            ),
        )
    except (TypeError, ValueError):
        limit = 0
    if limit <= 0:
        return ""
    if len(child_specs) <= limit:
        return ""
    return (
        f"create_subagents 单次最多创建 {limit} 个下一层任务；"
        "请拆成多次调用，并保持每个 goal 短小完整。"
    )


# LLM: Recursive child specs pass through the same output-ref normalization as root
# creation before permissions and attributes are derived; do not add a second path policy.
# 函数用途: 把一项递归派工参数转换成孙代理规格，并统一绑定当前任务目录。
def _hierarchy_child_spec(
    agent: object,
    raw: object,
    *,
    tool_name: str,
) -> HierarchyChildSpec | ToolHandlerOutcome:
    if not isinstance(raw, dict):
        return _schedule_error("children 每一项必须是对象。", tool_name=tool_name)
    raw = normalize_create_output_params(raw, agent)
    goal = str(raw.get("goal") or "").strip()
    if not goal:
        return _schedule_error("children 每一项必须包含 goal。", tool_name=tool_name)
    return HierarchyChildSpec(
        goal=goal,
        description=str(raw.get("description") or "").strip()[:240],
        agent_name=str(raw.get("agent_name") or raw.get("role") or "worker").strip(),
        role=str(raw.get("role") or "worker").strip(),
        thought=str(raw.get("thought") or "").strip(),
        plan=string_list(raw.get("plan"), TOOL_TEXT_LIST_OPTIONS),
        allowed_skills=string_list(raw.get("allowed_skills"), TOOL_TEXT_LIST_OPTIONS),
        allowed_tools=string_list(raw.get("allowed_tools"), TOOL_TEXT_LIST_OPTIONS),
        acceptance_checks=[],
        extra_write_roots=resolved_extra_write_roots(agent, raw, goal),
        context_manifest=create_context_manifest(raw),
        context_packs=create_context_packs(raw),
        attributes=create_task_attributes(raw, agent),
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
