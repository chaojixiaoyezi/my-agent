# LLM: 组装原存储领域，线程本地槽仅给显式短批次使用，不创建跨线程连接池或新持久状态。
# LLM: C实例只存IO策略，callback由原global scope临时运输，不将来源或连接放进Store。
# 模块用途: 组装原存储与连接范围，C本地末写要求当次原scope。
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
# LLM: coordinator_io默认False保旧算法；True不授写权、不缓存source，schema/记录/重建须消费原scope。
# 类用途: 提供原Store入口和纯IO策略，保持正式数据与投影边界。
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
    # LLM: coordinator_io仅保存更严格IO策略，不保存source/callback；实际borrow绑定原global_write_commit_scope，默认False不进新门。
    # 函数用途: 组装原Store与连接槽，C实例每次本地末写都须当前原scope。
    def __init__(
        self,
        db_path: str | Path,
        *,
        files_dir: str | Path | None = None,
        events_path: str | Path | None = None,
        enable_fts: bool = True,
        coordinator_io: bool = False,
    ):
        self.db_path = Path(db_path)
        self.root = self.db_path.parent
        self.files_dir = Path(files_dir) if files_dir is not None else self.root / "files"
        self.events_path = Path(events_path) if events_path is not None else self.root / "events.jsonl"
        self.enable_fts = enable_fts
        self.coordinator_io = coordinator_io
        self._fts_available = False
        self._connection_local = local()
        self._init_schema()
        self.task_registry = TaskRegistry(self)

    @property
    def fts_available(self) -> bool:
        return self.enable_fts and self._fts_available


__all__ = ["LocalStore"]
