# LLM: Memory archive module; keep task/run workspace files and long-term memory records stable.
# 模块用途: 维护任务工作区、运行记录、compact 链和长期记忆归档。

from __future__ import annotations

"""filter predicate evaluation for archive query.

新手说明:
这个文件放的是查询过滤器的谓词评估函数。
它们接收一条记录，返回 True/False 表示是否应该包含在结果中。
"""

from dataclasses import dataclass
from typing import Any

from .archive_helpers import _archive_search_text, _created_at_sort, _is_date_only


# LLM: 归档查询从 archive JSON/JSONL 与 workspace 文件读取可恢复事实；修改 ArchiveFilterOptions 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 ArchiveFilterOptions 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass(frozen=True)
class ArchiveFilterOptions:
    query: str
    filters: dict[str, str]
    since: str | None
    until: str | None
    level: int | None = None


# LLM: 归档查询从 archive JSON/JSONL 与 workspace 文件读取可恢复事实；修改 filter_by_fields 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 判断 filter by fields 是否满足规则、查询或上下文条件，返回确定性的筛选结果。
def filter_by_fields(record: dict[str, Any], filters: dict[str, str]) -> bool:

    for field_name, value in filters.items():
        if str(record.get(field_name, "")) != value:
            return False
    return True


# LLM: 归档查询从 archive JSON/JSONL 与 workspace 文件读取可恢复事实；修改 filter_by_level 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 判断 filter by level 是否满足规则、查询或上下文条件，返回确定性的筛选结果。
def filter_by_level(record: dict[str, Any], level: int | None) -> bool:

    if level is None:
        return True
    return int(record.get("archive_level", -1)) == int(level)


# LLM: 归档查询从 archive JSON/JSONL 与 workspace 文件读取可恢复事实；修改 filter_by_time_window 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 判断 filter by time window 是否满足规则、查询或上下文条件，返回确定性的筛选结果。
def filter_by_time_window(
    record: dict[str, Any],
    since_ts: float | None,
    until_ts: float | None,
) -> bool:

    created_at = float(record.get("created_at_sort", 0.0) or 0.0)
    if since_ts is not None and created_at < since_ts:
        return False
    if until_ts is not None and created_at > until_ts:
        return False
    return True


# LLM: 归档查询从 archive JSON/JSONL 与 workspace 文件读取可恢复事实；修改 filter_by_query_text 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 判断 filter by query text 是否满足规则、查询或上下文条件，返回确定性的筛选结果。
def filter_by_query_text(record: dict[str, Any], query_text: str) -> bool:

    if not query_text:
        return True
    return query_text in _archive_search_text(record)


# LLM: 归档查询从 archive JSON/JSONL 与 workspace 文件读取可恢复事实；修改 evaluate_filters 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 基于规则或事件字段计算 evaluate filters 的判定结果，避免把推测当作事实写入。
def evaluate_filters(
    record: dict[str, Any],
    options: ArchiveFilterOptions,
) -> bool:
    if not filter_by_fields(record, options.filters):
        return False
    if not filter_by_level(record, options.level):
        return False

    since_ts = _created_at_sort(options.since or "", fallback=0.0) if options.since else None
    until_ts = _created_at_sort(options.until or "", fallback=0.0) if options.until else None
    if until_ts is not None and _is_date_only(options.until or ""):
        until_ts += 86399.999999

    if not filter_by_time_window(record, since_ts, until_ts):
        return False
    if not filter_by_query_text(record, options.query.strip().lower()):
        return False
    return True
