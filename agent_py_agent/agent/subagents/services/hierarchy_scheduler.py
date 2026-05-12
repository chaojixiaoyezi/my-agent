# LLM: Hierarchy scheduler creates child runs through explicit bundles, never from free-form runner text.
# 模块用途: 受控创建子代理/孙代理层级，统一限制深度、数量和 dry-run/apply 边界。

from __future__ import annotations

import time
from typing import Any

from ..debug_trace import trace_hierarchy_schedule
from ..models import SubAgentTask
from .base import CreateRunParams
from .hierarchy_acceptance import scheduled_child_acceptance_checks
from .hierarchy_agent_names import scheduled_child_agent_name
from .hierarchy_context import inherited_hierarchy_thought, scheduled_child_goal
from .hierarchy_qa_scheduler import qa_orchestration_advice
from .hierarchy_scheduled_role import scheduled_child_role
from .hierarchy_scheduler_models import (
    HierarchyChildSpec,
    HierarchyCreateChildRequest,
    HierarchyResultBuildRequest,
    HierarchyScheduledItem,
    HierarchyScheduleRequest,
    HierarchyScheduleResult,
)
from .hierarchy_scope_guards import (
    duplicate_child_domain_reason,
    schedule_block_reason,
    schedule_warnings,
)
from .hierarchy_tool_policy import (
    LeafWriteIntentRequest,
    ToolPolicyRequest,
    scheduled_child_tools,
    should_infer_leaf_coding_tools,
)
from .hierarchy_write_policy import (
    ChildWriteRootRequest,
    ScheduledWriteRootRequest,
    inherited_extra_write_roots,
    requested_child_write_roots,
    scheduled_child_extra_write_roots,
)


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
        quality_advice = qa_orchestration_advice(manager=self.manager, parent=parent, specs=request.child_specs)
        result_build = HierarchyResultBuildRequest(
            parent=parent,
            request=request,
            quality_advice=quality_advice,
            scheduling_warnings=schedule_warnings(self.manager, parent, request),
        )
        # LLM: generic guards run first; duplicate-domain guard needs persisted sibling metadata.
        reason = schedule_block_reason(parent, request) or duplicate_child_domain_reason(
            self.manager,
            parent,
            request,
        )
        if reason:
            return trace_hierarchy_schedule(
                self.manager,
                parent,
                _blocked_result(result_build, reason),
            )
        if not request.apply:
            return trace_hierarchy_schedule(
                self.manager,
                parent,
                _dry_run_result(result_build),
            )
        return trace_hierarchy_schedule(
            self.manager,
            parent,
            _apply_result(self.manager, result_build),
        )


# LLM: _blocked_result returns a refs-only plan without creating child runs.
# 函数用途: 构造阻断结果，保持 dry-run 形状稳定。
def _blocked_result(build: HierarchyResultBuildRequest, reason: str) -> HierarchyScheduleResult:
    parent = build.parent
    request = build.request
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
        quality_advice=build.quality_advice,
        scheduling_warnings=list(build.scheduling_warnings),
    )


# LLM: _dry_run_result previews exact depth/root/parent values without touching task files.
# 函数用途: 构造非写入预览结果，让上级代理先确认会创建什么。
def _dry_run_result(
    build: HierarchyResultBuildRequest,
) -> HierarchyScheduleResult:
    parent = build.parent
    request = build.request
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
        quality_advice=build.quality_advice,
        scheduling_warnings=list(build.scheduling_warnings),
    )


# LLM: _apply_result materializes planned specs through create_run so all persistence adapters stay in sync.
# 函数用途: 逐个创建下一层 run，并返回 refs-only 创建摘要。
def _apply_result(
    manager: Any,
    build: HierarchyResultBuildRequest,
) -> HierarchyScheduleResult:
    parent = build.parent
    request = build.request
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
        quality_advice=build.quality_advice,
        scheduling_warnings=list(build.scheduling_warnings),
    )


# LLM: _create_child converts one spec into CreateRunParams while preserving declared product paths.
# 函数用途: 复用现有 create_run 路径创建子任务；child spec 自己写出的产物路径会成为候选根，最终授权仍由角色策略裁决。
def _create_child(manager: Any, parent: SubAgentTask, spec: HierarchyChildSpec) -> SubAgentTask:
    requested_write_roots = requested_child_write_roots(
        ChildWriteRootRequest(
            parent=parent,
            spec_goal=spec.goal,
            explicit_roots=list(spec.extra_write_roots),
        )
    )
    role_probe_goal = scheduled_child_goal(parent, spec, write_roots=requested_write_roots)
    role = scheduled_child_role(parent, spec, requested_write_roots, goal=role_probe_goal)
    extra_write_roots = scheduled_child_extra_write_roots(
        ScheduledWriteRootRequest(
            role=role,
            spec_role=spec.role,
            requested_roots=requested_write_roots,
            leaf_write_intent=should_infer_leaf_coding_tools(
                LeafWriteIntentRequest(
                    spec=spec,
                    extra_write_roots=requested_write_roots,
                    goal=role_probe_goal,
                )
            ),
        )
    )
    goal = scheduled_child_goal(parent, spec, write_roots=extra_write_roots)
    return manager.create_run(
        params=_create_child_params(
            HierarchyCreateChildRequest(
                parent=parent,
                spec=spec,
                role=role,
                goal=goal,
                extra_write_roots=extra_write_roots,
            )
        )
    )


# LLM: _create_child_params maps derived hierarchy facts into the existing CreateRunParams bundle.
# 函数用途: 只负责组装 create_run 参数；不创建文件、不读取状态，便于后续增字段时局部修改。
def _create_child_params(request: HierarchyCreateChildRequest) -> CreateRunParams:
    parent = request.parent
    spec = request.spec
    agent_name = scheduled_child_agent_name(parent, spec)
    return CreateRunParams(
        goal=request.goal,
        thought=spec.thought or inherited_hierarchy_thought(parent, child_goal=request.goal),
        plan=spec.plan or ["读取父级 refs", "执行小切片", "写回状态和证据 refs", "等待父级验收"],
        agent_name=agent_name,
        role=request.role,
        parent_id=parent.id,
        root_id=parent.root_id or parent.id,
        depth=parent.depth + 1,
        allowed_skills=spec.allowed_skills or list(parent.allowed_skills),
        allowed_tools=scheduled_child_tools(
            ToolPolicyRequest(
                parent_tools=list(parent.allowed_tools),
                spec=spec,
                extra_write_roots=request.extra_write_roots,
                goal=request.goal,
            )
        ),
        owner=agent_name,
        supervisor=parent.id,
        final_owner=parent.final_owner or parent.owner,
        acceptance_checks=scheduled_child_acceptance_checks(
            spec,
            role=request.role,
            goal=request.goal,
            leaf_write_intent=should_infer_leaf_coding_tools(
                LeafWriteIntentRequest(
                    spec=spec,
                    extra_write_roots=inherited_extra_write_roots(parent),
                    goal=request.goal,
                )
            ),
        ),
        quality_contract=parent.quality_contract,
        context_manifest=parent.context_manifest,
        context_packs=parent.context_packs,
        extra_write_roots=request.extra_write_roots,
        workflow_mode="off",
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
            role=scheduled_child_role(
                parent,
                spec,
                requested_child_write_roots(
                    ChildWriteRootRequest(
                        parent=parent,
                        spec_goal=spec.goal,
                        explicit_roots=list(spec.extra_write_roots),
                    )
                ),
                goal=scheduled_child_goal(parent, spec),
            ),
            agent_name=scheduled_child_agent_name(parent, spec),
            goal=scheduled_child_goal(parent, spec),
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
