# LLM: Subagent workflow planner module; keep route, compile, and acceptance bundle shapes stable.
# 模块用途: 拆分子代理工作流的规划、编译、验收或存储逻辑。

from __future__ import annotations

"""Structured contracts for reusable subagent workflow templates."""

from dataclasses import dataclass, field


# LLM: WorkflowPhase 属于子代理工作流编排的类边界；调整时先确认模板选择、步骤编译和验收策略仍按原契约工作。
# 类用途: 集中保存工作流phase字段，让调用方按同一参数包传递上下文；关键副作用: 方法可能触发模板选择、步骤编译和验收策略相关副作用，需保持公开契约稳定。
@dataclass
class WorkflowPhase:
    """One executable phase in a workflow template."""

    id: str
    kind: str
    task: str
    acceptance: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)


# LLM: WorkflowTemplate 属于子代理工作流编排的类边界；调整时先确认模板选择、步骤编译和验收策略仍按原契约工作。
# 类用途: 集中保存工作流模板字段，让调用方按同一参数包传递上下文；关键副作用: 方法可能触发模板选择、步骤编译和验收策略相关副作用，需保持公开契约稳定。
@dataclass
class WorkflowTemplate:
    """A reusable workflow shape that can be selected for a parent task."""

    id: str
    name: str
    solves: list[str]
    fit_for: list[str]
    phases: list[WorkflowPhase]
    parent_acceptance: list[str]
    source: str = ""
    source_path: str = ""


# LLM: WorkflowLoadIssue 属于子代理工作流编排的类边界；调整时先确认模板选择、步骤编译和验收策略仍按原契约工作。
# 类用途: 集中保存工作流loadissue字段，让调用方按同一参数包传递上下文；关键副作用: 方法可能触发模板选择、步骤编译和验收策略相关副作用，需保持公开契约稳定。
@dataclass
class WorkflowLoadIssue:
    """A non-fatal template loading or validation problem."""

    message: str
    source_path: str = ""
    template_id: str = ""
    field: str = ""
    severity: str = "error"
