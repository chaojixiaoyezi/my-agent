"""判读吞吐反压(B 回炉:真机洪泛净负防回归)。

真机实锤(3h、5 路流、~250 条/秒):audit 抽检 4000+/用户把主代理判读吞吐淹了,
端到端逐条报出的真目标 156 → 18-29(崩 ~6 倍)。"多抬 ≠ 多报"——抬得再多,
判不过来就是白抬,还挤掉本该报的真目标。反压三件:
① 抽检预算按判读余量封顶(余量 = 每 pull 判读口粮 - spool 未读积压,消费即回升);
② 本 call 真车道候选先占余量,抽检只用剩余;
③ 交付端按车道价值排序(反馈最先、抽检殿后),有限判力先花在最可能真的行上。
真信号车道永不受反压限制。
"""

from __future__ import annotations

from agent_py_agent.agent.ingestion.config import IngestTuning
from agent_py_agent.agent.ingestion.engine import StreamDigestEngine
from agent_py_agent.agent.ingestion.harvester import (
    _harvest_cycle,
    judge_headroom,
    read_spool_records,
)
from agent_py_agent.agent.ingestion.watch_payloads import order_candidate_rows
from agent_py_agent.agent.ingestion.watch_state import new_state, persist_state


def _tuning(**overrides) -> IngestTuning:
    base = {"value_min_support": 16, "low_cardinality_limit": 8}
    base.update(overrides)
    return IngestTuning(**base)


def _normals(start: int, count: int) -> list[tuple[int, dict]]:
    return [(i, {"kind": "login", "status": "ok", "user": f"u{i}"}) for i in range(start, start + count)]


def _targets(start: int, count: int) -> list[tuple[int, dict]]:
    return [(i, {"kind": "login", "status": "diverted", "user": f"u{i}"}) for i in range(start, start + count)]


def test_zero_headroom_stops_audit_lane_and_books_throttle():
    # 积压满(余量 0)且涓流保底关闭时,抽检一条都不抬;钳掉的名额进 audit_throttled 账。
    engine = StreamDigestEngine(_tuning(audit_sample_per_pull=2, audit_floor_per_minute=0))
    engine.process(_normals(0, 200) + _targets(200, 40), now=1000.0)
    digest = engine.process(_normals(300, 100) + _targets(400, 20), now=1010.0, judge_headroom=0)
    assert [c for c in digest.candidates if c.reason == "audit_sample"] == []
    assert engine.totals["audit_throttled"] >= 1


def test_zero_headroom_audit_floor_trickle_keeps_flywheel_alive():
    """不足4·涓流保底(默认 1/min):真机 90 分钟 backlog 恒压死 headroom → 抽检整窗 0 条,
    反馈飞轮盲区发现断粮。floor 让钳位下每分钟仍能抬 1 条(与分钟允额共账不放大总量);
    本分钟额度用完即回到全钳,下一分钟恢复。全程 headroom=0 模拟真机恒积压形态。"""
    engine = StreamDigestEngine(_tuning(audit_sample_per_pull=2))  # 默认 floor=1
    first = engine.process(_normals(0, 200) + _targets(200, 40), now=1000.0, judge_headroom=0)
    assert len([c for c in first.candidates if c.reason == "audit_sample"]) == 1  # 涓流放 1 条
    assert engine.totals["audit_throttled"] >= 1  # 超涓流的部分仍如实记钳
    second = engine.process(_normals(300, 100) + _targets(400, 20), now=1010.0, judge_headroom=0)
    assert [c for c in second.candidates if c.reason == "audit_sample"] == []  # 同一分钟额度已用完
    third = engine.process(_normals(500, 100) + _targets(700, 20), now=1075.0, judge_headroom=0)
    assert len([c for c in third.candidates if c.reason == "audit_sample"]) == 1  # 下一分钟涓流恢复


def test_sustained_clamp_floor_holds_every_minute():
    """涓流保底的长时形态(g8 不足4 加固用例):headroom 连续多分钟恒 0(真机 90 分钟恒积压),
    每个分钟窗仍稳定放行 1 条抽检——判读下限不被反压钳到 0,反馈飞轮全程有饭吃。"""
    engine = StreamDigestEngine(_tuning(audit_sample_per_pull=2))  # 默认 floor=1/min
    engine.process(_normals(0, 200) + _targets(200, 40), now=1000.0)
    per_minute: list[int] = []
    for minute in range(5):
        now = 1060.0 + minute * 60.0
        start = 1000 + minute * 200
        digest = engine.process(_normals(start, 100) + _targets(start + 100, 20), now=now, judge_headroom=0)
        per_minute.append(len([c for c in digest.candidates if c.reason == "audit_sample"]))
    assert per_minute == [1, 1, 1, 1, 1], per_minute
    assert engine.totals["audit_throttled"] >= 5  # 超涓流的部分全程如实记钳


def test_true_lane_candidates_consume_headroom_before_audit():
    # 余量 3、本 call 真车道抬 2 条 → 抽检最多 1 条:真信号先占判力,抽检только剩余。
    engine = StreamDigestEngine(_tuning(audit_sample_per_pull=2))
    engine.process(_normals(0, 200), now=1000.0)
    batch = _normals(300, 100) + [
        (900, {"kind": "probe", "status": "ok", "user": "p1"}),
        (901, {"kind": "probe", "status": "ok", "user": "p2"}),
    ]
    digest = engine.process(batch, now=1010.0, judge_headroom=3)
    true_lane = [c for c in digest.candidates if c.reason != "audit_sample"]
    audit = [c for c in digest.candidates if c.reason == "audit_sample"]
    assert len(true_lane) == 2
    assert len(audit) == 1
    # 真车道绝不因反压被丢(哪怕余量已被自己占满)。
    zero_room = engine.process(
        [(950, {"kind": "probe2", "status": "ok", "user": "q1"})], now=1015.0, judge_headroom=0
    )
    assert [c for c in zero_room.candidates if c.reason != "audit_sample"]


def test_no_headroom_signal_keeps_legacy_behavior():
    # judge_headroom=None(inline 同步消费/离线):行为与旧版一致,抽检照常。
    engine = StreamDigestEngine(_tuning(audit_sample_per_pull=2))
    engine.process(_normals(0, 200) + _targets(200, 40), now=1000.0)
    digest = engine.process(_normals(300, 100) + _targets(400, 20), now=1010.0)
    assert len([c for c in digest.candidates if c.reason == "audit_sample"]) == 2
    assert engine.totals["audit_throttled"] == 0


def test_harvester_backpressure_roundtrip(tmp_path):
    # 端到端:收割洪泛 → spool 积压到一轮口粮即停抬 → 消费推进读游标 → 抽检自动恢复。
    state = new_state(
        tmp_path,
        "http://src.example/flood",
        {"audit_sample_per_pull": 4, "audit_sample_per_minute": 600, "audit_floor_per_minute": 0},
    )
    persist_state(state)
    feed = {"available": 0}

    def fake_fetch(url: str):
        since = int(url.split("since=")[1].split("&")[0])
        limit = int(url.split("limit=")[1].split("&")[0])
        upto = min(feed["available"], since + limit)
        items = [{"kind": "login", "status": "ok"} for _ in range(since, upto)]
        return True, {"items": items, "next_cursor": upto}, ""

    ration = state.tuning.max_candidates_per_pull
    for _cycle in range(6):
        feed["available"] += 300
        assert _harvest_cycle(state, fake_fetch)
    # 无人消费:spool 未读候选被反压钳在一轮判读口粮内,绝不无限灌。
    assert 0 < state.totals["spool_candidates"] <= ration
    assert judge_headroom(state) == ration - state.totals["spool_candidates"]
    assert state.engine.totals["audit_throttled"] >= 1
    # 消费者判完这一批(读游标推进)→ 余量回升 → 抽检恢复抬升。
    records, _backlog = read_spool_records(state, max_candidates=100)
    assert records
    assert judge_headroom(state) == ration
    before = state.totals["spool_candidates"]
    feed["available"] += 300
    assert _harvest_cycle(state, fake_fetch)
    assert state.totals["spool_candidates"] > before


def test_delivery_orders_high_value_lanes_first():
    rows = [
        {"stream_pos": 5, "triage": {"reason": "audit_sample"}},
        {"stream_pos": 9, "triage": {"reason": "confirmed_target_similar"}},
        {"stream_pos": 1, "triage": {"reason": "structurally_rare_signature"}},
        {"stream_pos": 3, "triage": {"reason": "spec_target_value"}},
        {"stream_pos": 2, "triage": {"reason": "minority_field_value"}},
        {"stream_pos": 7},  # 缺 triage 的兜底行按通用车道排,不崩
    ]
    ordered = order_candidate_rows(rows)
    assert [row["stream_pos"] for row in ordered] == [9, 3, 2, 1, 7, 5]
    # 同车道内按流序稳定。
    same_lane = order_candidate_rows(
        [
            {"stream_pos": 8, "triage": {"reason": "audit_sample"}},
            {"stream_pos": 4, "triage": {"reason": "audit_sample"}},
        ]
    )
    assert [row["stream_pos"] for row in same_lane] == [4, 8]
