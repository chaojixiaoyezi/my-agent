# LLM: 字段名是上层渲染和测试的契约，新增字段要保持默认兼容。
# 模块用途: LocalStore 查询结果、时间线条目和事件的 dataclass 模型。

from __future__ import annotations

"""defines LocalStore DTOs and shared constants for search, events, and storage rows.

给人看的解释：
这个文件只放 LocalStore 会传来传去的数据结构。
比如搜索结果长什么样、时间线事件长什么样，以及正文预览最多保留多少字符。
"""

from dataclasses import dataclass
from typing import Any

PREVIEW_CHARS = 12000


# LLM: LocalStoreEvent 属于 LocalStore 本地事实索引 的稳定结构；调整字段或继承关系前先核对序列化、导入和测试。
# 类用途: LocalStore 事件模型，保存事件类型、来源、目标和载荷。
@dataclass
class LocalStoreEvent:

    event_id: str
    event_type: str
    record_id: str
    payload: dict[str, Any]
    created_at: float


# LLM: LocalTimelineItem 属于 LocalStore 本地事实索引 的稳定结构；调整字段或继承关系前先核对序列化、导入和测试。
# 类用途: LocalStore 时间线条目，合并记录与事件展示字段。
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


# LLM: LocalSearchResult 属于 LocalStore 本地事实索引 的稳定结构；调整字段或继承关系前先核对序列化、导入和测试。
# 类用途: LocalStore 搜索结果，保存记录元数据、正文预览和得分。
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
