"""接管换人不丢缓冲区(spool at-least-once 交付)钉子。

真机实锤(盯守复测第二轮):子代理中途被接管换人时,继任者只从数据源游标往后续读,
没接手前任已 surface 进 spool、还没判完的候选 → 已抬升的真事成孤儿(3 条真事躺在
spool 里没人判、没上报)。根因=读游标在【交付时】就推进(at-most-once):前任拉走
一批、判完上报前死掉,这批永远不再投递。

钉死的契约:
1. 交付挂在途(inflight):同一消费者下一次 pull 才算确认判完(ack);
2. 换人重投:消费者身份变化时,前任未 ack 的在途批原样重投给继任者,不动交付游标;
3. 未判账(ack 口径):唤醒兜底/收口守卫按 written-acked 计——交付出去没确认的批
   不从账上消失;旧 sidecar(无 acked 字段)回落已交付数,历史行为不变;
4. 轮转不吃在途:有未确认在途批时 spool 不换代(重投依据不丢);
5. 工具层端到端:换 run pull 拿到的第一批就是前任的在途批(带 redelivery_note),
   open 回执亮未判积压(backlog_note),收割者起不来但 spool 有积压时仍消费 spool。
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
from agent.ingestion.audit_state import lane_unjudged_backlog
from agent.ingestion.watch_state import list_states, persist_state
from agent.ingestion.watch_state import new_state as _runtime_new_state
from agent.ingestion.watch_tool import WatchStreamTool


def new_state(*args, **kwargs):
    state = _runtime_new_state(*args, **kwargs)
    state.source_envelope = {
        "mode": "cursor",
        "record_boundary": "array_item",
        "record_list_key": "items",
        "cursor_field": "next_cursor",
        "cursor_semantics": "next_position",
        "request": {
            "method": "GET",
            "cursor_binding": {"location": "query", "name": "since", "initial": 0},
            "page_size_binding": {"location": "query", "name": "limit"},
        },
        "valid": True,
    }
    return state


class _FakeSource:
    """内存版游标源(与 test_ingestion_harvester 同款,精简)。"""

    def __init__(self) -> None:
        self.events: list[dict] = []

    def feed(self, count: int) -> None:
        base = len(self.events)
        for index in range(count):
            self.events.append({"seq": base + index, "kind": "beat", "note": f"n{base + index}"})

    def handle(self, request) -> tuple[bool, object, str]:
        from urllib.parse import parse_qs, urlsplit

        url = request.url
        query = parse_qs(urlsplit(url).query)
        since = int(query.get("since", ["0"])[0])
        limit = int(query.get("limit", ["50"])[0])
        items = [dict(event) for event in self.events if event["seq"] >= since][:limit]
        next_cursor = (items[-1]["seq"] + 1) if items else max(since, len(self.events))
        return True, {"items": items, "next_cursor": next_cursor}, ""


@pytest.fixture()
def owner_home(tmp_path, monkeypatch):
    fresh = ws.WatchRegistry()
    monkeypatch.setattr(ws, "registry", fresh)
    monkeypatch.setattr(wt, "registry", fresh)
    monkeypatch.setattr(hv, "harvesters", hv._HarvesterRegistry())
    yield tmp_path / "owner"
    # open 的 watch 会起收割线程;teardown 停掉防泄漏进后续测试。
    for watch_id in fresh.ids():
        hv.stop_harvester(watch_id)


def _harvested_state(owner_home: Path, source: _FakeSource, count: int):
    """喂 count 条进真收割管线(无 spec=冷启动整批直通),候选全部落 spool。"""
    state = new_state(owner_home, "http://src.example/pull", {})
    persist_state(state)
    source.feed(count)
    assert hv._harvest_cycle(state, source.handle)
    assert state.totals["spool_candidates"] == count
    return state


def _positions(records: list[dict]) -> list[int]:
    return [row["stream_pos"] for record in records for row in (record.get("candidates") or [])]


# ---------------------------------------------------------------------------
# 1/2/3:交付在途 → 同人 ack / 换人重投 / 未判账口径
# ---------------------------------------------------------------------------


def test_same_consumer_next_pull_acks_inflight(owner_home):
    source = _FakeSource()
    state = _harvested_state(owner_home, source, 6)
    first, _ = hv.read_spool_records(state, max_candidates=100, consumer="run-a")
    assert len(_positions(first)) == 6
    cursor = hv.read_spool_cursor(state)
    assert cursor["inflight"]["consumer"] == "run-a"
    assert cursor["inflight"]["count"] == 6
    assert hv.acked_candidates(cursor) == 0  # 交付≠判完
    # 同一消费者再来取(哪怕空手):上一批确认判完,在途清空。
    second, backlog = hv.read_spool_records(state, max_candidates=100, consumer="run-a")
    assert second == []
    cursor = hv.read_spool_cursor(state)
    assert "inflight" not in cursor
    assert hv.acked_candidates(cursor) == 6
    assert backlog["candidates_unjudged"] == 0


def test_consumer_change_redelivers_unacked_inflight(owner_home):
    source = _FakeSource()
    state = _harvested_state(owner_home, source, 5)
    first, _ = hv.read_spool_records(state, max_candidates=100, consumer="run-a")
    first_pos = _positions(first)
    # 前任(run-a)拉走后死掉,继任者(run-b)接管:同一批原样重投,交付游标不动。
    redelivered, backlog = hv.read_spool_records(state, max_candidates=100, consumer="run-b")
    assert _positions(redelivered) == first_pos
    assert backlog["redelivered_candidates"] == 5
    cursor = hv.read_spool_cursor(state)
    assert cursor["inflight"]["consumer"] == "run-b"  # 在途换主,重投只发生一次
    assert hv.acked_candidates(cursor) == 0
    # 继任者判完回来取下一批:ack 推进,消费继续(新事件照常交付)。
    source.feed(3)
    assert hv._harvest_cycle(state, source.handle)
    nxt, _ = hv.read_spool_records(state, max_candidates=100, consumer="run-b")
    assert _positions(nxt) == [5, 6, 7]
    assert hv.acked_candidates(hv.read_spool_cursor(state)) == 5


def test_unjudged_backlog_counts_inflight_until_acked(owner_home):
    source = _FakeSource()
    state = _harvested_state(owner_home, source, 4)
    hv.read_spool_records(state, max_candidates=100, consumer="run-a")
    persist_state(state)
    lane = list_states(owner_home)[0]
    # 交付出去还没确认:唤醒兜底的未判账必须还看得见这 4 条(死在判读中途不消失)。
    assert lane_unjudged_backlog(owner_home, lane) == 4
    hv.read_spool_records(state, max_candidates=100, consumer="run-a")  # ack
    assert lane_unjudged_backlog(owner_home, lane) == 0


def test_legacy_sidecar_without_acked_falls_back_to_consumed(owner_home):
    source = _FakeSource()
    state = _harvested_state(owner_home, source, 8)
    # 旧版 sidecar:只有 candidates_consumed(交付即消费的历史口径),无 acked/在途。
    sidecar = ws.state_dir(owner_home) / f"{state.watch_id}.read.json"
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text(
        json.dumps({"read_seq": 0, "offset": 0, "generation": 0, "candidates_consumed": 8, "updated_at": time.time()}),
        encoding="utf-8",
    )
    persist_state(state)
    lane = list_states(owner_home)[0]
    assert lane_unjudged_backlog(owner_home, lane) == 0  # 历史批不追溯重投
    assert hv.acked_candidates(hv.read_spool_cursor(state)) == 8


def test_rotation_skipped_while_inflight_unacked(owner_home, monkeypatch):
    source = _FakeSource()
    state = _harvested_state(owner_home, source, 3)
    hv.read_spool_records(state, max_candidates=100, consumer="run-a")  # 在途未确认
    monkeypatch.setattr(hv, "_SPOOL_ROTATE_BYTES", 1)  # 尺寸阈值必过,只考察在途闸
    source.feed(2)
    assert hv._harvest_cycle(state, source.handle)
    assert state.spool_generation == 0  # 有在途:不轮转(重投依据不丢)
    # 消费者取走新批(旧批 ack)、再空手一次把新批也 ack 掉 → 在途清空,轮转恢复。
    hv.read_spool_records(state, max_candidates=100, consumer="run-a")
    hv.read_spool_records(state, max_candidates=100, consumer="run-a")
    source.feed(1)
    assert hv._harvest_cycle(state, source.handle)
    assert state.spool_generation >= 1


def test_unrecoverable_inflight_acks_with_gap_accounting(owner_home):
    source = _FakeSource()
    state = _harvested_state(owner_home, source, 4)
    hv.read_spool_records(state, max_candidates=100, consumer="run-a")
    hv.spool_path(state).unlink()  # spool 文件没了:在途批不可恢复
    records, backlog = hv.read_spool_records(state, max_candidates=100, consumer="run-b")
    assert records == []
    assert backlog.get("redelivery_gap_candidates") == 4  # 缺口如实入账,零静默
    cursor = hv.read_spool_cursor(state)
    assert "inflight" not in cursor  # 坏在途不卡死消费
    assert hv.acked_candidates(cursor) == 4


# ---------------------------------------------------------------------------
# 5:工具层端到端(open 亮积压 / 换 run 重投批带提示 / 收割者不在仍消费积压)
# ---------------------------------------------------------------------------


def _tool(owner_home: Path, source: _FakeSource, run_id: str) -> WatchStreamTool:
    agent = SimpleNamespace(
        home_paths=SimpleNamespace(owner_home_dir=str(owner_home), owner_id="u-test"),
        _current_subagent_run_id=run_id,
    )
    tool = WatchStreamTool(agent)
    tool.allow_private_resolution = True
    tool._fetch_json = source.handle
    return tool


def test_takeover_pull_gets_predecessor_inflight_first_with_note(owner_home, monkeypatch):
    source = _FakeSource()
    source.feed(6)
    tool_a = _tool(owner_home, source, "run-a")
    opened = json.loads(tool_a.execute({"action": "open", "url": "http://127.0.0.1:9/pull?since=<next>&limit=<limit>"}).output)
    watch_id = opened["watch_id"]
    pulled_a = json.loads(tool_a.execute({"action": "pull", "watch_id": watch_id, "max_wait_seconds": 0}).output)
    assert len(pulled_a["candidates"]) == 6
    positions_a = sorted(row["stream_pos"] for row in pulled_a["candidates"])
    # 前任死了(没再 pull 确认),继任者 run-b 接管:第一批就是前任的在途批,带重投提示。
    tool_b = _tool(owner_home, source, "run-b")
    monkeypatch.setattr(wt, "registry", ws.registry)  # 同进程共享注册表(fixture 已换新)
    pulled_b = json.loads(tool_b.execute({"action": "pull", "watch_id": watch_id, "max_wait_seconds": 0}).output)
    assert sorted(row["stream_pos"] for row in pulled_b["candidates"]) == positions_a
    assert pulled_b["redelivered_candidates"] == 6
    assert "重投" in pulled_b["redelivery_note"]


def test_resumed_open_surfaces_unjudged_backlog_note(owner_home):
    source = _FakeSource()
    source.feed(5)
    tool_a = _tool(owner_home, source, "run-a")
    opened = json.loads(tool_a.execute({"action": "open", "url": "http://127.0.0.1:9/pull?since=<next>&limit=<limit>"}).output)
    watch_id = opened["watch_id"]
    tool_a.execute({"action": "pull", "watch_id": watch_id, "max_wait_seconds": 0})  # 交付在途未确认
    reopened = json.loads(tool_a.execute({"action": "open", "url": "http://127.0.0.1:9/pull?since=<next>&limit=<limit>"}).output)
    assert reopened["resumed_existing_watch"] is True
    assert reopened["spool_backlog_candidates"] == 5
    assert reopened["backlog_note"]


def test_pull_consumes_spool_backlog_even_without_harvester(owner_home, monkeypatch):
    source = _FakeSource()
    state = _harvested_state(owner_home, source, 4)
    persist_state(state)
    ws.registry.put(state)
    tool = _tool(owner_home, source, "run-b")
    # 收割者起不来(线程资源耗尽等):spool 有积压时绝不静默跳回 inline 源游标路。
    monkeypatch.setattr(wt, "registry", ws.registry)
    import agent.ingestion.harvester as harvester_mod

    monkeypatch.setattr(harvester_mod, "ensure_harvester", lambda *_a, **_k: None)
    pulled = json.loads(tool.execute({"action": "pull", "watch_id": state.watch_id, "max_wait_seconds": 0}).output)
    assert len(pulled["candidates"]) == 4
    assert pulled["harvester"]["running"] is False  # 如实呈现收割者不在
