
from __future__ import annotations

import time

from ...models import SubAgentTask
from . import context as hctx
from .schedule_idempotency import (
    ScheduledChildResolution,
    created_scheduled_children,
    dispatchable_scheduled_children,
    reused_scheduled_children,
)
from .scheduled_role import scheduled_child_agent_name, scheduled_child_role
from .scheduler_models import (
    HierarchyResultBuildRequest,
    HierarchyScheduledItem,
    HierarchyScheduleRequest,
    HierarchyScheduleResult,
)
from .write_policy import ChildWriteRootRequest, requested_child_write_roots


# LLM: A blocked result is observational and must preview the same sibling names
# that apply would use without creating or mutating child records.
# 函数用途: 构造被层级/权限限制拦下时的派工结果和一致的名称预览。
def limit_schedule_result(build: HierarchyResultBuildRequest, reason: str) -> HierarchyScheduleResult:
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
        items=planned_schedule_items(build),
        quality_advice=build.quality_advice,
    )


# LLM: Dry-run consumes the prepared build snapshot only and never advances the
# persistent sibling history by itself.
# 函数用途: 返回不落盘的递归派工预览，供调用方查看将创建的直属下级。
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
        items=planned_schedule_items(build),
        quality_advice=build.quality_advice,
    )


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
    )


# LLM: Dry-run and blocked previews must use the same historical sibling ordinal
# as apply, otherwise a preview can advertise names that the real create never
# assigns. The build request carries the parent snapshot used by both paths.
# 函数用途: 按同一父级历史编号预览即将创建的递归子代理名称与角色。
def planned_schedule_items(build: HierarchyResultBuildRequest) -> list[HierarchyScheduledItem]:
    parent = build.parent
    request = build.request
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
        for index, spec in enumerate(
            request.child_specs,
            start=build.sibling_start_index,
        )
    ]


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
