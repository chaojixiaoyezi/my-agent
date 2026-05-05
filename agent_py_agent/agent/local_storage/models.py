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
    """LLM: immutable event DTO returned when LocalStore records an audit event.

    给人看的解释：
    这表示'本地账本里发生过一件事'。比如写入了一条 memory，或者 gateway 处理完一个请求。
    """

    event_id: str
    event_type: str
    record_id: str
    payload: dict[str, Any]
    created_at: float


@dataclass
class LocalTimelineItem:
    """LLM: timeline DTO joining an event with optional record metadata.

    给人看的解释：
    这是给 timeline/status 页面看的事件行。它不只告诉你事件类型，还尽量带上来源、标题和业务 payload。
    """

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
    """LLM: searchable LocalStore record DTO with loaded content and ranking score.

    给人看的解释：
    这是一次搜索或读取记录后返回的'卡片'。里面有来源、标题、正文、元数据、可见性和分数。
    """

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
