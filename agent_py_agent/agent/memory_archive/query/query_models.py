from __future__ import annotations

"""LLM: query request/response dataclasses and pagination helpers.

新手说明:
这个文件放的是查询请求/响应的数据结构体和分页相关的辅助函数。
它们只是数据结构，不涉及文件读写或业务逻辑。
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ArchiveQueryRequest:
    """LLM: normalized query parameters for archive search."""

    query: str = ""
    since: str | None = None
    until: str | None = None
    level: int | None = None
    layer: str = "all"
    date_key: str | None = None
    limit: int = 100
    filters: dict[str, str] = field(default_factory=dict)


@dataclass
class ArchiveQueryResponse:
    """LLM: normalized archive search response with pagination info."""

    records: list[dict[str, Any]] = field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = 100
    has_more: bool = False

    @property
    def pages(self) -> int:
        """Return total number of pages."""
        if self.page_size <= 0:
            return 1
        return (self.total + self.page_size - 1) // self.page_size


@dataclass
class ResumeContext:
    """LLM: context object for memory resume operations."""

    archive_matches: list[dict[str, Any]] = field(default_factory=list)
    local_hits: list[dict[str, Any]] = field(default_factory=list)
    task_payloads: list[dict[str, Any]] = field(default_factory=list)
    gateway_payloads: list[dict[str, Any]] = field(default_factory=list)
    guidance: dict[str, Any] = field(default_factory=dict)


def paginate_records(
    records: list[dict[str, Any]],
    *,
    page: int = 1,
    page_size: int = 100,
) -> ArchiveQueryResponse:
    """LLM: apply pagination to a record list and return response object.

    新手说明:
    对记录列表进行分页，返回带有分页信息的响应对象。

    参数说明:
    `records` 是完整的记录列表；`page` 是页码（从1开始）；`page_size` 是每页记录数。

    返回说明:
    返回带有分页信息的 ArchiveQueryResponse 对象。
    """

    total = len(records)
    start = (page - 1) * page_size
    end = start + page_size
    page_records = records[start:end]
    return ArchiveQueryResponse(
        records=page_records,
        total=total,
        page=page,
        page_size=page_size,
        has_more=end < total,
    )
