# LLM: 组装原存储领域，线程本地槽仅给显式短批次使用，不创建跨线程连接池或新持久状态。
# 模块用途: 组装本地存储并绑定每个实例的连接范围。
from __future__ import annotations

"""Composition root for LocalStore persistence, search, events, and ledgers."""

from pathlib import Path
from threading import local

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


# LLM: 每个实例保留原文件/schema归属；临时连接限调用线程，搜索、事件及账本仍沿各自领域实现。
# 类用途: 提供统一本地存储入口，不扩大批次连接的权限或事务范围。
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

    # LLM: 每个store的批次连接只绑定当前线程，初始化仍建原schema；不增加常驻数据库连接或跨线程共享。
    # 函数用途: 组装本地存储并准备短生命周期批次连接槽，原调用参数不变。
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
        self._connection_local = local()
        self._init_schema()
        self.task_registry = TaskRegistry(self)

    @property
    def fts_available(self) -> bool:
        return self.enable_fts and self._fts_available


__all__ = ["LocalStore"]
