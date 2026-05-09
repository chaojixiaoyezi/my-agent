# LLM: Hierarchy scheduler creates child runs through explicit bundles, never from free-form runner text.
# 模块用途: 受控创建子代理/孙代理层级，统一限制深度、数量和 dry-run/apply 边界。

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from ..models import SubAgentTask
from .base import CreateRunParams


# LLM: HierarchyChildSpec is the stable bundle for one planned descendant run.
# 类用途: 描述一个待创建的子/孙代理任务，避免用散乱 kwargs 扩展层级调度接口。
@dataclass(frozen=True)
class HierarchyChildSpec:
    goal: str
    agent_name: str = "worker"
    role: str = "worker"
    thought: str = ""
    plan: list[str] = field(default_factory=list)
    allowed_skills: list[str] = field(default_factory=list)
    allowed_tools: list[str] = field(default_factory=list)
    acceptance_checks: list[str] = field(default_factory=list)
    extra_write_roots: list[str] = field(default_factory=list)


# LLM: HierarchyScheduleRequest is the only business entrypoint for hierarchy materialization.
# 类用途: 集中保存层级调度的 parent、候选子任务、限制和执行模式。
@dataclass(frozen=True)
class HierarchyScheduleRequest:
    parent_run_id: str
    child_specs: list[HierarchyChildSpec]
    apply: bool = False
    requested_by: str = "parent"
    max_children: int = 0
    max_depth: int = 2


# LLM: HierarchyScheduledItem reports either a planned or created child without loading large artifacts.
# 类用途: 返回单个调度条目摘要，给 CLI、测试和后续调度器读取。
@dataclass(frozen=True)
class HierarchyScheduledItem:
    run_id: str
    parent_id: str
    root_id: str
    depth: int
    role: str
    agent_name: str
    goal: str
    created: bool
    reason: str = ""


# LLM: HierarchyScheduleResult keeps automatic behavior visible and conservative by default.
# 类用途: 返回层级调度结果、阻断原因和是否真正创建 run。
@dataclass(frozen=True)
class HierarchyScheduleResult:
    generated_at: float
    parent_run_id: str
    root_id: str
    dry_run: bool
    blocked: bool
    reason: str
    requested_by: str
    planned_count: int
    created_run_ids: list[str]
    items: list[HierarchyScheduledItem]
    manual_confirmation_required: bool = True
    automatic_execution_allowed: bool = False


# LLM: SubAgentHierarchyScheduler owns hierarchy limits and delegates actual persistence to SubAgentManager.
# 类用途: 封装层级创建规则；只通过 manager.create_run 写任务，避免绕开既有工单/控制面同步。
class SubAgentHierarchyScheduler:
    """Create child or grandchild task records under an existing parent task."""

    # LLM: __init__ stores the manager facade used for loading and creating task records.
    # 函数用途: 初始化层级调度服务依赖；本身不读写文件。
    def __init__(self, manager: Any) -> None:
        self.manager = manager

    # LLM: schedule_children validates hierarchy limits before optional materialization.
    # 函数用途: dry-run 或真正创建下一层 run；超过深度/数量限制时只返回 blocked。
    def schedule_children(self, request: HierarchyScheduleRequest) -> HierarchyScheduleResult:
        parent = self.manager.load(request.parent_run_id)
        reason = _schedule_block_reason(parent, request)
        if reason:
            return _blocked_result(parent, request, reason)
        if not request.apply:
            return _dry_run_result(parent, request)
        return _apply_result(self.manager, parent, request)


# LLM: _schedule_block_reason keeps guard checks deterministic and side-effect free.
# 函数用途: 判断本轮层级调度是否因深度、数量或空计划被阻断。
def _schedule_block_reason(parent: SubAgentTask, request: HierarchyScheduleRequest) -> str:
    if not request.child_specs:
        return "no_child_specs"
    if parent.depth + 1 > request.max_depth:
        return f"max_depth_exceeded:{request.max_depth}"
    if request.max_children > 0 and len(parent.child_ids) + len(request.child_specs) > request.max_children:
        return f"max_children_exceeded:{request.max_children}"
    return ""


# LLM: _blocked_result returns a refs-only plan without creating child runs.
# 函数用途: 构造阻断结果，保持 dry-run 形状稳定。
def _blocked_result(
    parent: SubAgentTask,
    request: HierarchyScheduleRequest,
    reason: str,
) -> HierarchyScheduleResult:
    return HierarchyScheduleResult(
        generated_at=time.time(),
        parent_run_id=parent.id,
        root_id=parent.root_id or parent.id,
        dry_run=not request.apply,
        blocked=True,
        reason=reason,
        requested_by=request.requested_by,
        planned_count=len(request.child_specs),
        created_run_ids=[],
        items=_planned_items(parent, request),
    )


# LLM: _dry_run_result previews exact depth/root/parent values without touching task files.
# 函数用途: 构造非写入预览结果，让上级代理先确认会创建什么。
def _dry_run_result(parent: SubAgentTask, request: HierarchyScheduleRequest) -> HierarchyScheduleResult:
    return HierarchyScheduleResult(
        generated_at=time.time(),
        parent_run_id=parent.id,
        root_id=parent.root_id or parent.id,
        dry_run=True,
        blocked=False,
        reason="dry_run",
        requested_by=request.requested_by,
        planned_count=len(request.child_specs),
        created_run_ids=[],
        items=_planned_items(parent, request),
    )


# LLM: _apply_result materializes planned specs through create_run so all persistence adapters stay in sync.
# 函数用途: 逐个创建下一层 run，并返回 refs-only 创建摘要。
def _apply_result(
    manager: Any,
    parent: SubAgentTask,
    request: HierarchyScheduleRequest,
) -> HierarchyScheduleResult:
    created = [_create_child(manager, parent, spec) for spec in request.child_specs]
    return HierarchyScheduleResult(
        generated_at=time.time(),
        parent_run_id=parent.id,
        root_id=parent.root_id or parent.id,
        dry_run=False,
        blocked=False,
        reason="created",
        requested_by=request.requested_by,
        planned_count=len(request.child_specs),
        created_run_ids=[item.id for item in created],
        items=[_created_item(item) for item in created],
    )


# LLM: _create_child converts one schedule spec into the existing CreateRunParams bundle.
# 函数用途: 复用现有 create_run 路径创建子任务，保证 work-order、runtime workspace 和控制面同步。
def _create_child(manager: Any, parent: SubAgentTask, spec: HierarchyChildSpec) -> SubAgentTask:
    return manager.create_run(
        params=CreateRunParams(
            goal=spec.goal,
            thought=spec.thought or f"执行由 {parent.id} 派生的层级子任务。",
            plan=spec.plan or ["读取父级 refs", "执行小切片", "写回状态和证据 refs", "等待父级验收"],
            agent_name=spec.agent_name or spec.role or "worker",
            role=spec.role or "worker",
            parent_id=parent.id,
            root_id=parent.root_id or parent.id,
            depth=parent.depth + 1,
            allowed_skills=spec.allowed_skills or list(parent.allowed_skills),
            allowed_tools=spec.allowed_tools or list(parent.allowed_tools),
            owner=spec.agent_name or spec.role or parent.owner,
            supervisor=parent.id,
            final_owner=parent.final_owner or parent.owner,
            acceptance_checks=spec.acceptance_checks,
            quality_contract=parent.quality_contract,
            context_manifest=parent.context_manifest,
            context_packs=parent.context_packs,
            extra_write_roots=spec.extra_write_roots or [],
            workflow_mode="off",
        )
    )


# LLM: _planned_items mirrors created item shape while keeping run_id empty in dry-runs.
# 函数用途: 生成预览条目，调用方无需猜测 root/depth/parent。
def _planned_items(parent: SubAgentTask, request: HierarchyScheduleRequest) -> list[HierarchyScheduledItem]:
    return [
        HierarchyScheduledItem(
            run_id="",
            parent_id=parent.id,
            root_id=parent.root_id or parent.id,
            depth=parent.depth + 1,
            role=spec.role or "worker",
            agent_name=spec.agent_name or spec.role or "worker",
            goal=spec.goal,
            created=False,
            reason="planned",
        )
        for spec in request.child_specs
    ]


# LLM: _created_item converts persisted child task data into the public schedule item shape.
# 函数用途: 返回已创建 run 的轻量摘要，不读取 artifact 正文。
def _created_item(task: SubAgentTask) -> HierarchyScheduledItem:
    return HierarchyScheduledItem(
        run_id=task.id,
        parent_id=task.parent_id,
        root_id=task.root_id,
        depth=task.depth,
        role=task.role,
        agent_name=task.agent_name,
        goal=task.goal,
        created=True,
    )
