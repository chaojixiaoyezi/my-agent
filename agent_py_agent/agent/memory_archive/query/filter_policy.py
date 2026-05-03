from __future__ import annotations

"""LLM: filter predicate evaluation for archive query.

新手说明:
这个文件放的是查询过滤器的谓词评估函数。
它们接收一条记录，返回 True/False 表示是否应该包含在结果中。
"""

from typing import Any

from .archive_helpers import _archive_search_text, _created_at_sort, _is_date_only


def filter_by_fields(record: dict[str, Any], filters: dict[str, str]) -> bool:
    """LLM: apply exact-match field filters to one record.

    新手说明:
    检查记录的所有指定字段是否与过滤器中的值完全匹配。

    参数说明:
    `record` 是标准化归档记录；`filters` 是需要精确匹配的字段字典。

    返回说明:
    匹配过滤器时返回 True，否则返回 False。
    """

    for field_name, value in filters.items():
        if str(record.get(field_name, "")) != value:
            return False
    return True


def filter_by_level(record: dict[str, Any], level: int | None) -> bool:
    """LLM: apply archive level filter to one record.

    新手说明:
    如果指定了 level，检查记录的 archive_level 是否匹配。

    参数说明:
    `record` 是标准化归档记录；`level` 是目标等级。

    返回说明:
    通过等级过滤时返回 True，否则返回 False。
    """

    if level is None:
        return True
    return int(record.get("archive_level", -1)) == int(level)


def filter_by_time_window(
    record: dict[str, Any],
    since_ts: float | None,
    until_ts: float | None,
) -> bool:
    """LLM: apply time window filter to one record.

    新手说明:
    如果记录的时间戳不在指定窗口内，返回 False。

    参数说明:
    `record` 是标准化归档记录；`since_ts` 是窗口起始时间戳；`until_ts` 是窗口结束时间戳。

    返回说明:
    在时间窗口内时返回 True，否则返回 False。
    """

    created_at = float(record.get("created_at_sort", 0.0) or 0.0)
    if since_ts is not None and created_at < since_ts:
        return False
    if until_ts is not None and created_at > until_ts:
        return False
    return True


def filter_by_query_text(record: dict[str, Any], query_text: str) -> bool:
    """LLM: apply keyword search filter to one record.

    新手说明:
    如果 query_text 不为空，检查它是否出现在记录的搜索文本中。

    参数说明:
    `record` 是标准化归档记录；`query_text` 是小写关键词。

    返回说明:
    匹配关键词时返回 True，否则返回 False。
    """

    if not query_text:
        return True
    return query_text in _archive_search_text(record)


def evaluate_filters(
    record: dict[str, Any],
    *,
    query: str,
    filters: dict[str, str],
    since: str | None,
    until: str | None,
    level: int | None = None,
) -> bool:
    """LLM: apply all filter predicates to one record.

    新手说明:
    对一条记录应用所有过滤器谓词。所有谓词都返回 True 时才通过。

    参数说明:
    `record` 是标准化归档记录；其他参数与 filter_archive_records 相同。

    返回说明:
    所有过滤器都通过时返回 True，否则返回 False。
    """

    if not filter_by_fields(record, filters):
        return False
    if not filter_by_level(record, level):
        return False

    since_ts = _created_at_sort(since or "", fallback=0.0) if since else None
    until_ts = _created_at_sort(until or "", fallback=0.0) if until else None
    if until_ts is not None and _is_date_only(until or ""):
        until_ts += 86399.999999

    if not filter_by_time_window(record, since_ts, until_ts):
        return False
    if not filter_by_query_text(record, query.strip().lower()):
        return False
    return True
