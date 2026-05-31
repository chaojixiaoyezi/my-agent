# LLM: Subagent orchestration module; keep task workspace, manager facade, and report contracts stable.
# 模块用途: 支撑主代理派发、跟踪、验收、汇总子代理任务。

from __future__ import annotations

"""base task creation and lifecycle service.

给人看的解释：
这里承接子代理任务创建、分割、注册卡等基础能力。
SubAgentManager 通过 facade 方法委托到这里。
"""

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..effective_permissions import effective_permission_snapshot
from ..models import SubAgentTask
from .collaboration_registry import register_collaboration_agent_capability
from .inheritance_manifest import build_inheritance_manifest
from .output_ref_rebinding import rebind_task_output_refs_to_run
from .persistence_model_normalizers import (
    _normalize_context_manifest,
    _normalize_context_packs,
    _normalize_quality_contract,
)

if TYPE_CHECKING:
    from ..models import ContextManifest, QualityContract, SubAgentCard


# LLM: CreateRunParams 属于子代理服务层的类边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 类用途: 集中保存createrun参数字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
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


# LLM: _load_parent_task keeps inheritance manifest creation best-effort and non-blocking.
# 函数用途: 读取父级任务快照，失败时返回 None，避免创建 child 因旧工单缺失而中断。
def _load_parent_task(manager: Any, parent_id: str):
    if not parent_id:
        return None
    try:
        return manager.load(parent_id)
    except FileNotFoundError:
        return None


# LLM: _session_identity_fields creates stable session/thread ids above concrete run attempts.
# 函数用途: 为新 run 生成独立会话身份，并把 parent/root session refs 传下去；旧记录缺字段时按 run_id 兼容。
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


# LLM: _create_run_route_attrs exposes workflow route machine facts without parsing the user goal.
# 函数用途: 从 CreateRunParams.attributes 提取 workflow 模板选择字段；普通 goal/thought 文本不作为机器事实来源。
def _create_run_route_attrs(params: CreateRunParams) -> dict[str, object]:
    attrs = params.attributes if isinstance(params.attributes, dict) else {}
    return {
        "explicit_template_id": str(attrs.get("workflow_template_id") or "").strip(),
        "workflow_task_type": str(attrs.get("workflow_task_type") or "").strip(),
        "workflow_risk_tags": attrs.get("workflow_risk_tags"),
    }


# LLM: _memory_retention_policy keeps retention policy open-world and non-blocking.
# 函数用途: 读取配置/创建参数里的策略名，空值回退默认策略。
def _memory_retention_policy(value: object) -> str:
    text = str(value or "").strip()
    return text or "parent_review_or_cleanup"


# LLM: _nonnegative_int normalizes optional retention day counts.
# 函数用途: 将配置/参数转成非负整数，无法解析时返回 0。
def _nonnegative_int(value: object) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, parsed)


# LLM: _apply_runtime_identity_and_memory_scope writes system-derived run identity facts.
# 函数用途: 给子代理设置 root/conversation/memory namespace，并写入 memory_scope 账本。
def _apply_runtime_identity_and_memory_scope(task: Any, params: CreateRunParams) -> None:
    # LLM: runtime identity records who owns the child run without promoting child memory globally.
    owner_id = str(task.owner or params.owner or "").strip()
    task.runtime_identity.root_run_id = task.root_id or task.id
    task.runtime_identity.service_owner_id = owner_id
    task.runtime_identity.effective_principal_id = owner_id
    task.runtime_identity.conversation_id = task.root_subagent_session_id or task.subagent_session_id
    task.runtime_identity.memory_namespace = f"subagent:{task.root_id or task.id}:{task.id}"
    task.runtime_identity.conversation_memory_policy = "task_scoped"
    task.runtime_identity.promotion_policy = "explicit_parent_review"
    task.attributes = {
        **dict(task.attributes or {}),
        "memory_scope": _memory_scope(task, params),
    }


# LLM: _memory_scope is the task-local memory retention record for one subagent.
# 函数用途: 生成子代理任务级记忆策略，不授予长期记忆写入权限。
def _memory_scope(task: Any, params: CreateRunParams) -> dict[str, object]:
    return {
        "schema_version": "subagent_memory_scope.v1",
        "namespace": task.runtime_identity.memory_namespace,
        "retention_policy": _memory_retention_policy(params.memory_retention_policy),
        "delete_after_days": _nonnegative_int(params.memory_delete_after_days),
        "destroy_summary_required": bool(params.destroy_summary_required),
        "auto_promote_to_parent_memory": False,
    }


# LLM: SubAgentBaseService 属于子代理服务层的类边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 类用途: 封装subagent基础服务操作，把状态读写和错误处理收束在服务层；关键副作用: 方法可能触发任务状态、报告记录和持久化副作用相关副作用，需保持公开契约稳定。
class SubAgentBaseService:
    """Base task creation and lifecycle service."""

    # LLM: __init__ 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
    # 函数用途: 初始化实例依赖和配置字段，为后续方法调用准备共享状态；关键副作用: 需保持任务状态、报告记录和持久化副作用上的返回值和副作用边界稳定。
    def __init__(self, manager: Any):
        self.manager = manager

    # LLM: split owns template child creation and leaves empty allowed_tools to role inference.
    # 函数用途: 按数量创建模板子任务；有显式工具才传入，空值交给 worker 角色模板和任务推断。
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

    # LLM: register_card 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
    # 函数用途: 处理registercard相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、报告记录和持久化副作用上的返回值和副作用边界稳定。
    def register_card(self, card: SubAgentCard) -> None:
        """Register a subagent role card."""
        self.manager.cards[card.name] = card

    # LLM: create_run 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
    # 函数用途: 构建createrun所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 会影响任务状态、报告记录和持久化副作用，需保持重试、超时和状态迁移语义。
    def create_run(
        self,
        *,
        params: CreateRunParams,
    ) -> SubAgentTask:
        """Create a subagent task record.

        workflow_mode controls workflow planning:
          - "off" : default, no workflow (backward compatible)
          - "plan" : run workflow planning, write result to task.workflow_plan
          - "auto" : run workflow planning, auto-merge worker spec and closeout into acceptance checklist
        """
        # LLM: role contracts normalize reporter/checker semantics before workflow planning and persistence.
        from ..role_contracts import apply_role_contract_to_create_params

        params = apply_role_contract_to_create_params(
            params,
            role_template_dirs=getattr(self.manager, "role_template_dirs", None),
        )
        prepared = self._prepare_run(params)
        task = self._build_task(params, prepared)
        self._finalize_task(task, params.parent_id)
        return task

    # LLM: _prepare_run 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
    # 函数用途: 处理preparerun相关的数据流，连接当前职责的前后步骤；关键副作用: 会影响任务状态、报告记录和持久化副作用，需保持重试、超时和状态迁移语义。
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

    # LLM: _build_task 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
    # 函数用途: 构建任务所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 主要返回派生结构或文本，需保持字段名、顺序和空值处理稳定。
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
        _apply_runtime_identity_and_memory_scope(task, params)
        task.inheritance_manifest = build_inheritance_manifest(parent_task, task)
        # LLM: Bind self-output refs after run_id exists so models cannot persist guessed sibling ids.
        rebind_task_output_refs_to_run(task)
        return task

    # LLM: _finalize_task 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
    # 函数用途: 处理finalize任务相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、报告记录和持久化副作用上的返回值和副作用边界稳定。
    def _finalize_task(self, task: SubAgentTask, parent_id: str) -> None:
        """Save task, register in local store, and link to parent if needed."""
        from ..debug_trace import trace_task_created

        self.manager.save(task)
        register_collaboration_agent_capability(self.manager, task)
        # LLM: trace_task_created is gated by subagent_debug_trace_level and writes only internal refs.
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

    # LLM: record_takeover 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
    # 函数用途: 写入takeover的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动任务状态、报告记录和持久化副作用，调用方依赖写入顺序和文件格式。
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
        self.manager.save(task)
        self.manager._write_takeover_file(task, record)
        return record
