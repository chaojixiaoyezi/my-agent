"""抬取背压 + 过载配速/如实标注钉子(P1 头号:别让缓冲区单向只进不出无限堆、过载不乱报)。

钉死四条契约(全结构化,零关键词):
1. content_mode(passthrough/冷启动全量直通)未读积压到上限 → 收割本拍不 drain(游标不动、
   backpressure_skips++),spool 被钳住;消费者推进读游标 → 背压释放、续抬。
2. content_mode 记录尺寸 = 一批可精读量(≤content_batch_size),模型每 pull 不再被怼整片。
3. 直通关闭(full_read_per_pull=0)的结构化降维源候选稀疏,不套背压(不误钳正常盯守)。
4. 过载(未判积压 ≥ 一个判读口粮)→ pull 载荷挂 overload 块如实标注;不过载则不挂。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from agent.ingestion import harvester as hv
from agent.ingestion import watch_state as ws
from agent.ingestion import watch_tool as wt
from agent.ingestion.config import IngestTuning
from agent.ingestion.watch_tool import WatchStreamTool


class _FakeSource:
    def __init__(self) -> None:
        self.events: list[dict] = []
        self.fail = False

    def feed(self, count: int, kind: str = "beat") -> None:
        base = len(self.events)
        for i in range(count):
            self.events.append({"seq": base + i, "kind": kind, "body": f"v{base + i}"})

    def handle(self, request) -> tuple[bool, object, str]:
        from urllib.parse import parse_qs, urlsplit

        url = request.url
        q = parse_qs(urlsplit(url).query)
        since = int(q.get("since", ["0"])[0])
        limit = int(q.get("limit", ["50"])[0])
        items = [dict(e) for e in self.events if e["seq"] >= since][:limit]
        nc = (items[-1]["seq"] + 1) if items else since
        return True, {"items": items, "next_cursor": nc}, ""


@pytest.fixture()
def owner_home(tmp_path, monkeypatch):
    fresh = ws.WatchRegistry()
    monkeypatch.setattr(ws, "registry", fresh)
    monkeypatch.setattr(wt, "registry", fresh)
    monkeypatch.setattr(hv, "harvesters", hv._HarvesterRegistry())
    return tmp_path / "owner"


def _tool(owner_home: Path, source: _FakeSource) -> WatchStreamTool:
    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=str(owner_home), owner_id="u"))
    tool = WatchStreamTool(agent)
    tool.allow_private_resolution = True
    tool._fetch_json = source.handle
    return tool


def _open(tool, **extra) -> dict:
    params = {"action": "open", "url": "http://127.0.0.1:9/pull?since=<next>&limit=<limit>", "watch_window_seconds": 1200}
    params.update(extra)
    return json.loads(tool.execute(params).output)


def _wait_until(predicate, timeout: float = 8.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def _state(owner_home: Path, watch_id: str):
    return ws.registry.get_or_load(owner_home, watch_id)


# --- 纯函数尺 -------------------------------------------------------------


def test_backpressure_and_overload_scales():
    t = IngestTuning()
    quota = hv.judge_quota(t)  # max(max_candidates_per_pull, full_read_per_pull)
    assert hv.backpressure_ceiling(t) == t.spool_backpressure_factor * quota
    assert hv.overload_threshold(t) == quota
    assert hv.content_batch_size(t) == t.full_read_per_pull


def test_backpressure_factor_zero_disables_ceiling():
    t = IngestTuning(spool_backpressure_factor=0)
    assert hv.backpressure_ceiling(t) == 0


# --- 契约1:背压钳住 spool、消费释放 ---------------------------------------


def test_content_mode_backpressure_caps_spool_without_consumer(owner_home):
    source = _FakeSource()
    source.feed(3000)  # 远超上限(默认 8×48=384)
    tool = _tool(owner_home, source)
    opened = _open(tool)  # 冷启动=content_mode,直通默认开
    state = _state(owner_home, opened["watch_id"])
    # 无人消费:收割抬到上限即停,未读积压被钳在 ceiling 附近(不是 3000 无界堆)。
    ceiling = hv.backpressure_ceiling(state.tuning)
    assert _wait_until(lambda: state.totals.get("backpressure_skips", 0) > 0, timeout=8.0)
    # 一拍最多再多灌一个 room(content_batch)才停,给一拍余量。
    assert hv.spool_unread(state) <= ceiling + hv.content_batch_size(state.tuning) + 5
    assert state.cursor < 3000  # 游标没被一次拉到底(存量没被整流倒进 spool)


def test_consumer_drain_releases_backpressure(owner_home):
    source = _FakeSource()
    source.feed(3000)
    tool = _tool(owner_home, source)
    opened = _open(tool)
    state = _state(owner_home, opened["watch_id"])
    assert _wait_until(lambda: state.totals.get("backpressure_skips", 0) > 0, timeout=8.0)
    capped_cursor = state.cursor
    # 消费一批 → 读游标推进 → 背压释放 → 收割续抬,游标越过之前的封顶点。
    for _ in range(6):
        tool.execute({"action": "pull", "watch_id": opened["watch_id"], "max_wait_seconds": 1.0})
    assert _wait_until(lambda: state.cursor > capped_cursor, timeout=8.0)


# --- 契约2:content_mode 记录尺寸有界 --------------------------------------


def test_content_mode_record_bounded_to_batch(owner_home):
    source = _FakeSource()
    source.feed(600)
    tool = _tool(owner_home, source)
    opened = _open(tool)
    state = _state(owner_home, opened["watch_id"])
    assert _wait_until(lambda: state.spool_seq >= 1, timeout=8.0)
    # 每条 spool 记录的候选数 ≤ content_batch_size(48),不再一条记录塞几百条。
    cap = hv.content_batch_size(state.tuning)
    for line in hv.spool_path(state).read_text(encoding="utf-8").splitlines():
        rec = json.loads(line)
        assert len(rec.get("candidates") or []) <= cap
    # 模型每 pull 拿到的批量也 ≤ 一个判读口粮量级。
    pulled = json.loads(tool.execute({"action": "pull", "watch_id": opened["watch_id"], "max_wait_seconds": 2.0}).output)
    assert len(pulled["candidates"]) <= hv.judge_quota(state.tuning)


# --- 契约3:结构化降维源(直通关)不套背压 --------------------------------


def test_structured_source_not_backpressured(owner_home):
    source = _FakeSource()
    source.feed(1500)
    tool = _tool(owner_home, source)
    # full_read_per_pull=0 → 非 content_mode:候选稀疏(同质 beat 压组,极少稀有),不背压。
    opened = _open(tool, full_read_per_pull=0, max_candidates_per_pull=8)
    state = _state(owner_home, opened["watch_id"])
    # 游标能一路追到底(结构化源不洪泛,不该被背压钳住)。
    assert _wait_until(lambda: state.cursor >= 1500, timeout=8.0)
    assert state.totals.get("backpressure_skips", 0) == 0


# --- 契约4:过载如实标注 --------------------------------------------------


def test_overload_note_attached_when_backlog_high(owner_home):
    source = _FakeSource()
    source.feed(2000)
    tool = _tool(owner_home, source)
    opened = _open(tool)
    state = _state(owner_home, opened["watch_id"])
    assert _wait_until(lambda: hv.spool_unread(state) >= hv.overload_threshold(state.tuning), timeout=8.0)
    pulled = json.loads(tool.execute({"action": "pull", "watch_id": opened["watch_id"], "max_wait_seconds": 1.0}).output)
    assert "overload" in pulled, "过载(积压≥一个判读口粮)必须挂 overload 如实标注"
    assert pulled["overload"]["unjudged_backlog"] >= hv.overload_threshold(state.tuning)
    assert isinstance(pulled["overload"]["backpressure_active"], bool)


def test_no_overload_note_when_caught_up(owner_home):
    source = _FakeSource()
    source.feed(5)  # 远小于一个判读口粮
    tool = _tool(owner_home, source)
    opened = _open(tool)
    state = _state(owner_home, opened["watch_id"])
    assert _wait_until(lambda: state.spool_seq >= 1, timeout=8.0)
    pulled = json.loads(tool.execute({"action": "pull", "watch_id": opened["watch_id"], "max_wait_seconds": 1.0}).output)
    assert "overload" not in pulled, "没过载不该挂 overload 块"
