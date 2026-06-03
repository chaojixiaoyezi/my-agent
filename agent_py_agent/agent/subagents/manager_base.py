
from __future__ import annotations

"""SubAgentBaseMixin facade for core lifecycle and work-order operations.

Work-order path/file helpers live in manager_work_orders.py to keep this mixin small.
"""

import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .manager_work_orders import (
    build_work_order_paths,
    ensure_work_order_files,
    validate_work_order,
    write_takeover_file,
)
from .models import SubAgentCard, SubAgentTask, TakeoverRecord, WorkOrderValidation
from .services.base import CreateRunParams
from .services.takeover.run import SubAgentTakeoverRunService
from .utils import _new_id

if TYPE_CHECKING:
    from ..local_store import LocalStore


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
    enable_self_learning: bool = False
    debug_trace_level: int = 0
    takeover_chain_max_depth: int = 0
    closeout_for_all_task_nodes: bool = False
    owner_id: str = ""
    owner_home_dir: str = ""
    owner_policy_snapshot: dict[str, object] | None = None


class SubAgentBaseMixin:
    """Facade delegating core task lifecycle to services."""

    def __init__(
        self,
        workspace: str | Path,
        *,
        params: SubAgentManagerInitParams | None = None,
        local_store: LocalStore | None = None,
        collaboration_store: Any | None = None,
        conversation_store: Any | None = None,
        workspace_root: str | Path | None = None,
        workspace_roots: list[str | Path] | None = None,
        role_template_dirs: list[str | Path] | None = None,
        enable_self_learning: bool = False,
        closeout_for_all_task_nodes: bool = False,
    ):
        params = params or SubAgentManagerInitParams(
            local_store=local_store,
            collaboration_store=collaboration_store,
            conversation_store=conversation_store,
            workspace_root=workspace_root,
            workspace_roots=workspace_roots,
            role_template_dirs=role_template_dirs,
            enable_self_learning=enable_self_learning,
            closeout_for_all_task_nodes=closeout_for_all_task_nodes,
        )
        self.workspace = Path(workspace)
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.cards: dict[str, SubAgentCard] = {}
        self.local_store = params.local_store
        self.collaboration_store = params.collaboration_store
        self.conversation_store = params.conversation_store
        self.workspace_root = Path(params.workspace_root).resolve() if params.workspace_root else self.workspace.resolve().parent
        self.workspace_roots = _normalized_workspace_roots(self.workspace_root, params.workspace_roots)
        self.role_template_dirs = _normalized_template_dirs(self.workspace_root, params.role_template_dirs)
        self.enable_self_learning = bool(params.enable_self_learning)
        self.debug_trace_level = _normalize_debug_trace_level(params.debug_trace_level)
        self.takeover_chain_max_depth = max(0, int(params.takeover_chain_max_depth or 0))
        self.closeout_for_all_task_nodes = bool(params.closeout_for_all_task_nodes)
        _apply_owner_scope(self, params)

        from .services.base import SubAgentBaseService
        from .services.lifecycle import SubAgentLifecycleService
        from .services.persistence import SubAgentPersistenceService

        self.lifecycle = SubAgentLifecycleService(self)
        self.persistence = SubAgentPersistenceService(self)
        self.base_service = SubAgentBaseService(self)

    def split(
        self,
        goal: str,
        count: int,
        *,
        workflow_mode: str = "off",
        allowed_tools: list[str] | None = None,
    ) -> list[SubAgentTask]:
        return self.base_service.split(
            goal,
            count,
            workflow_mode=workflow_mode,
            allowed_tools=allowed_tools,
        )

    def register_card(self, card: SubAgentCard) -> None:
        self.cards[card.name] = card

    def create_run(
        self,
        *,
        params: CreateRunParams | None = None,
        goal: str = "", thought: str = "", plan: list[str] | None = None,
        agent_name: str = "general", role: str = "general",
        parent_id: str = "", root_id: str = "", depth: int = 0,
        allowed_skills: list[str] | None = None, allowed_tools: list[str] | None = None,
        owner: str = "", supervisor: str = "", final_owner: str = "",
        acceptance_checks: list[str] | None = None, quality_contract: Any = None,
        context_manifest: Any = None, context_packs: Any = None,
        extra_write_roots: list[str] | None = None, workflow_mode: str = "off",
        attributes: dict[str, object] | None = None, parent_access_mode: str = "",
        memory_retention_policy: str = "parent_review_or_cleanup",
        memory_delete_after_days: int = 0,
        destroy_summary_required: bool = True,
    ) -> SubAgentTask:
        params = params or CreateRunParams(
            goal=goal, thought=thought, plan=plan or [], agent_name=agent_name, role=role,
            parent_id=parent_id, root_id=root_id, depth=depth,
            allowed_skills=allowed_skills, allowed_tools=allowed_tools,
            owner=owner, supervisor=supervisor, final_owner=final_owner,
            acceptance_checks=acceptance_checks, quality_contract=quality_contract,
            context_manifest=context_manifest, context_packs=context_packs,
            extra_write_roots=extra_write_roots, workflow_mode=workflow_mode, attributes=attributes,
            parent_access_mode=parent_access_mode,
            memory_retention_policy=memory_retention_policy,
            memory_delete_after_days=memory_delete_after_days,
            destroy_summary_required=destroy_summary_required,
        )
        return self.base_service.create_run(params=params)

    def record_takeover(
        self,
        run_id: str,
        *,
        take_over_by: str,
        reason: str,
        locked_files: list[str] | None = None,
    ):
        return self.base_service.record_takeover(run_id, take_over_by=take_over_by, reason=reason, locked_files=locked_files)

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


def _normalized_workspace_roots(primary: Path, roots: list[str | Path] | None) -> list[Path]:
    resolved: list[Path] = []
    for raw in [primary, *(roots or [])]:
        path = Path(raw).resolve()
        if path not in resolved:
            resolved.append(path)
    return resolved


def _apply_owner_scope(manager, params: SubAgentManagerInitParams) -> None:
    manager.owner_id = str(params.owner_id or "")
    manager.owner_home_dir = str(params.owner_home_dir or "")
    manager.owner_policy_snapshot = dict(params.owner_policy_snapshot or {})


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


from .services.persistence.model_normalizers import (
    _field_names,
    _list_value,
    _normalize_context_manifest,
    _normalize_context_packs,
    _normalize_quality_contract,
    _string_list_value,
)
from .services.workflow import (
    _WORKFLOW_MODES,
    _normalize_workflow_mode_value,
    _workflow_worker_tools,
)
