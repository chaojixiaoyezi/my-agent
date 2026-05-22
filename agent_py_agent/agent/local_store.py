# LLM: 这里保留旧主类导入路径，真实行为应留在 local_storage 拆分模块。
# 模块用途: LocalStore 兼容门面，组合 schema、records、search、events 和 maintenance mixin。

from __future__ import annotations

"""compatibility facade and composition root for the split LocalStore implementation.

给人看的解释：
LocalStore 的真实能力已经按 schema、records、search、events、maintenance 拆到 `local_storage/`。
这个文件只负责把这些 mixin 组合成原来的 `LocalStore` 主类，老导入路径继续可用。
"""

from pathlib import Path

from .local_storage import (
    PREVIEW_CHARS,
    AgentEventInput,
    AgentEventRecord,
    AgentRunRecord,
    AgentRuntimeQueryContext,
    AgentRuntimeQueryResult,
    AgentTreeReport,
    LocalSearchResult,
    LocalStoreControlPlaneMixin,
    LocalStoreEvent,
    LocalStoreEventMixin,
    LocalStoreMaintenanceMixin,
    LocalStoreRecordMixin,
    LocalStoreRuntimeGateLedgerMixin,
    LocalStoreSchemaMixin,
    LocalStoreSearchMixin,
    LocalStoreSharedProgressPanelMixin,
    LocalTimelineItem,
    RuntimeGateLedgerRecord,
    SharedProgressPanel,
    TaskRollupRecord,
)
from .task_registry import TaskRegistry


# LLM: LocalStore 属于 LocalStore 本地事实索引 的稳定结构；调整字段或继承关系前先核对序列化、导入和测试。
# 类用途: LocalStore 主门面，组合数据库 schema、记录读写、搜索、事件和维护能力。
class LocalStore(
    LocalStoreSchemaMixin,
    LocalStoreRecordMixin,
    LocalStoreSearchMixin,
    LocalStoreEventMixin,
    LocalStoreControlPlaneMixin,
    LocalStoreSharedProgressPanelMixin,
    LocalStoreRuntimeGateLedgerMixin,
    LocalStoreMaintenanceMixin,
):
    """composes LocalStore persistence, search, events, and maintenance APIs.

    给人看的解释：
    你可以把它理解成本地账本的门面：
    - records 表保存'有什么东西'
    - FTS/LIKE 负责'怎么搜到它'
    - files 目录保存'大正文'
    - events 表和 JSONL 保存'发生过什么事'
    """

    # LLM: LocalStore.__init__ 属于 LocalStore 本地事实索引 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 初始化 LocalStore 的依赖、配置和运行期字段。
    def __init__(
        self,
        db_path: str | Path,
        *,
        files_dir: str | Path | None = None,
        events_path: str | Path | None = None,
        enable_fts: bool = True,
    ):
        """initialize LocalStore paths, feature switches, and database schema.

        给人看的解释：
        创建 LocalStore 时只需要告诉它数据库在哪。
        正文目录和事件文件没传就用默认位置，然后立刻建表，保证后续读写不用再判断数据库是否准备好。
        """

        self.db_path = Path(db_path)
        self.root = self.db_path.parent
        self.files_dir = Path(files_dir) if files_dir is not None else self.root / "files"
        self.events_path = Path(events_path) if events_path is not None else self.root / "events.jsonl"
        self.enable_fts = enable_fts
        self._fts_available = False
        self._init_schema()
        self.task_registry = TaskRegistry(self)

    # LLM: LocalStore.fts_available 属于 LocalStore 本地事实索引 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 报告当前 LocalStore 是否同时启用并成功初始化 FTS。
    @property
    def fts_available(self) -> bool:
        """report whether FTS5 search is both enabled and available.

        给人看的解释：
        配置允许 FTS5 还不够，SQLite 本身也得支持。
        这个属性只有在两个条件都满足时才返回 true。
        """

        return self.enable_fts and self._fts_available


__all__ = [
    "AgentEventInput",
    "AgentEventRecord",
    "AgentRuntimeQueryContext",
    "AgentRuntimeQueryResult",
    "AgentRunRecord",
    "AgentTreeReport",
    "LocalSearchResult",
    "LocalStore",
    "LocalStoreEvent",
    "LocalTimelineItem",
    "PREVIEW_CHARS",
    "RuntimeGateLedgerRecord",
    "SharedProgressPanel",
    "TaskRollupRecord",
    "TaskRegistry",
]
