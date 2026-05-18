# LLM: Subagent workflow planner module; keep route, compile, and acceptance bundle shapes stable.
# 模块用途: 拆分子代理工作流的规划、编译、验收或存储逻辑。

from __future__ import annotations

"""workflow planning composes route, compile, and parent acceptance through bundles."""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .acceptance import ParentAcceptancePlan, plan_parent_acceptance
from .compiler import WorkflowDispatchPlan, compile_workflow
from .models import WorkflowTemplate
from .router import WorkflowRouteDecision, WorkflowRouteRequest, route_workflow
from .store import WorkflowTemplateStore, load_template_store


# LLM: WorkflowPlanConstraints 属于子代理工作流编排的类边界；调整时先确认模板选择、步骤编译和验收策略仍按原契约工作。
# 类用途: 集中保存工作流计划constraints字段，让调用方按同一参数包传递上下文；关键副作用: 方法可能触发模板选择、步骤编译和验收策略相关副作用，需保持公开契约稳定。
@dataclass
class WorkflowPlanConstraints:
    """Bundle for plan_workflow_for_goal keyword parameters (excluding goal)."""

    config: Any = None
    template_store: WorkflowTemplateStore | None = None
    explicit_template_id: str = ""
    workflow_task_type: str = ""
    workflow_risk_tags: object = None
    quality_contract: Any = None
    context_manifest: Any = None
    allowed_write_roots: list[str] | None = None
    forbidden_write_roots: list[str] | None = None


# LLM: WorkflowPlanningResult 属于子代理工作流编排的类边界；调整时先确认模板选择、步骤编译和验收策略仍按原契约工作。
# 类用途: 集中保存工作流planning结果字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass
class WorkflowPlanningResult:
    """A parent-reviewable workflow plan for a user goal."""

    goal: str
    decision: WorkflowRouteDecision
    enabled: bool
    template: WorkflowTemplate | None = None
    dispatch_plan: WorkflowDispatchPlan | None = None
    parent_acceptance_plan: ParentAcceptancePlan | None = None
    issues: list[str] = field(default_factory=list)

    # LLM: ok 属于子代理工作流编排的函数边界；调整时先确认模板选择、步骤编译和验收策略仍按原契约工作。
    # 函数用途: 处理ok相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持模板选择、步骤编译和验收策略上的返回值和副作用边界稳定。
    @property
    def ok(self) -> bool:
        return self.enabled and self.template is not None and self.dispatch_plan is not None

    # LLM: selected_template_id 属于子代理工作流编排的函数边界；调整时先确认模板选择、步骤编译和验收策略仍按原契约工作。
    # 函数用途: 读取或查询selected模板id需要的状态，返回调用方可继续处理的快照；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
    @property
    def selected_template_id(self) -> str:
        return self.decision.selected_template_id

    # LLM: to_dict 属于子代理工作流编排的函数边界；调整时先确认模板选择、步骤编译和验收策略仍按原契约工作。
    # 函数用途: 转换dict的数据表示，保持跨模块传递时的字段含义一致；关键副作用: 需保持模板选择、步骤编译和验收策略上的返回值和副作用边界稳定。
    def to_dict(self) -> dict[str, object]:
        """Return the audit-friendly dry-run preview payload."""

        return _planning_payload(self)


# LLM: _CompilePlansRequest 属于子代理工作流编排的类边界；调整时先确认模板选择、步骤编译和验收策略仍按原契约工作。
# 类用途: 集中保存compileplans请求字段，让调用方按同一参数包传递上下文；关键副作用: 方法可能触发模板选择、步骤编译和验收策略相关副作用，需保持公开契约稳定。
@dataclass(frozen=True)
class _CompilePlansRequest:
    template: WorkflowTemplate
    goal: str
    quality_contract: Any
    context_manifest: Any
    allowed_write_roots: list[str] | None
    forbidden_write_roots: list[str] | None


# LLM: _workflow_workers 属于子代理工作流编排的函数边界；调整时先确认模板选择、步骤编译和验收策略仍按原契约工作。
# 函数用途: 处理工作流workers相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持模板选择、步骤编译和验收策略上的返回值和副作用边界稳定。
def _workflow_workers(
    dispatch_plan: WorkflowDispatchPlan | None,
    template_phase_tasks: dict[str, str],
) -> list[dict[str, object]]:
    """Build worker preview rows from a dispatch plan bundle."""
    if dispatch_plan is None:
        return []
    return [
        {
            "phase_id": worker.phase_id,
            "role": worker.role,
            "kind": worker.kind,
            "task": _worker_task_summary(worker, template_phase_tasks),
            "acceptance_check_count": len(worker.acceptance_checks),
            "acceptance_checks": list(worker.acceptance_checks),
            "depends_on": list(worker.depends_on),
        }
        for worker in dispatch_plan.worker_specs
    ]


# LLM: _template_phase_tasks 属于子代理工作流编排的函数边界；调整时先确认模板选择、步骤编译和验收策略仍按原契约工作。
# 函数用途: 处理模板phasetasks相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持模板选择、步骤编译和验收策略上的返回值和副作用边界稳定。
def _template_phase_tasks(template: WorkflowTemplate | None) -> dict[str, str]:
    if template is None:
        return {}
    return {phase.id: phase.task for phase in template.phases}


# LLM: _parent_checklist 属于子代理工作流编排的函数边界；调整时先确认模板选择、步骤编译和验收策略仍按原契约工作。
# 函数用途: 处理父级checklist相关的数据流，连接当前职责的前后步骤；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
def _parent_checklist(parent_acceptance_plan: ParentAcceptancePlan | None) -> list[str]:
    if parent_acceptance_plan is None:
        return []
    return parent_acceptance_plan.checklist


# LLM: _planning_payload_base 属于子代理工作流编排的函数边界；调整时先确认模板选择、步骤编译和验收策略仍按原契约工作。
# 函数用途: 处理planning载荷基础相关的数据流，连接当前职责的前后步骤；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
def _planning_payload_base(result: WorkflowPlanningResult, workers: list[dict[str, object]]) -> dict[str, object]:
    return {
        "goal": result.goal,
        "selected_template_id": result.selected_template_id,
        "mode": result.decision.mode,
        "needs_confirmation": result.decision.needs_confirmation,
        "enabled": result.enabled,
        "ok": result.ok,
        "reason": result.decision.reason,
        "task_type": result.decision.task_type,
        "risk_tags": list(result.decision.risk_tags),
        "worker_count": len(workers),
        "workers": workers,
    }


# LLM: _planning_payload 属于子代理工作流编排的函数边界；调整时先确认模板选择、步骤编译和验收策略仍按原契约工作。
# 函数用途: 处理planning载荷相关的数据流，连接当前职责的前后步骤；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
def _planning_payload(result: WorkflowPlanningResult) -> dict[str, object]:
    workers = _workflow_workers(result.dispatch_plan, _template_phase_tasks(result.template))
    parent_checklist = _parent_checklist(result.parent_acceptance_plan)
    payload = _planning_payload_base(result, workers)
    payload.update(
        {
            "parent_acceptance_check_count": len(parent_checklist),
            "parent_acceptance_checklist": list(parent_checklist),
            "issues": list(result.issues),
        }
    )
    return payload

# LLM: plan_workflow_for_goal 属于子代理工作流编排的函数边界；调整时先确认模板选择、步骤编译和验收策略仍按原契约工作。
# 函数用途: 处理计划工作流目标相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持模板选择、步骤编译和验收策略上的返回值和副作用边界稳定。
def plan_workflow_for_goal(
    goal: str,
    *,
    constraints: WorkflowPlanConstraints,
) -> WorkflowPlanningResult:
    """Create a dry-run workflow plan without creating subagent tasks.

    Use WorkflowPlanConstraints to bundle all parameters.
    """
    _c = constraints
    store = _c.template_store or load_template_store()
    decision = route_workflow(WorkflowRouteRequest(
        goal=goal,
        config=_c.config,
        template_store=store,
        explicit_template_id=_c.explicit_template_id,
        workflow_task_type=_c.workflow_task_type,
        workflow_risk_tags=_c.workflow_risk_tags,
    ))
    issues = list(decision.issues)

    if decision.mode == "off" or not decision.selected_template_id:
        return _make_disabled_result(goal, decision, issues)

    template = store.get(decision.selected_template_id)
    if template is None:
        issues.append(f"selected workflow template not found: {decision.selected_template_id}")
        return _make_disabled_result(goal, decision, issues)

    dispatch_plan, parent_acceptance_plan = _compile_plans(
        _CompilePlansRequest(
            template=template,
            goal=goal,
            quality_contract=_c.quality_contract,
            context_manifest=_c.context_manifest,
            allowed_write_roots=_c.allowed_write_roots,
            forbidden_write_roots=_c.forbidden_write_roots,
        )
    )

    return WorkflowPlanningResult(
        goal=goal,
        decision=decision,
        enabled=True,
        template=template,
        dispatch_plan=dispatch_plan,
        parent_acceptance_plan=parent_acceptance_plan,
        issues=issues,
    )


# LLM: _make_disabled_result 属于子代理工作流编排的函数边界；调整时先确认模板选择、步骤编译和验收策略仍按原契约工作。
# 函数用途: 构建disabled结果所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 主要返回派生结构或文本，需保持字段名、顺序和空值处理稳定。
def _make_disabled_result(
    goal: str,
    decision: WorkflowRouteDecision,
    issues: list[str],
) -> WorkflowPlanningResult:
    """Return a disabled result for early-exit paths."""
    return WorkflowPlanningResult(
        goal=goal,
        decision=decision,
        enabled=False,
        issues=issues,
    )


# LLM: _compile_plans 属于子代理工作流编排的函数边界；调整时先确认模板选择、步骤编译和验收策略仍按原契约工作。
# 函数用途: 处理compileplans相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持模板选择、步骤编译和验收策略上的返回值和副作用边界稳定。
def _compile_plans(request: _CompilePlansRequest) -> tuple[WorkflowDispatchPlan, ParentAcceptancePlan]:
    """Compile both dispatch and parent-acceptance plans from a resolved template."""
    dispatch_plan = compile_workflow(
        request.template,
        goal=request.goal,
        quality_contract=request.quality_contract,
        context_manifest=request.context_manifest,
        allowed_write_roots=request.allowed_write_roots,
        forbidden_write_roots=request.forbidden_write_roots,
    )
    parent_acceptance_plan = plan_parent_acceptance(
        request.template,
        goal=request.goal,
        quality_contract=request.quality_contract,
    )
    return dispatch_plan, parent_acceptance_plan


# LLM: write_workflow_plan_preview 属于子代理工作流编排的函数边界；调整时先确认模板选择、步骤编译和验收策略仍按原契约工作。
# 函数用途: 写入工作流计划preview的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动模板选择、步骤编译和验收策略，调用方依赖写入顺序和文件格式。
def write_workflow_plan_preview(
    result: WorkflowPlanningResult,
    output_dir: str | Path,
) -> dict[str, Path]:
    """Write the dry-run workflow plan preview as JSON and Markdown."""

    preview_dir = Path(output_dir)
    preview_dir.mkdir(parents=True, exist_ok=True)
    json_path = preview_dir / "subagent_workflow_plan_preview.json"
    markdown_path = preview_dir / "SUBAGENT_WORKFLOW_PLAN_PREVIEW.md"
    payload = result.to_dict()

    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    markdown_path.write_text(_render_workflow_plan_preview_markdown(payload), encoding="utf-8")
    return {"json": json_path, "markdown": markdown_path}


# LLM: _render_workflow_plan_preview_markdown 属于子代理工作流编排的函数边界；调整时先确认模板选择、步骤编译和验收策略仍按原契约工作。
# 函数用途: 渲染或汇总工作流计划previewmarkdown的展示文本，保持命令行、日志和审计输出一致；关键副作用: 会更新模板选择、步骤编译和验收策略，需避免破坏既有状态机约定。
def _render_workflow_plan_preview_markdown(payload: dict[str, object]) -> str:
    lines = _workflow_plan_header_lines(payload)
    lines.extend(_workflow_plan_worker_lines(payload.get("workers")))
    lines.extend(["", "## Parent Acceptance Checklist"])
    lines.extend(_workflow_plan_list_lines(payload.get("parent_acceptance_checklist")))
    lines.extend(["", "## Issues"])
    lines.extend(_workflow_plan_list_lines(payload.get("issues")))
    lines.append("")
    return "\n".join(lines)


# LLM: _workflow_plan_header_lines 属于子代理工作流编排的函数边界；调整时先确认模板选择、步骤编译和验收策略仍按原契约工作。
# 函数用途: 处理工作流计划headerlines相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持模板选择、步骤编译和验收策略上的返回值和副作用边界稳定。
def _workflow_plan_header_lines(payload: dict[str, object]) -> list[str]:
    return [
        "# Subagent Workflow Plan Preview",
        "",
        f"- goal: {payload['goal']}",
        f"- selected_template_id: {payload['selected_template_id'] or 'none'}",
        f"- mode: {payload['mode']}",
        f"- needs_confirmation: {payload['needs_confirmation']}",
        f"- enabled: {payload['enabled']}",
        f"- ok: {payload['ok']}",
        f"- task_type: {payload['task_type']}",
        f"- reason: {payload['reason']}",
        "",
        "## Workers",
    ]


# LLM: _workflow_plan_worker_lines 属于子代理工作流编排的函数边界；调整时先确认模板选择、步骤编译和验收策略仍按原契约工作。
# 函数用途: 处理工作流计划工作器lines相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持模板选择、步骤编译和验收策略上的返回值和副作用边界稳定。
def _workflow_plan_worker_lines(workers: object) -> list[str]:
    lines: list[str] = []
    if not isinstance(workers, list) or not workers:
        return ["", "No workers would be planned."]

    for worker in workers:
        if not isinstance(worker, dict):
            continue
        depends_on = ", ".join(str(item) for item in worker.get("depends_on", [])) or "none"
        lines.extend(
            [
                "",
                f"### {worker.get('phase_id', '')}",
                f"- role: {worker.get('role', '')}",
                f"- kind: {worker.get('kind', '')}",
                f"- depends_on: {depends_on}",
                f"- task: {worker.get('task', '')}",
                f"- acceptance_check_count: {worker.get('acceptance_check_count', 0)}",
            ]
        )
    return lines


# LLM: _workflow_plan_list_lines 属于子代理工作流编排的函数边界；调整时先确认模板选择、步骤编译和验收策略仍按原契约工作。
# 函数用途: 处理工作流计划listlines相关的数据流，连接当前职责的前后步骤；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
def _workflow_plan_list_lines(value: object) -> list[str]:
    if isinstance(value, list) and value:
        return [f"- {item}" for item in value]
    return ["- none"]


# LLM: _worker_task_summary 属于子代理工作流编排的函数边界；调整时先确认模板选择、步骤编译和验收策略仍按原契约工作。
# 函数用途: 处理工作器任务summary相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持模板选择、步骤编译和验收策略上的返回值和副作用边界稳定。
def _worker_task_summary(worker: Any, template_phase_tasks: dict[str, str]) -> str:
    task = template_phase_tasks.get(worker.phase_id)
    if task:
        return task
    for line in worker.instructions.splitlines():
        if line.startswith("Phase task: "):
            return line.removeprefix("Phase task: ")
    return worker.instructions.splitlines()[0] if worker.instructions else ""
