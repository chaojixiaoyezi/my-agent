# LLM: manager 组合原 canonical 服务；原创建入口透传同一待提交对象，创建、换轮和控制使用同一 owner guard，不另立身份或生命周期入口。
# 模块用途: 组装子代理状态、准备对象提交和执行服务，提供统一的短事务协调入口。
"""Subagent orchestration manager."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .coordination import subagent_creation_guard
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
from .services.base import CreateRunParams, PreparedSubagentRun, SubAgentBaseService
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
    # G4 补（3.txt G4-1）：显式执行模式。空 = 按 owner_home_dir 推断
    # （有 home → MANAGED；无 → LOCAL_UNMANAGED，兼容存量调用）。
    execution_mode: str = ""
    # 与工具循环同一把沙箱门:ShellTool.path_access_policy.owner_scope_root。
    # 验收机器执行(DONE 绑定)以它为准——effective_permissions.owner_home 是快照
    # 默认值,在无沙箱环境(如 SimpleAgent)也非空,会把验收误判成必须 bwrap。
    owner_scope_root: str = ""
    owner_policy_snapshot: dict[str, object] | None = None

# LLM: 服务共享同一 owner 路径、RuntimeDB 与 canonical 状态，协调锁只包短事务，不替代各领域权威。
# 类用途: 提供子代理创建、执行和管理入口，组装各服务而不维护第二份运行状态。
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
        execution_mode="",
        owner_scope_root="",
        owner_policy_snapshot=None,
    ):
        params = params or _init_params_from_kwargs(locals())
        _init_manager_state(self, workspace, params)
        _attach_services(self)

    # LLM: 创建、换轮、插话预留和取消共用原 owner 文件锁；同线程嵌套复用，禁止锁内等待模型、启动探测或进程退出。
    # 函数用途: 返回子代理短事务边界，避免旧停止覆盖新执行轮或漏掉正在落盘的孩子。
    def creation_guard(self):
        return subagent_creation_guard(self.workspace)

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

    # LLM: 原公开创建入口透传完整规范参数及可选的同一准备对象；身份复核、物化、权限和父链仍唯一归 base_service，不在 facade 重建任务。
    # 函数用途: 创建子代理，或提交宿主已只读准备的同一任务；职责短标题仍只用于展示。
    def create_run(
        self,
        *,
        params: CreateRunParams | None = None,
        prepared: PreparedSubagentRun | None = None,
        goal: str = "",
        thought: str = "",
        plan: list[str] | None = None,
        description: str = "",
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
        return self.base_service.create_run(params=params, prepared=prepared)

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

    # LLM: Exact-id batch reads delegate to the persistence cache and never treat the selection
    # index as lifecycle authority. Callers receive canonical task copies plus structured errors.
    # 函数用途: 按一组明确 run_id 批量读取子代理，供状态界面避免扫描全部历史任务。
    def list_runs_by_ids_report(self, run_ids):
        return self.persistence.list_runs_by_ids_report(run_ids)

    # LLM: Root-scoped reads delegate to the persistence/index adapter and return canonical task
    # copies. Scheduler callers must not fall back to parsing goal text or display tree rows.
    # 函数用途: 精确读取某个根任务的子代理，供后台完成合批和续跑判断使用。
    def list_runs_for_root_report(self, root_task_id):
        return self.persistence.list_runs_for_root_report(root_task_id)

    # LLM: Ordinary callers cannot reopen a recovery-closed run. Only an exact structured
    # same-run continuation or lifecycle repair may opt in; keep this flag narrow and never
    # infer it from text.
    # 函数用途: 保存子代理权威状态；默认保护终态，只有明确的同一任务续跑/纠错入口才允许重新运行。
    def save(
        self,
        task: SubAgentTask,
        *,
        allow_terminal_reactivation: bool = False,
    ) -> None:
        self.persistence.save(
            task,
            allow_terminal_reactivation=allow_terminal_reactivation,
        )

    # LLM: Critical lifecycle domains use persistence.mutate so reducers observe and commit one
    # exact canonical revision under the shared cross-process guard; callers must keep them short.
    # 函数用途: 原子修改一个子代理的最新权威状态，供授权、收口和控制事件避免并发丢更新。
    def mutate(
        self,
        run_id: str,
        reducer,
        *,
        expected_revision: int | None = None,
    ) -> SubAgentTask:
        return self.persistence.mutate(
            run_id,
            reducer,
            expected_revision=expected_revision,
        )

    # LLM: Preserve the persistence service's boolean lease-fence result for session-pool loops.
    # 函数用途: 保存轻量 runner 心跳，并返回该旧执行轮是否仍允许继续刷新。
    def save_runner_session(
        self,
        run_id: str,
        session: dict[str, object],
        *,
        now: float,
    ) -> bool:
        """Write the runner lease fact without invoking a full task save."""

        return self.persistence.save_runner_session(run_id, session, now=now)

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
        execution_mode=str(values.get("execution_mode") or ""),
        owner_scope_root=str(values.get("owner_scope_root") or ""),
        owner_policy_snapshot=values.get("owner_policy_snapshot"),
    )


def _create_run_params_from_kwargs(values: dict[str, object]) -> CreateRunParams:
    return CreateRunParams(
        goal=values.get("goal"),
        thought=values.get("thought"),
        plan=values.get("plan") or [],
        description=values.get("description"),
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
    _attach_runtime_db(manager)


def _attach_runtime_db(manager: SubAgentManager) -> None:
    """R1 + G4 补（3.txt G4-1）：owner 权威 runtime.db 按显式模式挂载。

    ExecutionMode.MANAGED：必须完整权威链——owner_home_dir 缺失或
    RuntimeRepository 挂载失败 → 构造即抛（fail-closed，不延迟到调用
    点、不静默降级；旧实现的 OSError→None 是把「没找到 DB」当旁路）。
    ExecutionMode.LOCAL_UNMANAGED：显式选择本地非托管（纯文件层/投影），
    create_run 只写投影，不挂权威库。
    未显式传入 → 按有无 home 推断（兼容存量调用行为）。
    """
    from ..runtime_db.execution_mode import ExecutionMode, resolve_execution_mode
    from ..runtime_db.repository import RuntimeRepository
    from ..runtime_db.schema import runtime_db_path

    manager.runtime_db = None
    home = str(getattr(manager, "owner_home_dir", "") or "").strip()
    explicit = str(getattr(manager, "execution_mode", "") or "").strip()
    mode = resolve_execution_mode(explicit, has_home_dir=bool(home))
    manager.execution_mode = mode
    if mode is ExecutionMode.LOCAL_UNMANAGED:
        return  # 显式非托管：不建权威库（纯投影）。
    if not home:
        raise ValueError(
            "ExecutionMode.MANAGED 必须有 owner_home_dir（权威库挂载点）"
        )
    # MANAGED（显式或按 home 推断）：挂载失败即抛，绝不静默降级。
    manager.runtime_db = RuntimeRepository(runtime_db_path(home))


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
    # G4 补 1：显式执行模式先落实例（_attach_runtime_db 从实例读），
    # 空字符串 = 未显式指定，由挂载逻辑按 home 推断。
    manager.execution_mode = str(params.execution_mode or "")
    manager.owner_scope_root = str(params.owner_scope_root or "")
    manager.owner_policy_snapshot = dict(params.owner_policy_snapshot or {})
