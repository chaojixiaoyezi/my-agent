# LLM: Memory archive module; keep task/run workspace files and long-term memory records stable.
# 模块用途: 维护任务工作区、运行记录、compact 链和长期记忆归档。

from __future__ import annotations

"""query request/response dataclasses and pagination helpers.

新手说明:
这个文件放的是查询请求/响应的数据结构体和分页相关的辅助函数。
它们只是数据结构，不涉及文件读写或业务逻辑。
"""

from dataclasses import dataclass, field
from typing import Any


# LLM: 归档查询从 archive JSON/JSONL 与 workspace 文件读取可恢复事实；修改 ArchiveQueryRequest 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 ArchiveQueryRequest 的字段集合，在模块边界间传递结构化状态和结果。
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


# LLM: 归档查询从 archive JSON/JSONL 与 workspace 文件读取可恢复事实；修改 ArchiveQueryResponse 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 ArchiveQueryResponse 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass
class ArchiveQueryResponse:
    """normalized archive search response with pagination info."""

    records: list[dict[str, Any]] = field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = 100
    has_more: bool = False

    # LLM: 归档查询从 archive JSON/JSONL 与 workspace 文件读取可恢复事实；修改 pages 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 完成 pages 在当前模块中的核心转换或协调步骤，衔接 归档查询从 archive JSON/JSONL 与 workspace 文件读取可恢复事实。
    @property
    def pages(self) -> int:
        """Return total number of pages."""
        if self.page_size <= 0:
            return 1
        return (self.total + self.page_size - 1) // self.page_size


# LLM: 归档查询从 archive JSON/JSONL 与 workspace 文件读取可恢复事实；修改 ResumeContext 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 ResumeContext 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass
class ResumeContext:
    """context object for memory resume operations."""

    archive_matches: list[dict[str, Any]] = field(default_factory=list)
    local_hits: list[dict[str, Any]] = field(default_factory=list)
    task_payloads: list[dict[str, Any]] = field(default_factory=list)
    gateway_payloads: list[dict[str, Any]] = field(default_factory=list)
    guidance: dict[str, Any] = field(default_factory=dict)


# LLM: 归档查询从 archive JSON/JSONL 与 workspace 文件读取可恢复事实；修改 paginate_records 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 paginate records 在当前模块中的核心转换或协调步骤，衔接 归档查询从 archive JSON/JSONL 与 workspace 文件读取可恢复事实。
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
