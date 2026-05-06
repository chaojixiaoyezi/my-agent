from __future__ import annotations

"""LLM: defines LocalStore DTOs and shared constants for search, events, and storage rows.

给人看的解释：
这个文件只放 LocalStore 会传来传去的数据结构。
比如搜索结果长什么样、时间线事件长什么样，以及正文预览最多保留多少字符。
"""

from dataclasses import dataclass
from typing import Any

PREVIEW_CHARS = 12000


@dataclass
class LocalStoreEvent:

    event_id: str
    event_type: str
    record_id: str
    payload: dict[str, Any]
    created_at: float


@dataclass
class LocalTimelineItem:

    event_id: str
    event_type: str
    record_id: str
    source_type: str
    source_id: str
    title: str
    payload: dict[str, Any]
    created_at: float


@dataclass
class LocalSearchResult:

    id: str
    source_type: str
    source_id: str
    title: str
    content: str
    metadata: dict[str, Any]
    visibility: str
    created_at: float
    updated_at: float
    score: float = 0.0
    content_path: str = ""
