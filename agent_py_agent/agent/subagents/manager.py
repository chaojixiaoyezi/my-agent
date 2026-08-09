
"""Subagent orchestration manager."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .kernel import SubagentKernelMixin
from .manager_work_orders import (
    build_work_order_paths,
    ensure_work_order_files,
    validate_work_order,
    write_takeover_file,
)
from .models import SubAgentCard, SubAgentTask, TakeoverRecord, WorkOrderValidation
from .patch.patch_service import SubAgentPatchService
from .services.actions import SubAgentActionService
from .services.base import CreateRunParams, SubAgentBaseService
from .services.board.service import SubAgentBoardService
from .services.budget import SubAgentBudgetService
from .services.capability_service import SubAgentCapabilityService
from .services.channel_probe import SubAgentChannelProbeService
from .services.dispatch import SubAgentDispatchService, SubAgentParentPlannerService
from .services.hierarchy.service import SubAgentHierarchyService
from .services.indexing import SubAgentIndexingService
from .services.indexing.records import LocalRecordParams
from .services.lifecycle import SubAgentLifecycleService
from .services.memory_candidates import SubAgentMemoryCandidateService
from .services.persistence import SubAgentPersistenceService
from .services.runner_context_service import SubAgentRunnerContextService
from .services.runner_result_service import SubAgentRunnerResultService
from .services.takeover.run import SubAgentTakeoverRunService
from .utils import _new_id

if TYPE_CHECKING:
    from ..local_storage import LocalStore


@dataclass(frozen=True)
class SubAgentManagerInitParams:
    local_store: LocalStore | None = None
    # 协作账本依赖；为空时子代理仍按普通无协作上下文运行。
    collaboration_store: Any | None = None
    # 长期会话账本依赖；为空时只更新子代理树，不触发父代理后台唤醒。
    conversation_store: Any | None = None
    workspace_root: str | Path | None = None
    workspace_roots: list[str | Path] | None = None
    role_template_dirs: list[str | Path] | None = None
    # LLM: The owner CandidateService is injected by the composition root; a child manager cannot create another ledger.
    # 字段用途: 让子代理结果只投递到当前 owner 的统一 Memory 候选账本。
    candidate_service: object | None = None
    debug_trace_level: int = 0
    takeover_chain_max_depth: int = 0
    owner_id: str = ""
    owner_home_dir: str = ""
    # 与工具循环同一把沙箱门:ShellTool.path_access_policy.owner_scope_root。
    # 验收机器执行(DONE 绑定)以它为准——effective_permissions.owner_home 是快照
    # 默认值,在无沙箱环境(如 SimpleAgent)也非空,会把验收误判成必须 bwrap。
    owner_scope_root: str = ""
    owner_policy_snapshot: dict[str, object] | None = None

class SubAgentManager(SubagentKernelMixin):
    """Coordinate focused subagent services behind one manager."""

    def __init__(
        self,
        workspace,
        *,
        params: SubAgentManagerInitParams | None = None,
        local_store=None,
        collaboration_store=None,
        conversation_store=None,
        workspace_root=None,
        workspace_roots=None,
        role_template_dirs=None,
        candidate_service=None,
        debug_trace_level=0,
        takeover_chain_max_depth=0,
        owner_id="",
        owner_home_dir="",
        owner_scope_root="",
        owner_policy_snapshot=None,
    ):
        params = params or _init_params_from_kwargs(locals())
        _init_manager_state(self, workspace, params)
        _attach_services(self)

    @property
    def _indexing_service(self):
        return self.indexing


    def _log_local_record(
        self,
        *,
        params: LocalRecordParams | None = None,
        source_type: str = "",
        source_id: str = "",
        title: str = "",
        content: str = "",
        event_type: str = "",
        metadata: dict[str, object] | None = None,
    ):
        params = params or LocalRecordParams(
            source_type=source_type,
            source_id=source_id,
            title=title,
            content=content,
            event_type=event_type,
            metadata=metadata,
        )
        return self.indexing.log_local_record(params=params)

    def log_local_record(
        self,
        *,
        params: LocalRecordParams | None = None,
        source_type: str = "",
        source_id: str = "",
        title: str = "",
        content: str = "",
        event_type: str = "",
        metadata: dict[str, object] | None = None,
    ):
        params = params or LocalRecordParams(
            source_type=source_type,
            source_id=source_id,
            title=title,
            content=content,
            event_type=event_type,
            metadata=metadata,
        )
        return self.indexing.log_local_record(params=params)

    def split(
        self,
        goal: str,
        count: int,
        *,
        allowed_tools: list[str] | None = None,
    ) -> list[SubAgentTask]:
        return self.base_service.split(
            goal,
            count,
            allowed_tools=allowed_tools,
        )

    def register_card(self, card: SubAgentCard) -> None:
        self.cards[card.name] = card

    def create_run(
        self,
        *,
        params: CreateRunParams | None = None,
        goal: str = "",
        thought: str = "",
        plan: list[str] | None = None,
        agent_name: str = "general",
        role: str = "general",
        parent_id: str = "",
        root_id: str = "",
        depth: int = 0,
        allowed_skills: list[str] | None = None,
        allowed_tools: list[str] | None = None,
        owner: str = "",
        supervisor: str = "",
        final_owner: str = "",
        acceptance_checks: list[str] | None = None,
        quality_contract: Any = None,
        context_manifest: Any = None,
        context_packs: Any = None,
        extra_write_roots: list[str] | None = None,
        attributes: dict[str, object] | None = None,
        parent_access_mode: str = "",
        memory_retention_policy: str = "parent_review_or_cleanup",
        memory_delete_after_days: int = 0,
        destroy_summary_required: bool = True,
    ) -> SubAgentTask:
        params = params or _create_run_params_from_kwargs(locals())
        return self.base_service.create_run(params=params)

    def record_takeover(
        self,
        run_id: str,
        *,
        take_over_by: str,
        reason: str,
        locked_files: list[str] | None = None,
    ):
        return self.base_service.record_takeover(
            run_id,
            take_over_by=take_over_by,
            reason=reason,
            locked_files=locked_files,
        )

    def create_takeover_run(self, params):
        return SubAgentTakeoverRunService(self).create(params)

    def load(self, run_id: str) -> SubAgentTask:
        return self.persistence.load(run_id)

    def list_runs(self) -> list[SubAgentTask]:
        return self.persistence.list_runs()

    def list_runs_report(self):
        return self.persistence.list_runs_report()

    def save(self, task: SubAgentTask) -> None:
        self.persistence.save(task)

    def save_runner_session(
        self,
        run_id: str,
        session: dict[str, object],
        *,
        now: float,
    ) -> None:
        """Write the runner lease fact without invoking a full task save."""

        self.persistence.save_runner_session(run_id, session, now=now)

    def save_hierarchy_links(self, task: SubAgentTask) -> None:
        self.persistence.save(task, preserve_child_links=False)

    def add_child(self, parent_id: str, child_id: str) -> None:
        try:
            parent = self.load(parent_id)
        except FileNotFoundError:
            return
        if child_id not in parent.child_ids:
            parent.child_ids.append(child_id)
            parent.updated_at = time.time()
            self.save(parent)

    def _build_work_order_paths(
        self,
        run_id: str,
        task_dir: str | Path | None = None,
        extra_write_roots: list[str] | None = None,
    ) -> dict[str, object]:
        return build_work_order_paths(self, run_id, task_dir, extra_write_roots)

    def _ensure_work_order_files(self, task: SubAgentTask) -> None:
        ensure_work_order_files(task)

    def _write_takeover_file(self, task: SubAgentTask, record: TakeoverRecord) -> None:
        write_takeover_file(task, record)

    def validate_work_order(self, run_id: str) -> WorkOrderValidation:
        return validate_work_order(self, run_id)

    def _new_id(self, prefix: str) -> str:
        return _new_id(prefix)


def _init_params_from_kwargs(values: dict[str, object]) -> SubAgentManagerInitParams:
    return SubAgentManagerInitParams(
        local_store=values.get("local_store"),
        collaboration_store=values.get("collaboration_store"),
        conversation_store=values.get("conversation_store"),
        workspace_root=values.get("workspace_root"),
        workspace_roots=values.get("workspace_roots"),
        role_template_dirs=values.get("role_template_dirs"),
        candidate_service=values.get("candidate_service"),
        debug_trace_level=int(values.get("debug_trace_level") or 0),
        takeover_chain_max_depth=int(values.get("takeover_chain_max_depth") or 0),
        owner_id=str(values.get("owner_id") or ""),
        owner_home_dir=str(values.get("owner_home_dir") or ""),
        owner_scope_root=str(values.get("owner_scope_root") or ""),
        owner_policy_snapshot=values.get("owner_policy_snapshot"),
    )


def _create_run_params_from_kwargs(values: dict[str, object]) -> CreateRunParams:
    return CreateRunParams(
        goal=values.get("goal"),
        thought=values.get("thought"),
        plan=values.get("plan") or [],
        agent_name=values.get("agent_name"),
        role=values.get("role"),
        parent_id=values.get("parent_id"),
        root_id=values.get("root_id"),
        depth=values.get("depth"),
        allowed_skills=values.get("allowed_skills"),
        allowed_tools=values.get("allowed_tools"),
        owner=values.get("owner"),
        supervisor=values.get("supervisor"),
        final_owner=values.get("final_owner"),
        acceptance_checks=values.get("acceptance_checks"),
        quality_contract=values.get("quality_contract"),
        context_manifest=values.get("context_manifest"),
        context_packs=values.get("context_packs"),
        extra_write_roots=values.get("extra_write_roots"),
        attributes=values.get("attributes"),
        parent_access_mode=values.get("parent_access_mode"),
        memory_retention_policy=values.get("memory_retention_policy"),
        memory_delete_after_days=values.get("memory_delete_after_days"),
        destroy_summary_required=values.get("destroy_summary_required"),
    )


# LLM: Learning and memory_gate services were removed; this adapter is the only subagent-to-Memory write seam.
# 函数用途: 装配子代理服务，并把 lesson/finding 统一接到 owner CandidateService。
def _attach_services(manager: SubAgentManager) -> None:
    manager.lifecycle = SubAgentLifecycleService(manager)
    manager.persistence = SubAgentPersistenceService(manager)
    manager.base_service = SubAgentBaseService(manager)
    manager.actions = SubAgentActionService(manager)
    manager.board = SubAgentBoardService(manager)
    manager.budget = SubAgentBudgetService(manager)
    manager.capability = SubAgentCapabilityService(manager)
    manager.channel_probe = SubAgentChannelProbeService(manager)
    manager.dispatch = SubAgentDispatchService(manager)
    manager.hierarchy = SubAgentHierarchyService(manager)
    manager.indexing = SubAgentIndexingService(manager)
    manager.memory_candidates = SubAgentMemoryCandidateService(manager)
    manager.patch = SubAgentPatchService(manager)
    manager.parent_planner = SubAgentParentPlannerService(manager)
    manager.runner_context = SubAgentRunnerContextService(manager)
    manager.runner_result = SubAgentRunnerResultService(manager)


def _init_manager_state(manager: SubAgentManager, workspace: str | Path, params: SubAgentManagerInitParams) -> None:
    manager.workspace = Path(workspace)
    manager.workspace.mkdir(parents=True, exist_ok=True)
    manager.cards: dict[str, SubAgentCard] = {}
    manager.local_store = params.local_store
    manager.collaboration_store = params.collaboration_store
    manager.conversation_store = params.conversation_store
    manager.workspace_root = (
        Path(params.workspace_root).resolve()
        if params.workspace_root
        else manager.workspace.resolve().parent
    )
    manager.workspace_roots = _normalized_workspace_roots(manager.workspace_root, params.workspace_roots)
    manager.role_template_dirs = _normalized_template_dirs(manager.workspace_root, params.role_template_dirs)
    manager.candidate_service = params.candidate_service
    manager.debug_trace_level = _normalize_debug_trace_level(params.debug_trace_level)
    manager.takeover_chain_max_depth = max(0, int(params.takeover_chain_max_depth or 0))
    _apply_owner_scope(manager, params)


def _normalized_workspace_roots(primary: Path, roots: list[str | Path] | None) -> list[Path]:
    resolved: list[Path] = []
    for raw in [primary, *(roots or [])]:
        path = Path(raw).resolve()
        if path not in resolved:
            resolved.append(path)
    return resolved


def _normalized_template_dirs(primary: Path, dirs: list[str | Path] | None) -> list[Path]:
    raw_dirs = dirs if dirs else [primary / ".agent" / "subagents" / "roles"]
    resolved: list[Path] = []
    for raw in raw_dirs:
        path = Path(raw)
        if not path.is_absolute():
            path = primary / path
        path = path.resolve()
        if path not in resolved:
            resolved.append(path)
    return resolved


def _normalize_debug_trace_level(value: object) -> int:
    try:
        level = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, min(5, level))


def _apply_owner_scope(manager: SubAgentManager, params: SubAgentManagerInitParams) -> None:
    manager.owner_id = str(params.owner_id or "")
    manager.owner_home_dir = str(params.owner_home_dir or "")
    manager.owner_scope_root = str(params.owner_scope_root or "")
    manager.owner_policy_snapshot = dict(params.owner_policy_snapshot or {})
