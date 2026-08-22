
from __future__ import annotations

from typing import Any

from ...debug_trace import trace_hierarchy_schedule
from ...models import SubAgentTask
from ..base import CreateRunParams
from . import context as hctx
from .qa_scheduler import qa_orchestration_advice
from .schedule_idempotency import (
    ScheduledChildResolution,
    resolve_scheduled_child,
)
from .scheduled_role import (
    child_context_manifest,
    child_context_packs,
    scheduled_child_agent_name,
    scheduled_child_role,
)
from .scheduler_models import (
    HierarchyChildSpec,
    HierarchyCreateChildRequest,
    HierarchyResultBuildRequest,
    HierarchyScheduleRequest,
    HierarchyScheduleResult,
)
from .scheduler_results import (
    applied_schedule_result,
    dry_schedule_result,
    limit_schedule_result,
)
from .tool_policy import (
    LeafWriteIntentRequest,
    ToolPolicyRequest,
    scheduled_child_tools,
    should_infer_leaf_coding_tools,
)
from .write_policy import (
    ChildWriteRootRequest,
    ScheduledWriteRootRequest,
    requested_child_write_roots,
    scheduled_child_extra_write_roots,
)


class SubAgentHierarchyScheduler:
    """Create child or grandchild task records under an existing parent task."""

    def __init__(self, manager: Any) -> None:
        self.manager = manager

    def schedule_children(self, request: HierarchyScheduleRequest) -> HierarchyScheduleResult:
        parent = self.manager.load(request.parent_run_id)
        quality_advice = qa_orchestration_advice(manager=self.manager, parent=parent, specs=request.child_specs)
        result_build = HierarchyResultBuildRequest(
            parent=parent,
            request=request,
            quality_advice=quality_advice,
        )
        if reason := _explicit_limit_reason(parent, request):
            return trace_hierarchy_schedule(
                self.manager,
                parent,
                limit_schedule_result(result_build, reason),
            )
        if not request.apply:
            return trace_hierarchy_schedule(
                self.manager,
                parent,
                dry_schedule_result(result_build),
            )
        return trace_hierarchy_schedule(
            self.manager,
            parent,
            _apply_result(self.manager, result_build),
        )


def _explicit_limit_reason(parent: SubAgentTask, request: HierarchyScheduleRequest) -> str:
    max_depth = max(0, int(request.max_depth or 0))
    if max_depth and parent.depth + 1 > max_depth:
        return f"max_depth_exceeded:{max_depth}"
    max_children = max(0, int(request.max_children or 0))
    if max_children and len(parent.child_ids) + len(request.child_specs) > max_children:
        return f"max_children_exceeded:{max_children}"
    parent_skills = set(parent.allowed_skills)
    for spec in request.child_specs:
        requested = set(spec.allowed_skills)
        if requested and not requested.issubset(parent_skills):
            denied = sorted(requested - parent_skills)
            return "skill_scope_expansion_denied:" + ",".join(denied)
    return ""


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
    return applied_schedule_result(build, resolutions)


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
    role_probe_goal = hctx.scheduled_child_goal(parent, spec, write_roots=requested_write_roots)
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
    goal = hctx.scheduled_child_goal(parent, spec, write_roots=extra_write_roots)
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


# LLM: Recursive creation maps one already-authorized child spec into the same
# CreateRunParams used at the root. Description remains display-only while all
# capability and write bounds continue to come from structured parent facts.
# 函数用途: 将递归下级规格转成统一创建参数，并保留职责短标题与既有权限边界。
def _create_child_params(request: HierarchyCreateChildRequest) -> CreateRunParams:
    parent = request.parent
    spec = request.spec
    agent_name = scheduled_child_agent_name(parent, spec, sibling_index=request.sibling_index)
    return CreateRunParams(
        goal=request.goal,
        thought=spec.thought or hctx.inherited_hierarchy_thought(parent, child_goal=request.goal),
        plan=spec.plan or ["读取父级 refs", "执行当前任务", "向直接父级返回结果"],
        description=str(spec.description or "").strip()[:240],
        agent_name=agent_name,
        role=request.role,
        parent_id=parent.id,
        root_id=parent.root_id or parent.id,
        depth=parent.depth + 1,
        allowed_skills=spec.allowed_skills or list(parent.allowed_skills),
        # Role only selects which inherited capabilities survive; the shared
        # policy still intersects every candidate with the parent's run scope.
        allowed_tools=scheduled_child_tools(
            ToolPolicyRequest(
                parent_tools=list(parent.allowed_tools),
                spec=spec,
                extra_write_roots=request.extra_write_roots,
                goal=request.goal,
                role=request.role,
            )
        ),
        # owner 是任务归属的 owner 域 ID（base.py 默认取 manager.owner_id），
        # 不是 agent_name；5 月起的错填被统一授权查询门（B.4 owner 一致性）首次
        # 真实拦截——agent_name 进 owner 会让授权门把本 owner 域的任务当越权
        # 拒绝派工，且污染 local_store 归属、权限快照与 runtime 身份。
        owner=parent.owner,
        supervisor=parent.id,
        final_owner=parent.final_owner or parent.owner,
        # 历史字段只为旧账本反序列化保留；新建后代不再生成第二套机器验收清单。
        acceptance_checks=[],
        quality_contract=parent.quality_contract,
        context_manifest=child_context_manifest(parent, spec),
        context_packs=child_context_packs(parent, spec),
        extra_write_roots=request.extra_write_roots,
        attributes=hctx.inherited_hierarchy_attributes(parent, spec),
    )
