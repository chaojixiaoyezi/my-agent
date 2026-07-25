from __future__ import annotations

"""Composition root for LocalStore persistence, search, events, and ledgers."""

from pathlib import Path

from ..task_registry import TaskRegistry
from .control_plane import LocalStoreControlPlaneMixin
from .control_plane_panel import LocalStoreSharedProgressPanelMixin
from .events import LocalStoreEventMixin
from .ledger_redaction import LocalStoreLedgerRedactionMixin
from .maintenance import LocalStoreMaintenanceMixin
from .records import LocalStoreRecordMixin
from .runtime_gate_ledger import LocalStoreRuntimeGateLedgerMixin
from .schema import LocalStoreSchemaMixin
from .search import LocalStoreSearchMixin
from .tool_operations import LocalStoreToolOperationMixin


class LocalStore(
    LocalStoreSchemaMixin,
    LocalStoreRecordMixin,
    LocalStoreSearchMixin,
    LocalStoreEventMixin,
    LocalStoreControlPlaneMixin,
    LocalStoreSharedProgressPanelMixin,
    LocalStoreRuntimeGateLedgerMixin,
    LocalStoreToolOperationMixin,
    LocalStoreLedgerRedactionMixin,
    LocalStoreMaintenanceMixin,
):
    """Composes the local durable store APIs used by the runtime."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        files_dir: str | Path | None = None,
        events_path: str | Path | None = None,
        enable_fts: bool = True,
    ):
        self.db_path = Path(db_path)
        self.root = self.db_path.parent
        self.files_dir = Path(files_dir) if files_dir is not None else self.root / "files"
        self.events_path = Path(events_path) if events_path is not None else self.root / "events.jsonl"
        self.enable_fts = enable_fts
        self._fts_available = False
        self._init_schema()
        self.task_registry = TaskRegistry(self)

    @property
    def fts_available(self) -> bool:
        return self.enable_fts and self._fts_available


__all__ = ["LocalStore"]
