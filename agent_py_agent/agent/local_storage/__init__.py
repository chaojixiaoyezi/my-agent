# LLM: 保持 mixin、模型和常量导出稳定，避免破坏 LocalStore 门面。
# 模块用途: LocalStore 拆分实现的公开导出点。

from __future__ import annotations

"""public LocalStore support package split by schema, records, search, events, and maintenance.

给人看的解释：
LocalStore 已经拆成几个职责清楚的小文件。
外部仍然从 `agent.local_store` 导入主类；这里主要给内部组合类使用。
"""

from .events import LocalStoreEventMixin
from .maintenance import LocalStoreMaintenanceMixin
from .models import PREVIEW_CHARS, LocalSearchResult, LocalStoreEvent, LocalTimelineItem
from .records import LocalRecordInput, LocalRecordLogInput, LocalStoreRecordMixin
from .schema import LocalStoreSchemaMixin
from .search import LocalStoreSearchMixin

__all__ = [
    "LocalSearchResult",
    "LocalRecordInput",
    "LocalRecordLogInput",
    "LocalStoreEvent",
    "LocalStoreEventMixin",
    "LocalStoreMaintenanceMixin",
    "LocalStoreRecordMixin",
    "LocalStoreSchemaMixin",
    "LocalStoreSearchMixin",
    "LocalTimelineItem",
    "PREVIEW_CHARS",
]
