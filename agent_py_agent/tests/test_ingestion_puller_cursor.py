"""游标请求事实回归：程序只执行该来源现场学得的绑定和偏移，不猜接口语义。

包含型来源直接提交下一位置；若一个具体来源的文档证明请求游标需使用上一个位置，
Agent 把 ``cursor_binding.offset=-1`` 钉在该来源上。两种事实都必须无丢失、无重复。
"""

from __future__ import annotations

import time
from urllib.parse import parse_qs, urlsplit

from agent.ingestion.puller import DrainBudget
from agent.ingestion.puller import drain_source as _runtime_drain_source

_REQUEST_FACTS = {
    "method": "GET",
    "cursor_binding": {"location": "query", "name": "since", "initial": 0},
    "page_size_binding": {"location": "query", "name": "limit"},
}


def drain_source(
    fetch_json,
    source_url,
    cursor,
    budget,
    *,
    source_checkpoint=None,
    source_envelope=None,
):
    envelope = dict(source_envelope or {})
    envelope.setdefault("request", _REQUEST_FACTS)
    return _runtime_drain_source(
        fetch_json,
        source_url,
        cursor,
        budget,
        source_checkpoint=source_checkpoint,
        source_envelope=envelope,
    )


class _CursorSource:
    """内存游标源:emit 递增 seq;since 语义可切包含(>=)/排他(>);next_cursor=last+1。"""

    def __init__(self, *, exclusive: bool = False, next_cursor: bool = True, retain: int = 0) -> None:
        self.exclusive = exclusive
        self.emit_next_cursor = next_cursor
        self.retain = retain
        self.events: list[dict] = []
        self.seq = 0
        self._dropped = 0

    def emit(self, n: int = 1) -> None:
        for _ in range(n):
            self.seq += 1
            self.events.append({"seq": self.seq, "event_id": f"E-{self.seq:05d}"})
        if self.retain and len(self.events) > self.retain:
            drop = len(self.events) - self.retain
            self._dropped += drop
            self.events = self.events[drop:]

    def fetch(self, request) -> tuple[bool, object, str]:
        from urllib.parse import parse_qs, urlsplit

        url = request.url
        q = parse_qs(urlsplit(url).query)
        since = int(q.get("since", ["0"])[0])
        limit = int(q.get("limit", ["400"])[0])
        if self.exclusive:
            items = [dict(e) for e in self.events if e["seq"] > since][:limit]
        else:
            items = [dict(e) for e in self.events if e["seq"] >= since][:limit]
        payload: dict = {"items": items}
        if self.emit_next_cursor:
            payload["next_cursor"] = (items[-1]["seq"] + 1) if items else max(since, self.seq)
        return True, payload, ""


def _drain_all(src: _CursorSource, *, steps: int, emit_each: int = 1) -> tuple[list[int], int]:
    """模拟慢产快拉:每步 emit 若干 + drain 一拍;累计入队 seq 与缺口。"""
    cursor = 0
    checkpoint: dict = {}
    enqueued: list[int] = []
    gaps = 0
    for _ in range(steps):
        src.emit(emit_each)
        budget = DrainBudget(max_events=20000, page_limit=400, deadline=time.time() + 5)
        request = dict(_REQUEST_FACTS)
        if src.exclusive:
            request = {
                **request,
                "cursor_binding": {
                    **request["cursor_binding"],
                    "offset": -1,
                },
            }
        d = drain_source(
            src.fetch,
            "http://x/pull",
            cursor,
            budget,
            source_checkpoint=checkpoint,
            source_envelope={
                "request": request,
                "cursor_position_semantics": "contiguous_record_ordinal",
            },
        )
        cursor = d.cursor
        checkpoint = dict(d.source_checkpoint or {})
        enqueued += [ev["seq"] for _pos, ev in d.events]
        gaps += d.gap_events
    return enqueued, gaps


def test_inclusive_source_drains_fully_no_dupes():
    enq, gaps = _drain_all(_CursorSource(exclusive=False), steps=12)
    assert enq == list(range(1, 13))  # 一条不落
    assert len(enq) == len(set(enq))  # 一条不重(回退格边界被去重)
    assert gaps == 0  # 首拍流起点不算缺口(此前无已读位次)


def test_explicit_exclusive_request_offset_drains_fully():
    # 只有该来源明确钉住 offset=-1 才做偏移，不把这个特殊事实套给其他来源。
    enq, gaps = _drain_all(_CursorSource(exclusive=True), steps=12)
    assert enq == list(range(1, 13))
    assert gaps == 0


def test_explicit_exclusive_request_offset_handles_bursts():
    # 一拍多条(快产)也不漏：排他型 + 现场学得的 offset=-1 + 每拍 emit 3。
    enq, _gaps = _drain_all(_CursorSource(exclusive=True), steps=8, emit_each=3)
    assert enq == list(range(1, 25))


def test_next_cursor_source_no_double_enqueue_when_idle():
    # 空转轮(无新事件)反复拉:回退格每拍把边界那条重拉回来,必须每拍都去重,绝不重复入队。
    src = _CursorSource(exclusive=False)
    src.emit(3)
    cursor = 0
    checkpoint: dict = {}
    enq: list[int] = []
    for _ in range(5):  # 5 拍,但只有头拍有货
        budget = DrainBudget(max_events=20000, page_limit=400, deadline=time.time() + 5)
        d = drain_source(
            src.fetch,
            "http://x/pull",
            cursor,
            budget,
            source_checkpoint=checkpoint,
        )
        cursor = d.cursor
        checkpoint = dict(d.source_checkpoint or {})
        enq += [ev["seq"] for _pos, ev in d.events]
    assert enq == [1, 2, 3]  # 一条不重(空转轮不把边界条又收一遍)


def test_genuine_eviction_after_established_position_is_recorded_as_gap():
    # 滚动缓冲真淘汰仍如实记缺口(修复不能把真丢也吞掉):先建立已读位次,再让淘汰快过拉取。
    src = _CursorSource(exclusive=False, retain=3)
    src.emit(3)
    budget = DrainBudget(max_events=20000, page_limit=400, deadline=time.time() + 5)
    d0 = drain_source(
        src.fetch,
        "http://x/pull",
        0,
        budget,
        source_checkpoint={},
        source_envelope={
            "request": _REQUEST_FACTS,
            "cursor_position_semantics": "contiguous_record_ordinal",
        },
    )
    cursor = d0.cursor
    checkpoint = dict(d0.source_checkpoint or {})
    seen = [ev["seq"] for _p, ev in d0.events]
    assert seen == [1, 2, 3] and d0.gap_events == 0  # 建立位次:头三条全收、无缺口
    gaps = 0
    for _ in range(5):
        src.emit(5)  # 每拍产 5 条,只保留最近 3 条 → 每拍淘汰 2 条(建立位次之后=真丢)
        d = drain_source(
            src.fetch,
            "http://x/pull",
            cursor,
            budget,
            source_checkpoint=checkpoint,
            source_envelope={
                "request": _REQUEST_FACTS,
                "cursor_position_semantics": "contiguous_record_ordinal",
            },
        )
        cursor = d.cursor
        checkpoint = dict(d.source_checkpoint or {})
        gaps += d.gap_events
        seen += [ev["seq"] for _p, ev in d.events]
    assert gaps > 0  # 真淘汰的缺口没被吞
    missing = sorted(set(range(1, src.seq + 1)) - set(seen))
    assert missing and gaps == len(missing)  # 建立位次后每一条真丢都如实计缺口


def test_first_read_midstream_start_is_not_a_gap():
    # 流从非 1 起(或接手时游标为 0)首拍不该报幽灵缺口:此前无已读位次,谈不上漏。
    src = _CursorSource(exclusive=False)
    src.seq = 1000  # 流已经跑到 1000
    src.events = [{"seq": 1001, "event_id": "E-01001"}]
    src.seq = 1001
    budget = DrainBudget(max_events=20000, page_limit=400, deadline=time.time() + 5)
    d = drain_source(src.fetch, "http://x/pull", 0, budget)
    assert [ev["seq"] for _p, ev in d.events] == [1001]
    assert d.gap_events == 0


def test_opaque_timestamp_cursor_is_not_a_local_sequence_or_gap_counter():
    pages = {
        0: (
            [
                {"event": "a", "timestamp_ms": 1_000_000},
                {"event": "b", "timestamp_ms": 1_000_000},
                {"event": "c", "timestamp_ms": 1_000_000},
            ],
            1_000_001,
        ),
        1_000_001: (
            [
                {"event": "d", "timestamp_ms": 2_000_000},
                {"event": "e", "timestamp_ms": 2_000_000},
            ],
            2_000_001,
        ),
    }

    def fetch(request):
        query = parse_qs(urlsplit(request.url).query)
        external = int(query["since"][0])
        items, next_cursor = pages[external]
        return True, {
            "items": items,
            "next_cursor": next_cursor,
            "has_more": False,
        }, ""

    envelope = {
        "request": _REQUEST_FACTS,
        "cursor_position_semantics": "opaque",
    }
    first = drain_source(
        fetch,
        "http://x/pull",
        0,
        DrainBudget(max_events=10, page_limit=10, deadline=time.time() + 5),
        source_checkpoint={},
        source_envelope=envelope,
    )
    second = drain_source(
        fetch,
        "http://x/pull",
        first.cursor,
        DrainBudget(max_events=10, page_limit=10, deadline=time.time() + 5),
        source_checkpoint=first.source_checkpoint,
        source_envelope=envelope,
    )

    assert first.cursor == 3
    assert first.source_checkpoint == {"external_cursor": 1_000_001}
    assert [position for position, _event in first.events] == [0, 1, 2]
    assert second.cursor == 5
    assert second.source_checkpoint == {"external_cursor": 2_000_001}
    assert [position for position, _event in second.events] == [3, 4]
    assert first.gap_events == second.gap_events == 0


def test_oversized_http_page_halves_complete_record_count_until_it_fits():
    events = [{"seq": index, "body": "x" * 1000} for index in range(12)]
    requested: list[int] = []

    def fetch(request):
        url = request.url
        query = parse_qs(urlsplit(url).query)
        since = int(query["since"][0])
        limit = int(query["limit"][0])
        requested.append(limit)
        if limit > 3:
            return False, "响应体过大", "ARTIFACT_TOO_LARGE"
        items = [dict(row) for row in events if row["seq"] >= since][:limit]
        next_cursor = items[-1]["seq"] + 1 if items else len(events)
        return True, {"items": items, "next_cursor": next_cursor}, ""

    drain = drain_source(
        fetch,
        "http://x/pull",
        0,
        DrainBudget(max_events=12, page_limit=20, deadline=time.time() + 5),
    )

    assert requested[:4] == [20, 10, 5, 2]
    assert [event["seq"] for _seq, event in drain.events] == list(range(12))
    assert drain.error == ""
    assert drain.effective_page_limit == 2
    assert drain.page_limit_reductions == 3


def test_single_record_larger_than_http_ceiling_fails_without_truncation():
    requested: list[int] = []

    def fetch(request):
        url = request.url
        limit = int(parse_qs(urlsplit(url).query)["limit"][0])
        requested.append(limit)
        return False, "响应体过大", "ARTIFACT_TOO_LARGE"

    drain = drain_source(
        fetch,
        "http://x/pull",
        0,
        DrainBudget(max_events=1, page_limit=400, deadline=time.time() + 5),
    )

    assert requested[-1] == 1
    assert drain.events == []
    assert drain.error_code == "ARTIFACT_TOO_LARGE"
    assert drain.page_limit_reductions > 0


def test_non_object_page_items_keep_every_cursor_position():
    def fetch(_url: str):
        return True, {
            "items": [
                {"kind": "object"},
                "plain text record",
                ["nested", "record"],
                None,
            ],
            "next_cursor": 4,
        }, ""

    drain = drain_source(
        fetch,
        "http://x/pull",
        0,
        DrainBudget(max_events=4, page_limit=10, deadline=time.time() + 5),
    )

    assert drain.cursor == 4
    assert [seq for seq, _event in drain.events] == [0, 1, 2, 3]
    assert drain.events[1][1] == {"source_item": "plain text record"}
    assert drain.events[2][1] == {"source_item": ["nested", "record"]}
    assert drain.events[3][1] == {"source_item": None}


def test_ambiguous_page_envelope_fails_without_advancing_cursor():
    def fetch(_url: str):
        return True, {
            "alerts": [{"id": 1}],
            "metadata_rows": [{"name": "x"}],
            "next_cursor": 1,
        }, ""

    drain = drain_source(
        fetch,
        "http://x/pull",
        0,
        DrainBudget(max_events=1, page_limit=10, deadline=time.time() + 5),
    )

    assert drain.events == []
    assert drain.cursor == 0
    assert drain.error_code == "SOURCE_ENVELOPE_INVALID"


def test_invalid_next_cursor_fails_without_guessing():
    drain = drain_source(
        lambda _url: (True, {"items": [{"id": 1}], "next_cursor": "later"}, ""),
        "http://x/pull",
        0,
        DrainBudget(max_events=1, page_limit=10, deadline=time.time() + 5),
    )

    assert drain.events == []
    assert drain.cursor == 0
    assert drain.error_code == "SOURCE_ENVELOPE_INVALID"


def test_missing_next_cursor_fails_without_guessing():
    drain = drain_source(
        lambda _url: (True, {"items": [{"id": 1}]}, ""),
        "http://x/pull",
        0,
        DrainBudget(max_events=1, page_limit=10, deadline=time.time() + 5),
    )

    assert drain.events == []
    assert drain.cursor == 0
    assert drain.error_code == "SOURCE_ENVELOPE_INVALID"


def test_invalid_has_more_fails_without_advancing_cursor():
    drain = drain_source(
        lambda _url: (
            True,
            {"items": [{"id": 1}], "next_cursor": 1, "has_more": "yes"},
            "",
        ),
        "http://x/pull",
        0,
        DrainBudget(max_events=1, page_limit=10, deadline=time.time() + 5),
    )

    assert drain.events == []
    assert drain.cursor == 0
    assert drain.error_code == "SOURCE_ENVELOPE_INVALID"


def test_explicit_has_more_false_ends_even_when_page_is_full():
    drain = drain_source(
        lambda _url: (
            True,
            {
                "items": [{"seq": 0}, {"seq": 1}],
                "next_cursor": 2,
                "has_more": False,
            },
            "",
        ),
        "http://x/pull",
        0,
        DrainBudget(max_events=10, page_limit=2, deadline=time.time() + 5),
    )

    assert [event["seq"] for _seq, event in drain.events] == [0, 1]
    assert drain.cursor == 2
    assert drain.reached_end is True


def test_explicit_has_more_true_requires_progress():
    drain = drain_source(
        lambda _url: (
            True,
            {"items": [], "next_cursor": 0, "has_more": True},
            "",
        ),
        "http://x/pull",
        0,
        DrainBudget(max_events=2, page_limit=2, deadline=time.time() + 5),
    )

    assert drain.events == []
    assert drain.cursor == 0
    assert drain.error_code == "SOURCE_CURSOR_STALLED"


def test_limit_one_nonadvancing_response_fails_closed_without_looping():
    calls = {"count": 0}

    def fetch(_url: str):
        calls["count"] += 1
        return True, {"items": [{"seq": 9}], "next_cursor": 10}, ""

    drain = drain_source(
        fetch,
        "http://x/pull",
        10,
        DrainBudget(max_events=1, page_limit=1, deadline=time.time() + 5),
    )

    assert calls["count"] == 1
    assert drain.events == []
    assert drain.cursor == 10
    assert drain.reached_end is False
    assert drain.error_code == "SOURCE_CURSOR_STALLED"


def test_transient_http_failure_keeps_cursor_and_replays_same_position_after_recovery():
    calls: list[int] = []

    def failed_fetch(request):
        since = int(parse_qs(urlsplit(request.url).query)["since"][0])
        calls.append(since)
        return False, "HTTP 429: rate limited", "NETWORK_REQUEST_FAILED"

    failed = drain_source(
        failed_fetch,
        "http://x/pull",
        17,
        DrainBudget(max_events=2, page_limit=2, deadline=time.time() + 5),
    )

    assert failed.events == []
    assert failed.cursor == 17
    assert failed.error_code == "NETWORK_REQUEST_FAILED"

    def recovered_fetch(request):
        since = int(parse_qs(urlsplit(request.url).query)["since"][0])
        calls.append(since)
        return True, {
            "items": [{"seq": 17}, {"seq": 18}],
            "next_cursor": 19,
            "has_more": False,
        }, ""

    recovered = drain_source(
        recovered_fetch,
        "http://x/pull",
        failed.cursor,
        DrainBudget(max_events=2, page_limit=2, deadline=time.time() + 5),
    )

    assert calls == [17, 17]
    assert [event["seq"] for _position, event in recovered.events] == [17, 18]
    assert recovered.cursor == 19
    assert recovered.error_code == ""


def test_empty_end_page_does_not_skip_records_that_arrive_at_same_cursor_later():
    empty = drain_source(
        lambda _request: (
            True,
            {"items": [], "next_cursor": 23, "has_more": False},
            "",
        ),
        "http://x/pull",
        23,
        DrainBudget(max_events=2, page_limit=2, deadline=time.time() + 5),
    )

    assert empty.events == []
    assert empty.cursor == 23
    assert empty.reached_end is True

    later = drain_source(
        lambda request: (
            True,
            {
                "items": [{"seq": int(parse_qs(urlsplit(request.url).query)["since"][0])}],
                "next_cursor": 24,
                "has_more": False,
            },
            "",
        ),
        "http://x/pull",
        empty.cursor,
        DrainBudget(max_events=1, page_limit=1, deadline=time.time() + 5),
    )

    assert [event["seq"] for _position, event in later.events] == [23]
    assert later.cursor == 24
    assert later.error_code == ""


def test_custom_events_and_last_seen_next_cursor_drains_without_loss_or_duplicates():
    events = [{"seq": index, "event_id": f"E-{index}"} for index in range(1, 18)]
    requested_since: list[int] = []

    def fetch(request):
        url = request.url
        query = parse_qs(urlsplit(url).query)
        since = int(query["since"][0])
        limit = int(query["limit"][0])
        requested_since.append(since)
        page = [dict(row) for row in events if row["seq"] > since][:limit]
        last_seen = page[-1]["seq"] if page else max(0, since)
        return True, {"source": "demo", "events": page, "next": last_seen}, ""

    envelope = {
        "record_list_key": "events",
        "cursor_field": "next",
        "cursor_semantics": "last_seen",
        "cursor_position_semantics": "contiguous_record_ordinal",
        "request": {
            **_REQUEST_FACTS,
            "cursor_binding": {
                **_REQUEST_FACTS["cursor_binding"],
                "offset": -1,
            },
        },
    }
    cursor = 0
    checkpoint: dict = {}
    seen: list[int] = []
    for _ in range(8):
        drain = drain_source(
            fetch,
            "http://x/events",
            cursor,
            DrainBudget(max_events=5, page_limit=3, deadline=time.time() + 5),
            source_checkpoint=checkpoint,
            source_envelope=envelope,
        )
        assert drain.error_code == ""
        cursor = drain.cursor
        checkpoint = dict(drain.source_checkpoint or {})
        seen.extend(event["seq"] for _position, event in drain.events)
        if drain.reached_end:
            break

    assert requested_since[:2] == [0, 3]
    assert seen == list(range(1, 18))
    assert len(seen) == len(set(seen))
    assert cursor == 17
    assert checkpoint == {"external_cursor": 18}
