# LLM: Model-callable hierarchy tools let a running subagent create only its own next layer.
# 模块用途: 承载 schedule_child_subagents 工具，把 runner 内部层级派工和通用编排工具解耦。

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..action_protocol import subagent_schedule_envelope_from_payload
from ..subagents.services.hierarchy_scheduler import (
    HierarchyChildSpec,
    HierarchyScheduleRequest,
    HierarchyScheduleResult,
)
from ..tools import BaseTool, ToolExecutionResult
from .orchestration_tool_specs import build_schedule_child_subagents_spec
from .orchestration_write_guard import external_write_target_error
from .parameters import _bool_param, _non_negative_int, _string_list
from .runner_context import current_subagent_run_id

if TYPE_CHECKING:
    from ..core import SimpleAgent

_MAX_CHILDREN_PER_TOOL_CALL = 2


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
        return ToolExecutionResult("schedule_child_subagents", True, _schedule_payload_json(result))


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
        "role/goal/allowed_tools/extra_write_roots 是否放在每个 child 对象里。"
    )


# LLM: _schedule_tool_params tolerates real-model namespace wrappers without changing the public bundle.
# 函数用途: 兼容模型把 schedule_child_subagents 参数包进 orchestration 字段；顶层显式字段仍优先生效。
def _schedule_tool_params(params: dict[str, object]) -> dict[str, object]:
    nested = params.get("orchestration")
    if not isinstance(nested, dict):
        return params
    merged = dict(nested)
    for key, value in params.items():
        if key not in {"tool", "orchestration"}:
            merged[key] = value
    return merged


# LLM: _schedule_request converts validated tool params into the manager service bundle.
# 函数用途: 构造 HierarchyScheduleRequest，集中处理 apply、children、depth 和审计请求者。
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
# 函数用途: runner 内 schedule_child_subagents 省略 apply 时默认创建；显式 false 仍可预览。
def _schedule_apply_default(params: dict[str, object]) -> bool:
    if "apply" in params:
        return _bool_param(params.get("apply"), default=False)
    return True


# LLM: _schedule_payload_json renders service results without leaking large workspace content.
# 函数用途: 输出层级调度结果摘要，包含新建 run id、层级关系和阻断原因。
def _schedule_payload_json(result: HierarchyScheduleResult) -> str:
    payload = {
        "parent_run_id": result.parent_run_id,
        "root_id": result.root_id,
        "dry_run": result.dry_run,
        "blocked": result.blocked,
        "reason": result.reason,
        "created_run_ids": result.created_run_ids,
        "planned_count": result.planned_count,
        "items": [_schedule_item_payload(item) for item in result.items],
    }
    if result.quality_advice is not None:
        payload["quality_advice"] = _quality_advice_payload(result.quality_advice)
    if result.scheduling_warnings:
        payload["scheduling_warnings"] = list(result.scheduling_warnings)
        payload["coordination_advice"] = (
            "这些是审计提示，不是底层阻断。父级需要用看板、消息或任务说明协调文件 ownership；"
            "如果是 QA 后修复或共享文件补丁，可以继续执行并在最终报告里说明原因。"
        )
    payload["typed_envelope"] = subagent_schedule_envelope_from_payload(
        payload,
        tool="schedule_child_subagents",
    ).to_dict()
    return json.dumps(payload, ensure_ascii=False, indent=2)


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


# LLM: _quality_advice_payload makes QA planning visible without materializing a fixed workflow.
# 函数用途: 把服务层 quality_advice 转成模型可读 JSON，提示候选角色和红线，实际派工仍由 LLM 决定。
def _quality_advice_payload(advice) -> dict[str, object]:
    return {
        "phase": advice.phase,
        "llm_next_step": advice.llm_next_step,
        "guardrails": list(advice.guardrails),
        "suggested_roles": list(advice.suggested_roles),
        "suggested_children": [_quality_child_payload(item) for item in advice.suggested_children],
    }


# LLM: _quality_child_payload keeps suggested QA specs refs-only and safe for prompt reuse.
# 函数用途: 输出候选 QA child 的最小字段，LLM 可复制后按 scope/work_group_id 自行调整。
def _quality_child_payload(item) -> dict[str, object]:
    return {
        "goal": item.goal,
        "agent_name": item.agent_name,
        "role": item.role,
        "acceptance_checks": list(item.acceptance_checks),
    }


# LLM: _hierarchy_child_specs parses the model-provided children list into strict schedule bundles.
# 函数用途: 解析 schedule_child_subagents.children，支持 JSON 字符串或对象列表并拒绝空 goal。
def _hierarchy_child_specs(params: dict[str, object]) -> list[HierarchyChildSpec] | ToolExecutionResult:
    raw_children = params.get("children") if "children" in params else params.get("child_specs")
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
        target_error = external_write_target_error(agent, spec.goal, spec.allowed_tools)
        if target_error:
            return target_error
    return ""


# LLM: _schedule_max_depth accepts model-friendly relative depth when absolute depth would block all children.
# 函数用途: 兼容模型把 max_depth=1 理解成“再开一层”的写法，同时保留顶层绝对 depth 语义。
def _schedule_max_depth(agent, parent_run_id: str, params: dict[str, object]) -> int:
    parsed = _non_negative_int(params.get("max_depth"), default=3)
    if "max_depth" not in params:
        return parsed
    try:
        parent_depth = int(agent.subagents.load(parent_run_id).depth)
    except Exception:
        return parsed
    if parsed <= parent_depth:
        return parent_depth + max(1, parsed)
    return parsed
