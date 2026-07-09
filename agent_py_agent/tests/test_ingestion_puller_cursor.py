"""游标语义无关拉取回归:harvester 抬取必须把源【抽干】,不因源把 since 当排他/包含而丢流。

真机实锤(/audit 保证档全链路):源把 ?since= 当【排他】(seq>since)解读时,旧实现按
since=next_cursor(=last+1)拉,每拍都跳过边界那条 → 每隔一条丢一条,真事入队率腰斩(10/23)。
修复:回退一格拉(since=cursor-1)+ 按已读位次去重,包含型/排他型源都一条不落、一条不重。
"""

from __future__ import annotations

import time

from agent.ingestion.puller import DrainBudget, drain_source


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

    def fetch(self, url: str) -> tuple[bool, object, str]:
        from urllib.parse import parse_qs, urlsplit

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
    enqueued: list[int] = []
    gaps = 0
    for _ in range(steps):
        src.emit(emit_each)
        budget = DrainBudget(max_events=20000, page_limit=400, deadline=time.time() + 5)
        d = drain_source(src.fetch, "http://x/pull", cursor, budget)
        cursor = d.cursor
        enqueued += [ev["seq"] for _pos, ev in d.events]
        gaps += d.gap_events
    return enqueued, gaps


def test_inclusive_source_drains_fully_no_dupes():
    enq, gaps = _drain_all(_CursorSource(exclusive=False), steps=12)
    assert enq == list(range(1, 13))  # 一条不落
    assert len(enq) == len(set(enq))  # 一条不重(回退格边界被去重)
    assert gaps == 0  # 首拍流起点不算缺口(此前无已读位次)


def test_exclusive_source_drains_fully():
    # 头号真机 bug:排他型源(seq>since)旧实现每隔一条丢一条。修复后一条不落。
    enq, gaps = _drain_all(_CursorSource(exclusive=True), steps=12)
    assert enq == list(range(1, 13))
    assert gaps == 0


def test_exclusive_source_burst_batches_drain_fully():
    # 一拍多条(快产)也不漏:排他型 + 每拍 emit 3。
    enq, _gaps = _drain_all(_CursorSource(exclusive=True), steps=8, emit_each=3)
    assert enq == list(range(1, 25))


def test_next_cursor_source_no_double_enqueue_when_idle():
    # 空转轮(无新事件)反复拉:回退格每拍把边界那条重拉回来,必须每拍都去重,绝不重复入队。
    src = _CursorSource(exclusive=False)
    src.emit(3)
    cursor = 0
    enq: list[int] = []
    for _ in range(5):  # 5 拍,但只有头拍有货
        budget = DrainBudget(max_events=20000, page_limit=400, deadline=time.time() + 5)
        d = drain_source(src.fetch, "http://x/pull", cursor, budget)
        cursor = d.cursor
        enq += [ev["seq"] for _pos, ev in d.events]
    assert enq == [1, 2, 3]  # 一条不重(空转轮不把边界条又收一遍)


def test_genuine_eviction_after_established_position_is_recorded_as_gap():
    # 滚动缓冲真淘汰仍如实记缺口(修复不能把真丢也吞掉):先建立已读位次,再让淘汰快过拉取。
    src = _CursorSource(exclusive=False, retain=3)
    src.emit(3)
    budget = DrainBudget(max_events=20000, page_limit=400, deadline=time.time() + 5)
    d0 = drain_source(src.fetch, "http://x/pull", 0, budget)
    cursor = d0.cursor
    seen = [ev["seq"] for _p, ev in d0.events]
    assert seen == [1, 2, 3] and d0.gap_events == 0  # 建立位次:头三条全收、无缺口
    gaps = 0
    for _ in range(5):
        src.emit(5)  # 每拍产 5 条,只保留最近 3 条 → 每拍淘汰 2 条(建立位次之后=真丢)
        d = drain_source(src.fetch, "http://x/pull", cursor, budget)
        cursor = d.cursor
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
