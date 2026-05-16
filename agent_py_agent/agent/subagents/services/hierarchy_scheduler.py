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
from .hierarchy_child_context import child_context_manifest, child_context_packs
from .hierarchy_context import inherited_hierarchy_thought, scheduled_child_goal
from .hierarchy_qa_scheduler import qa_orchestration_advice
from .hierarchy_schedule_idempotency import (
    ScheduledChildResolution,
    created_scheduled_children,
    dispatchable_scheduled_children,
    resolve_scheduled_child,
    reused_scheduled_children,
)
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
    active_duplicate_child_reason,
    qa_phase_block_reason,
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
        # LLM: red-line guards stay hard; duplicate coordination domains are emitted as warnings.
        reason = (
            schedule_block_reason(parent, request)
            or qa_phase_block_reason(
                self.manager,
                parent,
                request,
            )
            or active_duplicate_child_reason(self.manager, parent, request)
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
    resolutions = [
        _resolve_child(manager, parent, spec, sibling_index=index)
        for index, spec in enumerate(request.child_specs, start=1)
    ]
    created = created_scheduled_children(resolutions)
    reused = reused_scheduled_children(resolutions)
    dispatchable = dispatchable_scheduled_children(resolutions)
    return HierarchyScheduleResult(
        generated_at=time.time(),
        parent_run_id=parent.id,
        root_id=parent.root_id or parent.id,
        dry_run=False,
        blocked=False,
        reason=_apply_reason(resolutions),
        requested_by=request.requested_by,
        planned_count=len(request.child_specs),
        created_run_ids=[item.id for item in created],
        reused_run_ids=[item.id for item in reused],
        dispatch_run_ids=[item.id for item in dispatchable],
        items=[_resolved_item(item) for item in resolutions],
        quality_advice=build.quality_advice,
        scheduling_warnings=list(build.scheduling_warnings),
    )


# LLM: _resolve_child converts one spec into a schedule contract before creating or reusing a run.
# 函数用途: 复用现有 create_run 路径或返回同合同 direct child；child spec 写出的产物路径仍由角色策略裁决。
def _resolve_child(
    manager: Any,
    parent: SubAgentTask,
    spec: HierarchyChildSpec,
    *,
    sibling_index: int,
) -> ScheduledChildResolution:
    return resolve_scheduled_child(
        manager,
        _child_create_params(parent, spec, sibling_index=sibling_index),
    )


# LLM: _child_create_params derives CreateRunParams without mutating task state.
# 函数用途: 把 child spec、父级状态、工具策略和命名规则转换为可比较/可创建的参数 bundle。
def _child_create_params(
    parent: SubAgentTask,
    spec: HierarchyChildSpec,
    *,
    sibling_index: int,
) -> CreateRunParams:
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
    return _create_child_params(
        HierarchyCreateChildRequest(
            parent=parent,
            spec=spec,
            role=role,
            goal=goal,
            extra_write_roots=extra_write_roots,
            sibling_index=sibling_index,
        )
    )


# LLM: _apply_reason exposes mixed create/reuse outcomes without forcing the model to infer from ids.
# 函数用途: 给调度结果提供稳定 reason；纯复用、纯创建、混合三种情况都明确。
def _apply_reason(resolutions: list[ScheduledChildResolution]) -> str:
    if not resolutions:
        return "created"
    created_count = sum(1 for item in resolutions if not item.reused)
    reused_count = len(resolutions) - created_count
    if created_count and reused_count:
        return "created_or_reused"
    if reused_count:
        return "reused"
    return "created"


# LLM: _create_child_params maps derived hierarchy facts into the existing CreateRunParams bundle.
# 函数用途: 只负责组装 create_run 参数；不创建文件、不读取状态，便于后续增字段时局部修改。
def _create_child_params(request: HierarchyCreateChildRequest) -> CreateRunParams:
    parent = request.parent
    spec = request.spec
    agent_name = scheduled_child_agent_name(parent, spec, sibling_index=request.sibling_index)
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
        context_manifest=child_context_manifest(parent, spec),
        context_packs=child_context_packs(parent, spec),
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
            agent_name=scheduled_child_agent_name(parent, spec, sibling_index=index),
            goal=scheduled_child_goal(parent, spec),
            created=False,
            reason="planned",
        )
        for index, spec in enumerate(request.child_specs, start=1)
    ]


# LLM: _resolved_item converts persisted child task data into the public schedule item shape.
# 函数用途: 返回新建或复用 run 的轻量摘要，不读取 artifact 正文。
def _resolved_item(resolution: ScheduledChildResolution) -> HierarchyScheduledItem:
    task = resolution.task
    return HierarchyScheduledItem(
        run_id=task.id,
        parent_id=task.parent_id,
        root_id=task.root_id,
        depth=task.depth,
        role=task.role,
        agent_name=task.agent_name,
        goal=task.goal,
        created=not resolution.reused,
        reason="reused" if resolution.reused else "created",
    )
