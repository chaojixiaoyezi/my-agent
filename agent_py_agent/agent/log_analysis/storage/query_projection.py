# LLM: Log-analysis module; keep ingest, query, and detector data contracts stable.
# 模块用途: 支撑日志导入、查询、检测、案例和分析报告生成。

from __future__ import annotations

"""query row matching, summaries, and preview projection for local log storage."""

from collections import Counter
from typing import Any

from .base import QueryCriteria, event_time_value, nested_get, parse_event_time, stable_digest

FIELD_ALIASES = {
    "attacker_ip": ("attacker_ip", "src_ip"),
    "victim_ip": ("victim_ip", "dst_ip"),
    "domain": ("domain", "host", "sni", "dns_query"),
    "uri": ("uri", "url", "path", "api"),
    "alert_type": ("alert_type", "event_type"),
}

PREVIEW_FIELDS = (
    "event_id",
    "alert_id",
    "event_time",
    "source_id",
    "source_product",
    "event_type",
    "alert_type",
    "threat_name",
    "attacker_ip",
    "victim_ip",
    "src_ip",
    "dst_ip",
    "domain",
    "uri",
    "api",
    "raw_ref",
)


# LLM: 日志分析存储层读写本地事件、finding、case 和 evidence 投影；修改 summarize_rows 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 summarize rows 在当前模块中的核心转换或协调步骤，衔接 日志分析存储层读写本地事件、finding、case 和 evidence 投影。
def summarize_rows(rows: list[dict[str, Any]], parameters: dict[str, Any]) -> dict[str, Any]:
    times = [str(event_time_value(row)) for row in rows if event_time_value(row) not in (None, "")]
    return {
        "filters": {key: value for key, value in parameters.items() if key != "limit" and value not in (None, "")},
        "row_count": len(rows),
        "first_event_time": min(times) if times else None,
        "last_event_time": max(times) if times else None,
        "top_alert_types": _top_counts(rows, "alert_type"),
        "top_attackers": _top_counts(rows, "attacker_ip"),
        "top_victims": _top_counts(rows, "victim_ip"),
        "top_domains": _top_counts(rows, "domain"),
    }


# LLM: 日志分析存储层读写本地事件、finding、case 和 evidence 投影；修改 sanitize_event_for_preview 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 sanitize event for preview 在当前模块中的核心转换或协调步骤，衔接 日志分析存储层读写本地事件、finding、case 和 evidence 投影。
def sanitize_event_for_preview(row: dict[str, Any]) -> dict[str, Any]:
    preview: dict[str, Any] = {}
    for field in PREVIEW_FIELDS:
        value = nested_get(row, field)
        if value not in (None, ""):
            preview[field] = value
    payload = nested_get(row, "payload")
    if payload not in (None, ""):
        text = str(payload)
        preview["payload_preview"] = text[:120]
        preview["payload_sha256"] = stable_digest(text)
    return preview


# LLM: 日志分析存储层读写本地事件、finding、case 和 evidence 投影；修改 query_matches 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 收集或查询 query matches 的候选结果，并按参数完成筛选、排序或数量限制。
def query_matches(row: dict[str, Any], criteria: QueryCriteria) -> bool:
    if not _matches_time(row, criteria.start_time, criteria.end_time):
        return False
    for field, aliases in FIELD_ALIASES.items():
        expected = getattr(criteria, field)
        if expected in (None, ""):
            continue
        if not _matches_any_alias(row, aliases, str(expected)):
            return False
    return True


# LLM: 日志分析存储层读写本地事件、finding、case 和 evidence 投影；修改 _matches_time 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 判断 matches time 是否满足规则、查询或上下文条件，返回确定性的筛选结果。
def _matches_time(row: dict[str, Any], start_time: str | None, end_time: str | None) -> bool:
    if not start_time and not end_time:
        return True
    value = parse_event_time(event_time_value(row))
    if value is None:
        return False
    start = parse_event_time(start_time) if start_time else None
    end = parse_event_time(end_time) if end_time else None
    if start is not None and value < start:
        return False
    if end is not None and value > end:
        return False
    return True


# LLM: 日志分析存储层读写本地事件、finding、case 和 evidence 投影；修改 _matches_any_alias 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 判断 matches any alias 是否满足规则、查询或上下文条件，返回确定性的筛选结果。
def _matches_any_alias(row: dict[str, Any], aliases: tuple[str, ...], expected: str) -> bool:
    expected_normalized = expected.lower()
    for alias in aliases:
        actual = nested_get(row, alias)
        if actual is None:
            continue
        if str(actual).lower() == expected_normalized:
            return True
    return False


# LLM: 日志分析存储层读写本地事件、finding、case 和 evidence 投影；修改 _top_counts 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 top counts 在当前模块中的核心转换或协调步骤，衔接 日志分析存储层读写本地事件、finding、case 和 evidence 投影。
def _top_counts(rows: list[dict[str, Any]], field: str) -> list[dict[str, Any]]:
    counter: Counter[str] = Counter()
    aliases = FIELD_ALIASES.get(field, (field,))
    for row in rows:
        _count_first_alias(counter, row, aliases)
    return [{"value": value, "count": count} for value, count in counter.most_common(5)]


# LLM: 日志分析存储层读写本地事件、finding、case 和 evidence 投影；修改 _count_first_alias 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 count first alias 在当前模块中的核心转换或协调步骤，衔接 日志分析存储层读写本地事件、finding、case 和 evidence 投影。
def _count_first_alias(counter: Counter[str], row: dict[str, Any], aliases: tuple[str, ...]) -> None:
    for alias in aliases:
        value = nested_get(row, alias)
        if value not in (None, ""):
            counter[str(value)] += 1
            return
