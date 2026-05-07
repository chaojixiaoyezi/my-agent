# LLM: Subagent workflow planner module; keep route, compile, and acceptance bundle shapes stable.
# 模块用途: 拆分子代理工作流的规划、编译、验收或存储逻辑。

from __future__ import annotations

"""Compile workflow templates into reviewable worker dispatch plans."""

from dataclasses import dataclass, field
from typing import Any

from .models import WorkflowTemplate


# LLM: WorkflowWorkerSpec 属于子代理工作流编排的类边界；调整时先确认模板选择、步骤编译和验收策略仍按原契约工作。
# 类用途: 集中保存工作流工作器spec字段，让调用方按同一参数包传递上下文；关键副作用: 方法可能触发模板选择、步骤编译和验收策略相关副作用，需保持公开契约稳定。
@dataclass
class WorkflowWorkerSpec:
    """Reviewable instructions for one workflow phase worker."""

    phase_id: str
    role: str
    kind: str
    goal: str
    instructions: str
    acceptance_checks: list[str] = field(default_factory=list)
    allowed_write_roots: list[str] = field(default_factory=list)
    forbidden_write_roots: list[str] = field(default_factory=list)
    quality_contract: Any = None
    context_manifest: Any = None
    cannot_self_accept: bool = True
    depends_on: list[str] = field(default_factory=list)


# LLM: WorkflowDispatchPlan 属于子代理工作流编排的类边界；调整时先确认模板选择、步骤编译和验收策略仍按原契约工作。
# 类用途: 集中保存工作流调度计划字段，让调用方按同一参数包传递上下文；关键副作用: 方法可能触发模板选择、步骤编译和验收策略相关副作用，需保持公开契约稳定。
@dataclass
class WorkflowDispatchPlan:
    """A compiled workflow that can be inspected before dispatch."""

    template_id: str
    template_name: str
    goal: str
    worker_specs: list[WorkflowWorkerSpec]
    parent_acceptance: list[str] = field(default_factory=list)
    quality_contract: Any = None
    context_manifest: Any = None


# LLM: CompileWorkflowParams 属于子代理工作流编排的类边界；调整时先确认模板选择、步骤编译和验收策略仍按原契约工作。
# 类用途: 集中保存compile工作流参数字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass(frozen=True)
class CompileWorkflowParams:
    # LLM: 工作流编译输入先收束成参数包，再展开为各工作器规格。
    goal: str
    quality_contract: Any = None
    context_manifest: Any = None
    allowed_write_roots: list[str] | None = None
    forbidden_write_roots: list[str] | None = None


# LLM: _WorkerSpecRequest 属于子代理工作流编排的类边界；调整时先确认模板选择、步骤编译和验收策略仍按原契约工作。
# 类用途: 集中保存工作器spec请求字段，让调用方按同一参数包传递上下文；关键副作用: 方法可能触发模板选择、步骤编译和验收策略相关副作用，需保持公开契约稳定。
@dataclass(frozen=True)
class _WorkerSpecRequest:
    template: WorkflowTemplate
    phase: object
    values: CompileWorkflowParams
    allowed_roots: list[str]
    forbidden_roots: list[str]


# LLM: compile_workflow 属于子代理工作流编排的函数边界；调整时先确认模板选择、步骤编译和验收策略仍按原契约工作。
# 函数用途: 处理compile工作流相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持模板选择、步骤编译和验收策略上的返回值和副作用边界稳定。
def compile_workflow(
    template: WorkflowTemplate,
    *,
    params: CompileWorkflowParams | None = None,
    goal: str = "",
    quality_contract: Any = None,
    context_manifest: Any = None,
    allowed_write_roots: list[str] | None = None,
    forbidden_write_roots: list[str] | None = None,
) -> WorkflowDispatchPlan:
    """Compile a template into worker specs without creating SubAgentTask."""

    values = params or CompileWorkflowParams(
        goal, quality_contract, context_manifest, allowed_write_roots, forbidden_write_roots
    )
    allowed_roots = list(values.allowed_write_roots or [])
    forbidden_roots = list(values.forbidden_write_roots or [])
    worker_specs = [
        _worker_spec(_WorkerSpecRequest(template, phase, values, allowed_roots, forbidden_roots))
        for phase in template.phases
    ]

    return WorkflowDispatchPlan(
        template_id=template.id,
        template_name=template.name,
        goal=values.goal,
        worker_specs=worker_specs,
        parent_acceptance=list(template.parent_acceptance),
        quality_contract=values.quality_contract,
        context_manifest=values.context_manifest,
    )


# LLM: _worker_spec 属于子代理工作流编排的函数边界；调整时先确认模板选择、步骤编译和验收策略仍按原契约工作。
# 函数用途: 处理工作器spec相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持模板选择、步骤编译和验收策略上的返回值和副作用边界稳定。
def _worker_spec(request: _WorkerSpecRequest) -> WorkflowWorkerSpec:
    # LLM: 阶段展开逻辑留在内部，公共编译入口只负责串接模板到计划。
    template = request.template
    phase = request.phase
    values = request.values
    return WorkflowWorkerSpec(
        phase_id=phase.id,
        role=phase.kind,
        kind=phase.kind,
        goal=values.goal,
        instructions=_build_worker_instructions(
            template=template,
            phase_id=phase.id,
            phase_kind=phase.kind,
            phase_task=phase.task,
        ),
        acceptance_checks=list(phase.acceptance),
        allowed_write_roots=list(request.allowed_roots),
        forbidden_write_roots=list(request.forbidden_roots),
        quality_contract=values.quality_contract,
        context_manifest=values.context_manifest,
        cannot_self_accept=True,
        depends_on=list(phase.depends_on),
    )


# LLM: _build_worker_instructions 属于子代理工作流编排的函数边界；调整时先确认模板选择、步骤编译和验收策略仍按原契约工作。
# 函数用途: 构建工作器instructions所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 主要返回派生结构或文本，需保持字段名、顺序和空值处理稳定。
def _build_worker_instructions(
    *,
    template: WorkflowTemplate,
    phase_id: str,
    phase_kind: str,
    phase_task: str,
) -> str:
    return "\n".join(
        [
            f"Workflow template: {template.id} ({template.name}).",
            f"Phase: {phase_id} ({phase_kind}).",
            f"Phase task: {phase_task}",
            "You are not the only worker; other workers may modify adjacent router, gate, or workflow code in parallel.",
            "Do not roll back or overwrite changes made by other workers.",
            "You cannot self-accept final completion; parent acceptance must make the final call.",
            "Leave concrete evidence for every claimed result, including commands run, files changed, and verification output.",
            "Report residual risks and any deferred or unverified items before handing off.",
        ]
    )
