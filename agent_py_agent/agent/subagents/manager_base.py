from __future__ import annotations

"""LLM: SubAgentBaseMixin facade for core lifecycle and work-order operations.

Work-order path/file helpers live in manager_work_orders.py to keep this mixin small.
"""

import time
from pathlib import Path
from typing import TYPE_CHECKING

from .manager_work_orders import (
    build_work_order_paths,
    ensure_work_order_files,
    validate_work_order,
    write_takeover_file,
)
from .models import SubAgentCard, SubAgentTask, TakeoverRecord, WorkOrderValidation
from .services.base import CreateRunParams
from .utils import _new_id

if TYPE_CHECKING:
    from ..local_store import LocalStore


class SubAgentBaseMixin:
    """Facade delegating core task lifecycle to services."""

    def __init__(
        self,
        workspace: str | Path,
        local_store: LocalStore | None = None,
        workspace_root: str | Path | None = None,
        workspace_roots: list[str | Path] | None = None,
        enable_self_learning: bool = False,
    ):
        self.workspace = Path(workspace)
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.cards: dict[str, SubAgentCard] = {}
        self.local_store = local_store
        self.workspace_root = Path(workspace_root).resolve() if workspace_root else self.workspace.resolve().parent
        self.workspace_roots = _normalized_workspace_roots(self.workspace_root, workspace_roots)
        self.enable_self_learning = bool(enable_self_learning)

        from .services.base import SubAgentBaseService
        from .services.lifecycle import SubAgentLifecycleService
        from .services.persistence import SubAgentPersistenceService

        self.lifecycle = SubAgentLifecycleService(self)
        self.persistence = SubAgentPersistenceService(self)
        self.base_service = SubAgentBaseService(self)

    def split(self, goal: str, count: int, *, workflow_mode: str = "off") -> list[SubAgentTask]:
        return self.base_service.split(goal, count, workflow_mode=workflow_mode)

    def register_card(self, card: SubAgentCard) -> None:
        self.cards[card.name] = card

    def create_run(self, **kwargs) -> SubAgentTask:
        if "params" in kwargs:
            return self.base_service.create_run(params=kwargs["params"])
        return self.base_service.create_run(params=CreateRunParams(**kwargs))

    def record_takeover(
        self,
        run_id: str,
        *,
        take_over_by: str,
        reason: str,
        locked_files: list[str] | None = None,
    ):
        return self.base_service.record_takeover(run_id, take_over_by=take_over_by, reason=reason, locked_files=locked_files)

    def load(self, run_id: str) -> SubAgentTask:
        return self.persistence.load(run_id)

    def list_runs(self) -> list[SubAgentTask]:
        return self.persistence.list_runs()

    def save(self, task: SubAgentTask) -> None:
        self.persistence.save(task)

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


from .services.base import _extract_write_dirs
from .services.persistence import (
    _field_names,
    _list_value,
    _normalize_context_manifest,
    _normalize_context_packs,
    _normalize_quality_contract,
    _string_list_value,
)
from .services.workflow import (
    _CODING_SUBAGENT_TOOLS,
    _READ_ONLY_SUBAGENT_TOOLS,
    _WORKFLOW_MODES,
    _normalize_workflow_mode_value,
    _workflow_worker_tools,
)
