
from __future__ import annotations

"""query request/response dataclasses and pagination helpers.

新手说明:
这个文件放的是查询请求/响应的数据结构体和分页相关的辅助函数。
它们只是数据结构，不涉及文件读写或业务逻辑。
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ArchiveQueryRequest:
    """normalized query parameters for archive search."""

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
    """normalized archive search response with pagination info."""

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
    """context object for memory resume operations."""

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
