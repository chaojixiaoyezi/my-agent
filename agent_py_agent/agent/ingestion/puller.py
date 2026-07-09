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
    # 多形态源的附加游标(sources.py):file 源=已读行号;其余源恒 0。
    aux_cursor: int = 0
    # poll 源本次真的查了接口的时刻(0=没查,节拍未到);调用方据此更新 last_poll_at。
    fetched_at: float = 0.0


def drain_source(fetch_json, source_url: str, cursor: int, budget: DrainBudget) -> DrainResult:
    """从 cursor 连续翻页拉到流末尾/预算上限;next_cursor 缺失时按页长近似推进。"""
    result = DrainResult(cursor=cursor)
    while len(result.events) < budget.max_events and time.time() < budget.deadline:
        page_ok = _pull_one_page(fetch_json, source_url, result, budget.page_limit)
        if not page_ok or result.reached_end:
            break
    return result


def _pull_one_page(fetch_json, source_url: str, result: DrainResult, page_limit: int) -> bool:
    # 游标语义无关拉取(治真机实锤:源把 since 当【排他】>since 解读时,按 since=next_cursor 拉每拍
    # 都跳过边界那条→每隔一条丢一条,真事入队率腰斩)。做法:回退一格拉(since=cursor-1),把边界
    # 那条一并要回来,再按【已读位次】去重——
    #   · 包含型源(seq>=since):返回 [cursor-1, cursor, …],去重掉 cursor-1(已读),其余照收;
    #   · 排他型源(seq>since):返回 [cursor, cursor+1, …],一条不落全收。
    # 两种源都不丢、不重。cursor=0(从未读过)不回退(没有前一格)。位次由 next_cursor 反推(权威);
    # 源不给 next_cursor 时按包含型近似(与历史行为一致,标准 ?since= 语义)。
    since = result.cursor - 1 if result.cursor > 0 else 0
    ok, payload, error_code = fetch_json(_page_url(source_url, since, page_limit))
    result.pages += 1
    if not ok:
        result.error = str(payload)[:500]
        result.error_code = error_code or "NETWORK_REQUEST_FAILED"
        return False
    items = _items_of(payload)
    next_cursor = _next_cursor_of(payload, since, len(items))
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


def _next_cursor_of(payload: object, since: int, item_count: int) -> int:
    raw = payload.get("next_cursor") if isinstance(payload, dict) else None
    try:
        return int(raw) if raw is not None else since + item_count
    except (TypeError, ValueError):
        return since + item_count


def _append_items(result: DrainResult, items: list[dict], next_cursor: int) -> None:
    """seq_hint 从 next_cursor 反推(页内连续);收进【本次请求游标及之后】的位次。

    去重回退格:拉取回退了一格(since=cursor-1),边界那条(位次 < cursor=已读)只是为了在排他型
    源上把该收的那条要回来,这里丢弃它——包含型源不会双收,排他型源不会漏。缺口只按【真丢】记:
    首条【新】位次越过下一个应读位次(cursor)=上游滚动缓冲淘汰,如实入账;首拍(cursor=0,从未读过)
    的流起点不算缺口(此前没有任何已读位次,谈不上"漏")。"""
    if not items:
        return
    first_seq = next_cursor - len(items)
    fresh: list[tuple[int, dict]] = []
    for index, item in enumerate(items):
        seq = first_seq + index
        if seq < result.cursor:
            continue  # 回退格重复拉回的已读边界条,去重
        fresh.append((seq, item))
    if not fresh:
        return
    lead_seq = fresh[0][0]
    if result.cursor > 0 and lead_seq > result.cursor:
        result.gap_events += lead_seq - result.cursor
    result.events.extend(fresh)


__all__ = ["DrainBudget", "DrainResult", "drain_source"]
