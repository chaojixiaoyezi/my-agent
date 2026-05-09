# LLM: Model-callable hierarchy tools let a running subagent create only its own next layer.
# 模块用途: 承载 schedule_child_subagents 工具，把 runner 内部层级派工和通用编排工具解耦。

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

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
        parent_run_id = current_subagent_run_id(self.agent)
        if not parent_run_id:
            return _schedule_error("缺少当前 subagent runner 上下文；顶层派工请使用 create_subagents。")
        child_specs = _hierarchy_child_specs(params)
        if isinstance(child_specs, ToolExecutionResult):
            return child_specs
        target_error = _hierarchy_target_error(self.agent, child_specs)
        if target_error:
            return _schedule_error(target_error)
        result = self.agent.subagents.schedule_child_runs(
            params=_schedule_request(
                ScheduleRequestBuildParams(self.agent, parent_run_id, child_specs, params)
            )
        )
        return ToolExecutionResult("schedule_child_subagents", True, _schedule_payload_json(result))


# LLM: _schedule_error keeps all schedule_child_subagents failures consistently named.
# 函数用途: 生成层级调度工具的失败结果，便于模型和测试稳定识别工具名。
def _schedule_error(message: str) -> ToolExecutionResult:
    return ToolExecutionResult("schedule_child_subagents", False, message)


# LLM: _schedule_request converts validated tool params into the manager service bundle.
# 函数用途: 构造 HierarchyScheduleRequest，集中处理 apply、children、depth 和审计请求者。
def _schedule_request(request: ScheduleRequestBuildParams) -> HierarchyScheduleRequest:
    return HierarchyScheduleRequest(
        parent_run_id=request.parent_run_id,
        child_specs=request.child_specs,
        apply=_bool_param(request.raw_params.get("apply"), default=False),
        requested_by=request.parent_run_id,
        max_children=_non_negative_int(request.raw_params.get("max_children"), default=0),
        max_depth=_schedule_max_depth(request.agent, request.parent_run_id, request.raw_params),
    )


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


# LLM: _hierarchy_child_specs parses the model-provided children list into strict schedule bundles.
# 函数用途: 解析 schedule_child_subagents.children，支持 JSON 字符串或对象列表并拒绝空 goal。
def _hierarchy_child_specs(params: dict[str, object]) -> list[HierarchyChildSpec] | ToolExecutionResult:
    raw_children = params.get("children") or params.get("child_specs")
    children = _json_list_param(raw_children)
    if not children:
        return _schedule_error("缺少必填参数 children。")
    specs: list[HierarchyChildSpec] = []
    for raw in children:
        spec = _hierarchy_child_spec(raw)
        if isinstance(spec, ToolExecutionResult):
            return spec
        specs.append(spec)
    return specs


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
