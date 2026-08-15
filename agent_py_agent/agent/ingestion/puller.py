"""游标增量拉取:按 offset 续读源接口直到追平流末尾或预算耗尽;缺口如实入账。

fetch_json 由调用方注入(工具层带出站闸),本模块纯逻辑可测。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .source_http import render_source_http_request


@dataclass(frozen=True)
class DrainBudget:
    max_events: int
    page_limit: int
    deadline: float


@dataclass
class DrainResult:
    events: list[tuple[int, dict]] = field(default_factory=list)
    cursor: int = 0
    # Only prepare-learned source adapters use this opaque JSON checkpoint.
    # ``cursor`` remains the local monotonic record ordinal for spool/source_ref.
    source_checkpoint: dict[str, Any] | None = None
    # Prepare-learned adapters return one stable source-position key for each
    # complete record.  It identifies the source delivery position, not event
    # contents or a business/device id; the harvester commits it atomically
    # with cursor/spool so only an overlapping retry of that position dedupes.
    source_record_keys: dict[int, str] = field(default_factory=dict)
    pages: int = 0
    reached_end: bool = False
    gap_events: int = 0
    error: str = ""
    error_code: str = ""
    # 多形态源的附加游标(sources.py):file 源=已读行号;其余源恒 0。
    aux_cursor: int = 0
    # poll 源本次真的查了接口的时刻(0=没查,节拍未到);调用方据此更新 last_poll_at。
    fetched_at: float = 0.0
    # HTTP 游标源因整页响应超过传输安全上限时，按完整记录数减半后的本次有效页长。
    # 0 表示本次不适用/未调整；调用方可把更小值保存为该 watch 后续页长上限。
    effective_page_limit: int = 0
    page_limit_reductions: int = 0
    # file 来源临时读取事实。未完成片段只在调用边界内携带，调用方须先持久化它，
    # 才能提交任何新的文件游标。
    source_identity: str = ""
    incomplete_fragment: bytes | None = None
    fragment_start: int = 0
    fragment_checked: bool = False


def drain_source(
    fetch_json,
    source_url: str,
    cursor: int,
    budget: DrainBudget,
    *,
    source_checkpoint: dict[str, Any] | None = None,
    source_envelope: dict[str, object] | None = None,
) -> DrainResult:
    """从来源断点连续翻页；本地记录序号与外部游标严格分离。

    ``cursor`` 只表示本 watch 已提交的本地完整记录数量。HTTP 来源返回的
    时间戳、offset、页号或其他整数位置保存在 ``source_checkpoint``，不能拿来
    生成本地记录序号或推断丢失数量。只有来源契约明确声明游标是连续记录位次时，
    才允许做缺口算术。

    若服务明确返回 ``ARTIFACT_TOO_LARGE``，就在同一游标把完整记录页长减半
    重试，最小为 1；limit=1
    仍过大时保留结构化错误，让调用方明确报告这个极端单条，绝不静默截断。
    """
    external_cursor = _checkpoint_cursor(source_checkpoint, fallback=cursor)
    result = DrainResult(
        cursor=cursor,
        source_checkpoint={"external_cursor": external_cursor},
    )
    if budget.max_events <= 0:
        return result
    page_limit = max(1, int(budget.page_limit))
    result.effective_page_limit = page_limit
    while len(result.events) < budget.max_events and time.time() < budget.deadline:
        requested = page_limit
        page_ok = _pull_one_page(
            fetch_json,
            source_url,
            result,
            requested,
            source_envelope=source_envelope,
        )
        if not page_ok:
            if result.error_code == "ARTIFACT_TOO_LARGE" and requested > 1:
                page_limit = max(1, requested // 2)
                result.effective_page_limit = page_limit
                result.page_limit_reductions += 1
                result.error = ""
                result.error_code = ""
                continue
            break
        if result.reached_end:
            break
    return result


def _pull_one_page(
    fetch_json,
    source_url: str,
    result: DrainResult,
    page_limit: int,
    *,
    source_envelope: dict[str, object] | None = None,
) -> bool:
    # 只使用该来源明确返回并已提交的下一位置。程序不猜接口是包含型还是排他型，
    # 也不再统一回退一格。若现场文档证明请求值需要相对已提交位置偏移，偏移作为
    # 该具体来源的 cursor_binding.offset 保存，由请求渲染层机械执行。
    request_cursor = _checkpoint_cursor(result.source_checkpoint, fallback=result.cursor)
    envelope = source_envelope or {}
    request_facts = envelope.get("request")
    if not isinstance(request_facts, dict):
        result.error = "HTTP 来源缺少已验证的请求配置"
        result.error_code = "SOURCE_REQUEST_INVALID"
        return False
    try:
        request = render_source_http_request(
            source_url,
            request_facts,
            cursor=request_cursor,
            page_size=page_limit,
        )
    except ValueError as exc:
        result.error = str(exc)[:500]
        result.error_code = (
            "SOURCE_SECRET_UNAVAILABLE"
            if "SecretRef" in str(exc)
            else "SOURCE_REQUEST_INVALID"
        )
        return False
    ok, payload, error_code = fetch_json(request)
    result.pages += 1
    if not ok:
        result.error = str(payload)[:500]
        result.error_code = error_code or "NETWORK_REQUEST_FAILED"
        return False
    try:
        items = _items_of(
            payload,
            field=str(envelope.get("record_list_key") or ""),
        )
        next_cursor = _next_cursor_of(
            payload,
            field=str(envelope.get("cursor_field") or ""),
            semantics=str(envelope.get("cursor_semantics") or ""),
        )
        has_more = _has_more_of(
            payload,
            field=str(envelope.get("has_more_field") or ""),
        )
    except ValueError as exc:
        result.error = str(exc)[:500]
        result.error_code = "SOURCE_ENVELOPE_INVALID"
        return False
    if items and next_cursor <= request_cursor:
        result.error = (
            f"数据源返回了 {len(items)} 条记录，但 next_cursor={next_cursor} "
            f"没有越过本次已提交位置={request_cursor}"
        )[:500]
        result.error_code = "SOURCE_CURSOR_STALLED"
        return False
    if has_more is True and not items:
        result.error = (
            "数据源声明 has_more=true，但当前页没有完整记录，无法证明游标推进"
        )
        result.error_code = "SOURCE_CURSOR_STALLED"
        return False
    previous_external_cursor = request_cursor
    appended = _append_items(
        result,
        items,
        next_cursor,
        previous_external_cursor=previous_external_cursor,
        cursor_position_semantics=str(
            envelope.get("cursor_position_semantics") or "opaque"
        ),
    )
    if has_more is True and appended == 0 and next_cursor <= previous_external_cursor:
        result.error = (
            "数据源声明 has_more=true，但 next_cursor 没有产生可消费进展"
        )
        result.error_code = "SOURCE_CURSOR_STALLED"
        return False
    result.reached_end = (
        not has_more
        if has_more is not None
        else (
            len(items) < page_limit
            or (appended == 0 and next_cursor <= previous_external_cursor)
        )
    )
    result.source_checkpoint = {"external_cursor": next_cursor}
    return True


def _items_of(payload: object, *, field: str = "") -> list[dict]:
    """信封适配(纯结构):优先 items 键;否则取顶层唯一的列表值。

    页内每个位置都是一条源记录。非对象 JSON 值用 ``source_item`` 无损包装，
    不能因为类型不理想就静默丢掉并打乱游标位置。没有唯一列表边界时明确失败，
    留在同一游标等待接入方式修正，而不是猜一个字段继续。
    """
    if not isinstance(payload, dict):
        raise ValueError("游标源响应必须是 JSON 对象信封")
    if field:
        if field not in payload:
            raise ValueError(f"游标源响应缺少记录数组字段 {field}")
        items = payload.get(field)
        if not isinstance(items, list):
            raise ValueError(f"游标源响应的 {field} 必须是数组")
        return [_event_item(item) for item in items]
    if "items" in payload:
        items = payload.get("items")
        if not isinstance(items, list):
            raise ValueError("游标源响应的 items 必须是数组")
        return [_event_item(item) for item in items]
    list_values = [value for value in payload.values() if isinstance(value, list)]
    if len(list_values) == 1:
        return [_event_item(item) for item in list_values[0]]
    if not list_values:
        raise ValueError("游标源响应没有 items，也没有唯一的顶层记录数组")
    raise ValueError("游标源响应有多个顶层数组，无法确定哪一个是记录列表")


def _event_item(item: object) -> dict:
    if isinstance(item, dict):
        return item
    return {"source_item": item}


def _next_cursor_of(
    payload: object,
    *,
    field: str = "",
    semantics: str = "",
) -> int:
    cursor_field = field or "next_cursor"
    if not isinstance(payload, dict) or cursor_field not in payload:
        raise ValueError(f"游标源响应缺少 {cursor_field}")
    raw = payload.get(cursor_field)
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"游标源 {cursor_field} 不是整数: {raw!r}") from None
    if isinstance(raw, bool):
        raise ValueError(f"游标源 {cursor_field} 不能是布尔值")
    if parsed < 0:
        raise ValueError(f"游标源 {cursor_field} 不能为负数: {parsed}")
    cursor_semantics = semantics or "next_position"
    if cursor_semantics == "last_seen":
        return parsed + 1
    if cursor_semantics != "next_position":
        raise ValueError(f"游标源 cursor_semantics 不支持: {cursor_semantics}")
    return parsed


def _has_more_of(payload: object, *, field: str = "") -> bool | None:
    has_more_field = field or "has_more"
    if not isinstance(payload, dict) or has_more_field not in payload:
        return None
    raw = payload.get(has_more_field)
    if not isinstance(raw, bool):
        raise ValueError(f"游标源 {has_more_field} 不是布尔值: {raw!r}")
    return raw


def _append_items(
    result: DrainResult,
    items: list[dict],
    next_cursor: int,
    *,
    previous_external_cursor: int,
    cursor_position_semantics: str,
) -> int:
    """把完整来源记录映射为本地连续序号。

    默认外部游标是 opaque(不透明位置)，例如时间戳、页码或供应商 offset；程序只
    负责保存并在下一次请求时原样使用，不能用数值差猜丢了多少条。只有 prepare
    已明确发布 ``contiguous_record_ordinal`` 时，才可按来源位次去重重叠边界并
    统计真实缺口。无论哪种外部游标，本地序号都只按实际接纳的完整记录递增。
    """
    if not items:
        return 0
    accepted = items
    if cursor_position_semantics == "contiguous_record_ordinal":
        first_external_position = next_cursor - len(items)
        accepted = [
            item
            for index, item in enumerate(items)
            if first_external_position + index >= previous_external_cursor
        ]
        if accepted:
            first_fresh_external = next_cursor - len(accepted)
            if previous_external_cursor > 0 and first_fresh_external > previous_external_cursor:
                result.gap_events += first_fresh_external - previous_external_cursor
    start = result.cursor
    result.events.extend((start + index, item) for index, item in enumerate(accepted))
    result.cursor += len(accepted)
    return len(accepted)


def _checkpoint_cursor(checkpoint: object, *, fallback: int) -> int:
    """Read the typed external HTTP cursor; old states migrate on first commit."""

    if isinstance(checkpoint, dict) and "external_cursor" in checkpoint:
        raw = checkpoint.get("external_cursor")
        if not isinstance(raw, bool):
            try:
                parsed = int(raw)
            except (TypeError, ValueError):
                parsed = -1
            if parsed >= 0:
                return parsed
    return max(0, int(fallback))


__all__ = ["DrainBudget", "DrainResult", "drain_source"]
