
from __future__ import annotations

"""base task creation and lifecycle service.

这里承接子代理任务创建、分割、注册卡等基础能力。
SubAgentManager 通过当前服务组合调用这里。
运行身份会写 memory scope 和 runtime config scope，供 worker 装载 task overlay。
"""

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..effective_permissions import effective_permission_snapshot
from ..models import SubAgentTask
from .collaboration_registry import register_collaboration_agent_capability
from .inheritance_manifest import build_inheritance_manifest
from .output_ref_rebinding import rebind_task_output_refs_to_run
from .persistence.model_normalizers import (
    _normalize_context_manifest,
    _normalize_context_packs,
    _normalize_quality_contract,
)
from .runtime_config_scope import apply_config_overlay_ref, runtime_config_scope

if TYPE_CHECKING:
    from ..models import ContextManifest, QualityContract, SubAgentCard


@dataclass(frozen=True)
class CreateRunParams:
    """Bundle of create_run parameters."""

    goal: str
    thought: str
    plan: list[str]
    agent_name: str = "general"
    role: str = "general"
    parent_id: str = ""
    root_id: str = ""
    depth: int = 0
    allowed_skills: list[str] | None = None
    allowed_tools: list[str] | None = None
    owner: str = ""
    supervisor: str = ""
    final_owner: str = ""
    acceptance_checks: list[str] | None = None
    quality_contract: Any = None
    context_manifest: Any = None
    context_packs: Any = None
    extra_write_roots: list[str] | None = None
    workflow_mode: str = "off"
    normalize_role: bool = True
    attributes: dict[str, object] | None = None
    parent_access_mode: str = ""
    memory_retention_policy: str = "parent_review_or_cleanup"
    memory_delete_after_days: int = 0
    destroy_summary_required: bool = True


def _load_parent_task(manager: Any, parent_id: str):
    if not parent_id:
        return None
    try:
        return manager.load(parent_id)
    except FileNotFoundError:
        return None


def _session_identity_fields(run_id: str, parent_task: Any | None) -> dict[str, str]:
    session_id = f"session-{run_id}"
    parent_session = str(getattr(parent_task, "subagent_session_id", "") or "")
    root_session = str(getattr(parent_task, "root_subagent_session_id", "") or parent_session or session_id)
    return {
        "subagent_session_id": session_id,
        "agent_thread_id": f"thread-{run_id}",
        "parent_subagent_session_id": parent_session,
        "root_subagent_session_id": root_session,
    }


def _create_run_route_attrs(params: CreateRunParams) -> dict[str, object]:
    attrs = params.attributes if isinstance(params.attributes, dict) else {}
    return {
        "explicit_template_id": str(attrs.get("workflow_template_id") or "").strip(),
        "workflow_task_type": str(attrs.get("workflow_task_type") or "").strip(),
        "workflow_risk_tags": attrs.get("workflow_risk_tags"),
    }


def _memory_retention_policy(value: object) -> str:
    text = str(value or "").strip()
    return text or "parent_review_or_cleanup"


def _nonnegative_int(value: object) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, parsed)


def _apply_runtime_identity_and_memory_scope(task: Any, params: CreateRunParams, *, parent_task: Any | None) -> None:
    owner_id = str(task.owner or params.owner or "").strip()
    task.runtime_identity.root_run_id = task.root_id or task.id
    task.runtime_identity.service_owner_id = owner_id
    task.runtime_identity.effective_principal_id = owner_id
    task.runtime_identity.conversation_id = task.root_subagent_session_id or task.subagent_session_id
    task.runtime_identity.memory_namespace = f"subagent:{task.root_id or task.id}:{task.id}"
    task.runtime_identity.conversation_memory_policy = "task_scoped"
    task.runtime_identity.promotion_policy = "explicit_parent_review"
    apply_config_overlay_ref(task, params, parent_task=parent_task)
    task.attributes = {
        **dict(task.attributes or {}),
        "memory_scope": _memory_scope(task, params),
        "runtime_config_scope": runtime_config_scope(task),
    }


def _memory_scope(task: Any, params: CreateRunParams) -> dict[str, object]:
    return {
        "schema_version": "subagent_memory_scope.v1",
        "namespace": task.runtime_identity.memory_namespace,
        "retention_policy": _memory_retention_policy(params.memory_retention_policy),
        "delete_after_days": _nonnegative_int(params.memory_delete_after_days),
        "destroy_summary_required": bool(params.destroy_summary_required),
        "auto_promote_to_parent_memory": False,
    }


class SubAgentBaseService:
    """Base task creation and lifecycle service."""

    def __init__(self, manager: Any):
        self.manager = manager

    def split(
        self,
        goal: str,
        count: int,
        *,
        workflow_mode: str = "off",
        allowed_tools: list[str] | None = None,
    ) -> list[SubAgentTask]:
        """Split a goal into multiple subagent task records.

        Currently uses template-based splitting for simplicity.
        """
        count = max(1, count)
        tasks: list[SubAgentTask] = []
        for i in range(1, count + 1):
            task = self.create_run(
                params=CreateRunParams(
                    goal=f"{goal} / 子任务{i}",
                    thought="先缩小任务边界，明确输入、输出和验证证据，再执行。",
                    plan=["理解目标", "列出交付物", "执行最小验证", "汇报结果和证据"],
                    role="worker",
                    allowed_tools=list(allowed_tools) if allowed_tools else None,
                    extra_write_roots=[],
                    workflow_mode=workflow_mode,
                ),
            )
            tasks.append(task)
        return tasks

    def register_card(self, card: SubAgentCard) -> None:
        """Register a subagent role card."""
        self.manager.cards[card.name] = card

    def create_run(
        self,
        *,
        params: CreateRunParams,
    ) -> SubAgentTask:
        """Create a subagent task record.

        workflow_mode controls workflow planning:
          - "off" : default, no workflow
          - "plan" : run workflow planning, write result to task.workflow_plan
          - "auto" : run workflow planning, auto-merge worker spec and closeout into acceptance checklist
        """
        from ..role_contracts import apply_role_contract_to_create_params

        params = apply_role_contract_to_create_params(
            params,
            role_template_dirs=getattr(self.manager, "role_template_dirs", None),
        )
        prepared = self._prepare_run(params)
        task = self._build_task(params, prepared)
        self._finalize_task(task, params.parent_id)
        return task

    def _prepare_run(self, params: CreateRunParams) -> dict[str, object]:
        """Prepare run context: paths, workflow planning, merged acceptance checks."""
        from ..services.workflow import (
            _merge_workflow_acceptance_checks,
            _normalize_workflow_mode_value,
            _try_workflow_plan,
            _WorkflowPlanAttempt,
        )

        run_id = self.manager._new_id("subagent")
        paths = self.manager._build_work_order_paths(run_id, extra_write_roots=params.extra_write_roots)
        normalized_workflow_mode = _normalize_workflow_mode_value(params.workflow_mode)

        workflow_plan_dict: dict[str, object] | None = None
        merged_acceptance = list(params.acceptance_checks or [])
        if normalized_workflow_mode != "off":
            route_attrs = _create_run_route_attrs(params)
            workflow_plan_dict = _try_workflow_plan(
                _WorkflowPlanAttempt(
                    goal=params.goal,
                    explicit_template_id=str(route_attrs["explicit_template_id"]),
                    workflow_task_type=str(route_attrs["workflow_task_type"]),
                    workflow_risk_tags=route_attrs["workflow_risk_tags"],
                    quality_contract=params.quality_contract,
                    context_manifest=params.context_manifest,
                    allowed_write_roots=params.extra_write_roots,
                ),
            )
            merged_acceptance = _merge_workflow_acceptance_checks(merged_acceptance, workflow_plan_dict)

        return {
            "run_id": run_id,
            "paths": paths,
            "normalized_workflow_mode": normalized_workflow_mode,
            "workflow_plan_dict": workflow_plan_dict,
            "merged_acceptance": merged_acceptance,
            "now": time.time(),
        }

    def _build_task(self, params: CreateRunParams, prepared: dict[str, object]) -> SubAgentTask:
        """Build SubAgentTask from params and prepared context."""
        run_id = prepared["run_id"]
        now = prepared["now"]
        workflow_plan_dict = prepared["workflow_plan_dict"]

        parent_task = _load_parent_task(self.manager, params.parent_id)
        task = SubAgentTask(
            id=run_id,
            goal=params.goal,
            thought=params.thought,
            plan=params.plan,
            agent_name=params.agent_name,
            role=params.role,
            owner=str(params.owner or getattr(self.manager, "owner_id", "") or "").strip(),
            supervisor=params.supervisor,
            final_owner=params.final_owner,
            parent_id=params.parent_id,
            root_id=params.root_id or run_id,
            depth=params.depth,
            **_session_identity_fields(run_id, parent_task),
            allowed_skills=params.allowed_skills or [],
            allowed_tools=params.allowed_tools or [],
            acceptance_checks=prepared["merged_acceptance"],
            quality_contract=_normalize_quality_contract(params.quality_contract),
            context_manifest=_normalize_context_manifest(params.context_manifest),
            context_packs=_normalize_context_packs(params.context_packs),
            effective_permissions=effective_permission_snapshot(
                parent_task=parent_task,
                parent_access_mode=params.parent_access_mode,
                owner_policy=getattr(self.manager, "owner_policy_snapshot", {}),
            ),
            created_at=now,
            updated_at=now,
            heartbeat_at=now,
            workflow_mode=prepared["normalized_workflow_mode"],
            workflow_template_id=str((workflow_plan_dict or {}).get("selected_template_id") or ""),
            workflow_plan=workflow_plan_dict or {},
            attributes=dict(params.attributes or {}),
            **prepared["paths"],
        )
        _apply_runtime_identity_and_memory_scope(task, params, parent_task=parent_task)
        task.inheritance_manifest = build_inheritance_manifest(parent_task, task)
        rebind_task_output_refs_to_run(task)
        return task

    def _finalize_task(self, task: SubAgentTask, parent_id: str) -> None:
        """Save task, register in local store, and link to parent if needed."""
        from ..debug_trace import trace_task_created

        self.manager.save(task)
        register_collaboration_agent_capability(self.manager, task)
        trace_task_created(self.manager, task)
        if self.manager.local_store:
            self.manager.local_store.task_registry.register_task(
                task_id=task.id,
                session_id=task.root_id,
                user_id=task.owner or "",
                status=task.status,
                goal=task.goal,
            )
        if parent_id:
            self.manager.add_child(parent_id, task.id)

    def record_takeover(
        self,
        run_id: str,
        *,
        take_over_by: str,
        reason: str,
        locked_files: list[str] | None = None,
    ) -> TakeoverRecord:
        """Record a takeover and write TAKEOVER.md.

        This does not actually kill the subagent process, but records ownership and lock files.
        """
        from ..models import TakeoverRecord
        from ..utils import _merge_list

        task = self.manager.load(run_id)
        record = TakeoverRecord(
            id=self.manager._new_id("takeover"),
            run_id=run_id,
            take_over_by=take_over_by,
            reason=reason,
            locked_files=locked_files or [],
            previous_owner=task.owner,
            created_at=time.time(),
        )
        task.takeover_records.append(record)
        task.takeover_by = take_over_by
        task.takeover_reason = reason
        task.locked_files = _merge_list(task.locked_files, record.locked_files)
        task.final_owner = take_over_by
        task.status = "TAKEN_OVER"
        task.updated_at = time.time()
        attrs = dict(getattr(task, "attributes", {}) or {})
        runtime_scope = dict(attrs.get("runtime_config_scope") or runtime_config_scope(task))
        runtime_scope["takeover_by"] = take_over_by
        runtime_scope["takeover_record_id"] = record.id
        runtime_scope["loaded_as"] = "task_layer_after_takeover" if runtime_scope.get("overlay_ref") else "base_config"
        attrs["runtime_config_scope"] = runtime_scope
        task.attributes = attrs
        self.manager.save(task)
        self.manager._write_takeover_file(task, record)
        return record
