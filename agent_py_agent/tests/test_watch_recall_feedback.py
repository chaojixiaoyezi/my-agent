"""摄取召回三件套(B2 抽检 / B3 反馈学习 / B4 倾斜)防回归。

真机实锤:判 0 误报但"筛"只把 26% 喂给模型、一个源 0/177 全瞎——真目标语义上真、
结构上和常态一样(取值频次超过少数派/首记号闸),结构预筛天生盲。核心闭环:
被压常态流被抽检车道分层复读 → 模型确认(record_finding 带 watch_id+stream_pos)→
收件箱回灌结构特征 → 反馈车道自动抬同类 → 抽检向盲区源倾斜。全程零自然语言判断。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from agent_py_agent.agent.agent_core.runtime.record_finding_tool import RecordFindingTool
from agent_py_agent.agent.ingestion.config import IngestTuning
from agent_py_agent.agent.ingestion.engine import Candidate, StreamDigestEngine
from agent_py_agent.agent.ingestion.watch_feedback import (
    FeedbackState,
    append_confirmation,
    consume_feedback_inbox,
    inbox_path,
    literal_feature_key,
)
from agent_py_agent.agent.ingestion.watch_payloads import _candidate_row
from agent_py_agent.agent.ingestion.watch_state import load_state, new_state, persist_state


def _tuning(**overrides) -> IngestTuning:
    base = {"value_min_support": 16, "low_cardinality_limit": 8}
    base.update(overrides)
    return IngestTuning(**base)


def _normals(start: int, count: int) -> list[tuple[int, dict]]:
    return [(i, {"kind": "login", "status": "ok", "user": f"u{i}"}) for i in range(start, start + count)]


def _targets(start: int, count: int) -> list[tuple[int, dict]]:
    return [(i, {"kind": "login", "status": "diverted", "user": f"u{i}"}) for i in range(start, start + count)]


def test_audit_lane_samples_suppressed_groups_with_rotation():
    # 被压组按"被抽次数升序→窗口计数降序"轮换复读;每 call 有名额上限。
    engine = StreamDigestEngine(_tuning(audit_sample_per_pull=2))
    engine.process(_normals(0, 200) + _targets(200, 40), now=1000.0)
    digest = engine.process(_normals(300, 100) + _targets(400, 20), now=1010.0)
    audit = [c for c in digest.candidates if c.reason == "audit_sample"]
    assert len(audit) == 2
    # 两个不同的组各被抽一次(轮换,不是重复抽同一组)。
    assert len({c.signature for c in audit}) == 2
    assert engine.totals["audit_sampled"] >= 2


def test_audit_budget_bounded_by_minute_allowance():
    engine = StreamDigestEngine(_tuning(audit_sample_per_pull=4, audit_sample_per_minute=6))
    for call in range(5):
        engine.process(_normals(call * 300, 260), now=1000.0 + call)
    # 同一分钟窗内合计不超过每分钟允额。
    assert engine.totals["audit_sampled"] <= 6


def test_audit_disabled_by_zero():
    engine = StreamDigestEngine(_tuning(audit_sample_per_pull=0))
    digest = engine.process(_normals(0, 300), now=1000.0)
    assert [c for c in digest.candidates if c.reason == "audit_sample"] == []


def test_confirmation_roundtrip_learns_and_lifts_similar(tmp_path):
    # 端到端盲区救活:目标与常态共形状、目标取值频次超少数派闸 → 全被压;
    # 抽检复读被模型确认 → 特征回灌 → 后续同类经反馈车道自动抬升。
    state = new_state(tmp_path, "http://src.example/stream", {"value_min_support": 16, "low_cardinality_limit": 8})
    persist_state(state)
    engine = state.engine
    # 预热 + 目标洪泛(diverted 窗口计数 >> value_rare_threshold → 少数派车道盲)。
    engine.process(_normals(0, 300) + _targets(300, 40), now=1000.0)
    digest = engine.process(_normals(400, 100) + _targets(500, 30), now=1010.0)
    lifted_before = [c for c in digest.candidates if c.reason == "confirmed_target_similar"]
    assert lifted_before == []
    audit_targets = [
        c for c in digest.candidates
        if c.reason == "audit_sample" and c.event.get("status") == "diverted"
    ]
    assert audit_targets, "抽检车道必须能复读到目标所在的被压组"
    # 模型确认这条抽检样本(record_finding 对账通路的收件箱侧)。
    assert append_confirmation(tmp_path, state.watch_id, audit_targets[0].seq_hint)
    consumed = consume_feedback_inbox(state, now=1020.0)
    assert consumed == 1
    assert engine.totals["audit_confirmed"] == 1
    key = literal_feature_key("status", "s:diverted")
    assert key in engine.feedback.active_keys()
    # 后续同特征事件全部经反馈车道抬升(不受稀有闸限制)。
    digest2 = engine.process(_normals(700, 60) + _targets(800, 6), now=1030.0)
    lifted = [c for c in digest2.candidates if c.reason == "confirmed_target_similar"]
    assert len(lifted) == 6
    assert all(c.event.get("status") == "diverted" for c in lifted)
    assert lifted[0].value_path == "status"
    assert engine.totals["escalated_feedback"] == 6


def test_feedback_lane_flood_bounded_per_feature_window():
    engine = StreamDigestEngine(_tuning(feedback_feature_window_cap=5, feedback_max_candidates_per_pull=50))
    engine.process(_normals(0, 300), now=1000.0)
    engine.feedback.register_confirmed([literal_feature_key("status", "s:diverted")], now=1005.0)
    digest = engine.process(_targets(400, 20), now=1010.0)
    lifted = [c for c in digest.candidates if c.reason == "confirmed_target_similar"]
    assert len(lifted) == 5  # 每特征每窗口洪泛闸:确认特征配到常态取值时污染有上限


def test_feature_auto_retire_and_revive():
    state = FeedbackState()
    key = "v\x1estatus\x1es:noise"
    state.register_confirmed([key], now=1000.0)
    for _ in range(64):
        state.record_lift(key, now=1000.0 + 700.0, retire_min_lifted=64, window_seconds=300)
    assert key not in state.active_keys()  # 抬了 64 次仍只有注册那一次确认且已过 stale 窗 → 退休
    state.register_confirmed([key], now=2000.0)
    assert key in state.active_keys()  # 新确认自动复活


def test_tilt_raises_allowance_after_audit_confirm():
    # 每被压组每 call 只有一个 exemplar 可复读 → 用 6 种形状造 6 个组来观察名额档位。
    tuning = _tuning(audit_sample_per_pull=2, audit_tilt_per_pull=6, audit_sample_per_minute=6, audit_tilt_per_minute=24)
    engine = StreamDigestEngine(tuning)

    def _mixed(start: int) -> list[tuple[int, dict]]:
        return [
            (start + i, {"kind": f"k{i % 6}", "status": "ok", "user": f"u{start + i}"})
            for i in range(360)
        ]

    engine.process(_mixed(0), now=1000.0)
    baseline = engine.totals["audit_sampled"]
    assert baseline <= 2  # base 档:每 call 上限 2
    engine.totals["audit_confirmed"] = 1  # 盲区证据:抽检样本被确认过
    digest = engine.process(_mixed(1000), now=1200.0)
    audit = [c for c in digest.candidates if c.reason == "audit_sample"]
    assert len(audit) > 2  # 倾斜档名额生效(>base 每 call 上限)


def test_feedback_state_survives_persist_and_load(tmp_path):
    state = new_state(tmp_path, "http://src.example/stream", {})
    engine = state.engine
    engine.process(_normals(0, 200), now=1000.0)
    engine.feedback.register_confirmed([literal_feature_key("status", "s:diverted")], now=1000.0)
    engine.feedback.remember_position(123, ["v\x1estatus\x1es:x"], audit=True)
    state.feedback_offset = 57
    persist_state(state)
    loaded = load_state(tmp_path, state.watch_id)
    assert loaded is not None
    assert literal_feature_key("status", "s:diverted") in loaded.engine.feedback.active_keys()
    assert loaded.engine.feedback.ring.get(123, {}).get("a") == 1
    assert loaded.feedback_offset == 57


def test_ring_miss_counted_not_crashing(tmp_path):
    state = new_state(tmp_path, "http://src.example/stream", {})
    persist_state(state)
    assert append_confirmation(tmp_path, state.watch_id, 99999)
    consume_feedback_inbox(state, now=1000.0)
    assert state.engine.totals["feedback_ring_miss"] == 1
    assert state.engine.totals["feedback_confirmed_seen"] == 1


def test_append_confirmation_requires_existing_watch(tmp_path):
    assert not append_confirmation(tmp_path, "ws-0123456789", 5)
    assert not inbox_path(tmp_path, "ws-0123456789").exists()


def test_candidate_row_renders_audit_and_feedback_triage():
    audit_row = _candidate_row(
        Candidate(7, {"a": 1}, "sig", 40, False, 9, reason="audit_sample", audit_group_count=12)
    )
    assert audit_row["triage"]["audit_sample"] == {"group_window_count": 40, "count_this_call": 12}
    fb_row = _candidate_row(
        Candidate(
            8, {"a": 2}, "sig", 40, False, 9,
            reason="confirmed_target_similar",
            value_path="status", value_token="s:diverted",
            value_window_count=17, field_window_count=300,
        )
    )
    assert fb_row["triage"]["feedback_match"]["path"] == "status"
    assert fb_row["triage"]["feedback_match"]["value_window_count"] == 17


def test_record_finding_links_watch_feedback(tmp_path):
    home = tmp_path / "home"
    task_root = tmp_path / "task"
    (task_root / "work" / "shared").mkdir(parents=True)
    state = new_state(home, "http://src.example/stream", {})
    persist_state(state)
    agent = SimpleNamespace(
        home_paths=SimpleNamespace(owner_home_dir=str(home)),
        _current_run_task_workspace=str(task_root),
        subagents=None,
    )
    tool = RecordFindingTool(agent)
    result = tool.execute({"claim": "事件 evt-1 结果端 diverted 确认", "watch_id": state.watch_id, "stream_pos": 42})
    assert result.ok
    payload = json.loads(result.output)
    assert payload["watch_feedback_linked"] is True
    lines = inbox_path(home, state.watch_id).read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[0])["pos"] == 42


def test_record_finding_without_watch_params_unchanged(tmp_path):
    task_root = tmp_path / "task"
    (task_root / "work" / "shared").mkdir(parents=True)
    agent = SimpleNamespace(
        home_paths=SimpleNamespace(owner_home_dir=str(tmp_path)),
        _current_run_task_workspace=str(task_root),
        subagents=None,
    )
    result = RecordFindingTool(agent).execute({"claim": "普通结论"})
    assert result.ok
    assert "watch_feedback_linked" not in json.loads(result.output)


def test_record_finding_rejects_malformed_watch_id(tmp_path):
    task_root = tmp_path / "task"
    (task_root / "work" / "shared").mkdir(parents=True)
    agent = SimpleNamespace(
        home_paths=SimpleNamespace(owner_home_dir=str(tmp_path)),
        _current_run_task_workspace=str(task_root),
        subagents=None,
    )
    result = RecordFindingTool(agent).execute({"claim": "x", "watch_id": "../../etc", "stream_pos": 1})
    assert result.ok  # 结论账主通道不受影响
    assert json.loads(result.output)["watch_feedback_linked"] is False
