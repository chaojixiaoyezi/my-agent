
from __future__ import annotations

"""filter predicate evaluation for archive query.

新手说明:
这个文件放的是查询过滤器的谓词评估函数。
它们接收一条记录，返回 True/False 表示是否应该包含在结果中。
"""

from dataclasses import dataclass
from typing import Any

from .archive_helpers import _archive_search_text, _created_at_sort, _is_date_only


@dataclass(frozen=True)
class ArchiveFilterOptions:
    query: str
    filters: dict[str, str]
    since: str | None
    until: str | None
    level: int | None = None


def filter_by_fields(record: dict[str, Any], filters: dict[str, str]) -> bool:

    for field_name, value in filters.items():
        if str(record.get(field_name, "")) != value:
            return False
    return True


def filter_by_level(record: dict[str, Any], level: int | None) -> bool:

    if level is None:
        return True
    return int(record.get("archive_level", -1)) == int(level)


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


def filter_by_query_text(record: dict[str, Any], query_text: str) -> bool:

    if not query_text:
        return True
    return query_text in _archive_search_text(record)


def evaluate_filters(
    record: dict[str, Any],
    options: ArchiveFilterOptions,
) -> bool:
    if not filter_by_fields(record, options.filters):
        return False
    if not filter_by_level(record, options.level):
        return False

    since_ts = _created_at_sort(options.since or "", default=0.0) if options.since else None
    until_ts = _created_at_sort(options.until or "", default=0.0) if options.until else None
    if until_ts is not None and _is_date_only(options.until or ""):
        until_ts += 86399.999999

    if not filter_by_time_window(record, since_ts, until_ts):
        return False
    if not filter_by_query_text(record, options.query.strip().lower()):
        return False
    return True
