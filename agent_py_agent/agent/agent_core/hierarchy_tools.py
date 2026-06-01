# LLM: Model-callable hierarchy tools let a running subagent create only its own next layer.
# 模块用途: 承载 schedule_child_subagents 工具，把 runner 内部层级派工和通用编排工具解耦。

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..action_protocol import subagent_schedule_envelope_from_payload
from ..model_visible_ref_sanitizer import sanitize_model_visible_refs
from ..subagents.services.hierarchy_scheduler import (
    HierarchyChildSpec,
    HierarchyScheduleRequest,
    HierarchyScheduleResult,
)
from ..tools import BaseTool, ToolExecutionResult
from . import orchestration_background_dispatch
from .orchestration_create_context import create_context_manifest, create_context_packs
from .orchestration_dispatch_state_contract import dispatch_state_contract_payload
from .orchestration_quality_advice_payload import quality_advice_payload
from .orchestration_run_scope import remember_orchestration_run_ids
from .orchestration_tool_specs import build_schedule_child_subagents_spec
from .orchestration_write_guard import ExternalWriteTargetRequest, external_write_target_error
from .parameters import _bool_param, _non_negative_int, _string_list
from .runner_context import current_subagent_run_id

if TYPE_CHECKING:
    from ..core import SimpleAgent

_MAX_CHILDREN_PER_TOOL_CALL = 0


# LLM: ScheduleRequestBuildParams bundles internal request-build inputs to keep signatures stable.
# 类用途: 汇总当前 agent、parent run、child specs 和原始工具参数，避免后续字段扩展推高参数数量。
@dataclass(frozen=True)
class ScheduleRequestBuildParams:
    agent: object
    parent_run_id: str
    child_specs: list[HierarchyChildSpec]
    raw_params: dict[str, object]


# LLM: ScheduleChildSubagentsTool exposes child-run creation only from the active runner context.
# 类用途: 给主/子/孙节点 runner 暴露受控层级调度入口，parent_id 从当前 runner 上下文读取。
class ScheduleChildSubagentsTool(BaseTool):

    # LLM: __init__ stores the agent facade and stable tool specification.
    # 函数用途: 初始化当前节点层级调度工具，不执行任何写入。
    def __init__(self, agent: SimpleAgent):
        self.agent = agent
        self.spec = build_schedule_child_subagents_spec()

    # LLM: execute validates model JSON, builds a hierarchy request, and delegates scheduling.
    # 函数用途: 当前 runner 按 bundle 创建下一层 child runs；没有当前 run 上下文时直接阻断。
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
            result = self.agent.subagents.schedule_child_runs(
                params=_schedule_request(
                    ScheduleRequestBuildParams(self.agent, parent_run_id, child_specs, params)
                )
            )
        except (IndexError, TypeError, ValueError) as exc:
            return _schedule_error(_schedule_validation_error_message(exc))
        auto_start = _schedule_auto_start(self.agent, result, params)
        # LLM: remember child ids from schedule so the nested parent sees a machine state table immediately.
        # 函数用途: schedule_child_subagents 返回后直接给模型 dispatchable/running/blocked 状态，不靠 prose 抄 id。
        remember_orchestration_run_ids(self.agent, [*result.created_run_ids, *result.reused_run_ids])
        return ToolExecutionResult(
            "schedule_child_subagents",
            True,
            _schedule_payload_json(
                result,
                {
                    "auto_start": auto_start,
                    **dispatch_state_contract_payload(self.agent),
                },
            ),
        )


# LLM: _schedule_error keeps all schedule_child_subagents failures consistently named.
# 函数用途: 生成层级调度工具的失败结果，便于模型和测试稳定识别工具名。
def _schedule_error(message: str) -> ToolExecutionResult:
    return ToolExecutionResult("schedule_child_subagents", False, message)


# LLM: _schedule_validation_error_message keeps model-facing schedule failures actionable.
# 函数用途: 把底层参数异常转成结构化工具错误，避免真实 runner 看到裸 IndexError 后反复试错。
def _schedule_validation_error_message(exc: Exception) -> str:
    return (
        f"schedule_child_subagents 参数无效: {exc.__class__.__name__}。"
        "请检查 agent_name 是否带可识别后缀、children 是否为对象列表、"
        "role/goal/allowed_tools/output_files 是否放在每个 child 对象里。"
    )


# LLM: _schedule_tool_params keeps schedule_child_subagents parameters flat.
# 函数用途: 不再展开 orchestration 包装；模型可见协议只接受顶层 children/dry_run 等字段。
def _schedule_tool_params(params: dict[str, object]) -> dict[str, object]:
    return dict(params)


# LLM: _schedule_request converts validated tool params into the manager service bundle.
# 函数用途: 构造 HierarchyScheduleRequest，集中处理 dry_run、children、depth 和审计请求者。
def _schedule_request(request: ScheduleRequestBuildParams) -> HierarchyScheduleRequest:
    return HierarchyScheduleRequest(
        parent_run_id=request.parent_run_id,
        child_specs=request.child_specs,
        apply=_schedule_apply_default(request.raw_params),
        requested_by=request.parent_run_id,
        max_children=_non_negative_int(request.raw_params.get("max_children"), default=0),
        max_depth=_schedule_max_depth(request.agent, request.parent_run_id, request.raw_params),
    )


# LLM: _schedule_apply_default lets active runners materialize their own direct children by default.
# 函数用途: runner 内 schedule_child_subagents 省略 dry_run 时默认创建；显式 dry_run=true 预览。
def _schedule_apply_default(params: dict[str, object]) -> bool:
    if "apply" in params:
        raise ValueError("schedule_child_subagents 只接受 dry_run；请移除 apply。")
    if "dry_run" in params:
        return not _bool_param(params.get("dry_run"), default=True)
    return True


# LLM: _schedule_payload_json renders service results without leaking large workspace content.
# 函数用途: 输出层级调度结果摘要，包含新建/复用/建议 dispatch 的 run id、层级关系和阻断原因。
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
    return json.dumps(sanitize_model_visible_refs(payload), ensure_ascii=False, indent=2)


# LLM: _schedule_auto_start mirrors create_subagents startup while preserving current runner scope.
# 函数用途: schedule_child_subagents 创建下一层后后台启动可运行 child，并把状态放进同一个 payload。
def _schedule_auto_start(agent: object, result: HierarchyScheduleResult, params: dict[str, object]) -> dict[str, object]:
    if result.dry_run or result.blocked:
        return {"status": "not_needed", "run_ids": []}
    tasks = _load_schedule_dispatch_tasks(agent, result.dispatch_run_ids)
    return orchestration_background_dispatch.auto_start_tasks(agent, tasks, params)


# LLM: _load_schedule_dispatch_tasks loads only scheduler-selected child ids for auto-start.
# 函数用途: 按 dispatch_run_ids 读取刚创建或可复用的直接孩子；读取失败时跳过单项而不中断调度响应。
def _load_schedule_dispatch_tasks(agent: object, run_ids: list[str]) -> list[object]:
    manager = getattr(agent, "subagents", None)
    tasks: list[object] = []
    for run_id in run_ids:
        try:
            task = manager.load(run_id)
        except Exception:
            continue
        if getattr(task, "id", "") == run_id:
            tasks.append(task)
    return tasks


# LLM: _schedule_item_payload keeps the tool response refs-only and safe for model context.
# 函数用途: 把单个 child 创建/计划结果转换为轻量 JSON 字段。
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


# LLM: _hierarchy_child_specs parses the model-provided children list into strict schedule bundles.
# 函数用途: 解析 schedule_child_subagents.children，支持 JSON 字符串或对象列表并拒绝空 goal。
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


# LLM: _bulk_schedule_error turns real-model long child batches into explicit retry guidance.
# 函数用途: 限制 runner 单次层级调度最多 2 个 child，避免长 JSON 工具参数被截断后卡死。
def _bulk_schedule_error(child_specs: list[HierarchyChildSpec]) -> str:
    if _MAX_CHILDREN_PER_TOOL_CALL <= 0:
        return ""
    if len(child_specs) <= _MAX_CHILDREN_PER_TOOL_CALL:
        return ""
    return (
        "schedule_child_subagents 单次最多创建 2 个 child；"
        "请拆成多次调用，每次 1-2 个 child，并保持每个 goal 短小完整。"
    )


# LLM: _hierarchy_child_spec validates one child bundle from the model tool call.
# 函数用途: 把单个 child 对象转换为 HierarchyChildSpec，并保留工具、验收和写入边界字段。
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
        plan=_string_list(raw.get("plan")),
        allowed_skills=_string_list(raw.get("allowed_skills")),
        allowed_tools=_string_list(raw.get("allowed_tools")),
        acceptance_checks=_string_list(raw.get("acceptance_checks")),
        extra_write_roots=_string_list(raw.get("extra_write_roots")),
        context_manifest=create_context_manifest(raw),
        context_packs=create_context_packs(raw),
    )


# LLM: _json_list_param accepts common model encodings while keeping tool params explicit.
# 函数用途: 将列表、单对象或 JSON 字符串规范成对象列表；解析失败时返回空列表。
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


# LLM: _hierarchy_target_error reuses existing write-target guard before child task creation.
# 函数用途: 对每个 child goal/allowed_tools 做越界写入预检，失败时整批阻断。
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


# LLM: _schedule_max_depth accepts model-friendly relative depth when absolute depth would block all children.
# 函数用途: 兼容模型把 max_depth=1 理解成“再开一层”的写法，同时保留顶层绝对 depth 语义。
def _schedule_max_depth(agent, parent_run_id: str, params: dict[str, object]) -> int:
    parsed = _non_negative_int(params.get("max_depth"), default=0)
    if "max_depth" not in params:
        return parsed
    try:
        parent_depth = int(agent.subagents.load(parent_run_id).depth)
    except Exception:
        return parsed
    if parsed <= parent_depth:
        return parent_depth + max(1, parsed)
    return parsed
