"""游标增量拉取:按 offset 续读源接口直到追平流末尾或预算耗尽;缺口如实入账。

fetch_json 由调用方注入(工具层带出站闸),本模块纯逻辑可测。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass(frozen=True)
class DrainBudget:
    max_events: int
    page_limit: int
    deadline: float


@dataclass
class DrainResult:
    events: list[tuple[int, dict]] = field(default_factory=list)
    cursor: int = 0
    pages: int = 0
    reached_end: bool = False
    gap_events: int = 0
    error: str = ""
    error_code: str = ""


def drain_source(fetch_json, source_url: str, cursor: int, budget: DrainBudget) -> DrainResult:
    """从 cursor 连续翻页拉到流末尾/预算上限;next_cursor 缺失时按页长近似推进。"""
    result = DrainResult(cursor=cursor)
    while len(result.events) < budget.max_events and time.time() < budget.deadline:
        page_ok = _pull_one_page(fetch_json, source_url, result, budget.page_limit)
        if not page_ok or result.reached_end:
            break
    return result


def _pull_one_page(fetch_json, source_url: str, result: DrainResult, page_limit: int) -> bool:
    ok, payload, error_code = fetch_json(_page_url(source_url, result.cursor, page_limit))
    result.pages += 1
    if not ok:
        result.error = str(payload)[:500]
        result.error_code = error_code or "NETWORK_REQUEST_FAILED"
        return False
    items = _items_of(payload)
    next_cursor = _next_cursor_of(payload, result.cursor, len(items))
    _append_items(result, items, next_cursor)
    result.reached_end = len(items) < page_limit
    result.cursor = max(next_cursor, result.cursor)
    return True


def _page_url(source_url: str, cursor: int, limit: int) -> str:
    joiner = "&" if "?" in source_url else "?"
    return f"{source_url}{joiner}since={cursor}&limit={limit}"


def _items_of(payload: object) -> list[dict]:
    """信封适配(纯结构):优先 items 键;否则取顶层唯一的 dict 列表值。"""
    if not isinstance(payload, dict):
        return []
    items = payload.get("items")
    if isinstance(items, list):
        return [item for item in items if isinstance(item, dict)]
    list_values = [value for value in payload.values() if isinstance(value, list)]
    if len(list_values) == 1:
        return [item for item in list_values[0] if isinstance(item, dict)]
    return []


def _next_cursor_of(payload: object, cursor: int, item_count: int) -> int:
    raw = payload.get("next_cursor") if isinstance(payload, dict) else None
    try:
        return int(raw) if raw is not None else cursor + item_count
    except (TypeError, ValueError):
        return cursor + item_count


def _append_items(result: DrainResult, items: list[dict], next_cursor: int) -> None:
    """seq_hint 从 next_cursor 反推(页内连续);首条落点超过请求游标=上游丢弃,记缺口。"""
    if not items:
        return
    first_seq = next_cursor - len(items)
    if first_seq > result.cursor:
        result.gap_events += first_seq - result.cursor
    for index, item in enumerate(items):
        result.events.append((first_seq + index, item))


__all__ = ["DrainBudget", "DrainResult", "drain_source"]
