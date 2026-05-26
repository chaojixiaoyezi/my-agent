# LLM: Hierarchy schedule result rendering stays separate from child creation.
# 模块用途: 把层级调度的 blocked/dry-run/applied 返回结构集中组装，避免调度器文件继续膨胀。

from __future__ import annotations

import time

from ..models import SubAgentTask
from . import hierarchy_context as hctx
from .hierarchy_agent_names import scheduled_child_agent_name
from .hierarchy_schedule_idempotency import (
    ScheduledChildResolution,
    created_scheduled_children,
    dispatchable_scheduled_children,
    reused_scheduled_children,
)
from .hierarchy_scheduled_role import scheduled_child_role
from .hierarchy_scheduler_models import (
    HierarchyResultBuildRequest,
    HierarchyScheduledItem,
    HierarchyScheduleRequest,
    HierarchyScheduleResult,
)
from .hierarchy_write_policy import ChildWriteRootRequest, requested_child_write_roots


# LLM: blocked_schedule_result returns a refs-only plan without creating child runs.
# 函数用途: 构造阻断结果，保持 dry-run 形状稳定。
def blocked_schedule_result(build: HierarchyResultBuildRequest, reason: str) -> HierarchyScheduleResult:
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
        items=planned_schedule_items(parent, request),
        quality_advice=build.quality_advice,
        scheduling_warnings=list(build.scheduling_warnings),
    )


# LLM: dry_schedule_result previews exact depth/root/parent values without touching task files.
# 函数用途: 构造非写入预览结果，让上级代理先确认会创建什么。
def dry_schedule_result(build: HierarchyResultBuildRequest) -> HierarchyScheduleResult:
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
        items=planned_schedule_items(parent, request),
        quality_advice=build.quality_advice,
        scheduling_warnings=list(build.scheduling_warnings),
    )


# LLM: applied_schedule_result exposes create/reuse/dispatch ids from resolved children.
# 函数用途: 把已解析的子任务结果转成公开调度摘要，不负责创建或读取大产物。
def applied_schedule_result(
    build: HierarchyResultBuildRequest,
    resolutions: list[ScheduledChildResolution],
) -> HierarchyScheduleResult:
    created = created_scheduled_children(resolutions)
    reused = reused_scheduled_children(resolutions)
    dispatchable = dispatchable_scheduled_children(resolutions)
    parent = build.parent
    request = build.request
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
        items=[resolved_schedule_item(item) for item in resolutions],
        quality_advice=build.quality_advice,
        scheduling_warnings=list(build.scheduling_warnings),
    )


# LLM: planned_schedule_items mirrors created item shape while keeping run_id empty in dry-runs.
# 函数用途: 生成预览条目，调用方无需猜测 root/depth/parent。
def planned_schedule_items(parent: SubAgentTask, request: HierarchyScheduleRequest) -> list[HierarchyScheduledItem]:
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
                goal=hctx.scheduled_child_goal(parent, spec),
            ),
            agent_name=scheduled_child_agent_name(parent, spec, sibling_index=index),
            goal=hctx.scheduled_child_goal(parent, spec),
            created=False,
            reason="planned",
        )
        for index, spec in enumerate(request.child_specs, start=1)
    ]


# LLM: resolved_schedule_item converts persisted child task data into the public schedule item shape.
# 函数用途: 返回新建或复用 run 的轻量摘要，不读取 artifact 正文。
def resolved_schedule_item(resolution: ScheduledChildResolution) -> HierarchyScheduledItem:
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
