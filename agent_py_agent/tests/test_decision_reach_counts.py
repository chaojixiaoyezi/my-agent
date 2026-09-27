"""决策点到达/未触发原因计数：进程内累加、节流合并写盘、汇总与大白话诊断。"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.backends.decision_protocol import DecisionInputError, DecisionPrivacySkip
from agent_py_agent.agent.conversation import decision_reach_counts as counts
from agent_py_agent.agent.conversation.decision_reach_counts import (
    CALLED,
    decision_point_diagnostics,
    decision_reach_summary,
    flush_decision_reach_counts,
    miss_reason_label,
    note_decision_reach,
    stage_miss_reason,
)


@pytest.fixture(autouse=True)
def _fresh_counters(monkeypatch):
    monkeypatch.setattr(counts, "_PENDING", {})
    monkeypatch.setattr(counts, "_LAST_FLUSH", {})


# 函数用途: 构造带规范计数路径与开关的最小宿主。
def _agent(tmp_path, *, enabled=True):
    home = SimpleNamespace(owner_decision_reach_counts_json=tmp_path / "decision" / "reach_counts.json")
    return SimpleNamespace(home_paths=home, config=SimpleNamespace(decision_skip_records_enabled=enabled))


def test_first_reach_writes_then_later_counts_wait_in_memory_but_still_show(tmp_path, monkeypatch):
    agent = _agent(tmp_path)
    path = agent.home_paths.owner_decision_reach_counts_json
    note_decision_reach(agent, "delivery_quality", "not_test_command")
    first = json.loads(path.read_text(encoding="utf-8"))
    note_decision_reach(agent, "delivery_quality", "not_test_command")
    note_decision_reach(agent, "delivery_quality", CALLED)
    assert json.loads(path.read_text(encoding="utf-8")) == first  # 60 秒内不再写盘
    row = decision_reach_summary(agent.home_paths, since=time.time() - 3600)["points"]["delivery_quality"]
    assert (row["reached"], row["called"]) == (3, 1)
    assert row["not_called"] == [{"reason": "not_test_command", "label": miss_reason_label("not_test_command"), "count": 2}]


def test_throttled_flush_merges_into_disk_across_processes(tmp_path, monkeypatch):
    agent = _agent(tmp_path)
    path = agent.home_paths.owner_decision_reach_counts_json
    note_decision_reach(agent, "planning", "todo_count")
    # 另一个进程已写入的计数必须被合并而不是覆盖。
    payload = json.loads(path.read_text(encoding="utf-8"))
    hour = next(iter(payload["hours"]))
    payload["hours"][hour]["planning"]["todo_count"] += 5
    path.write_text(json.dumps(payload), encoding="utf-8")
    note_decision_reach(agent, "planning", "todo_count")
    clock = time.monotonic() + counts._FLUSH_SECONDS + 1
    monkeypatch.setattr(counts.time, "monotonic", lambda: clock)
    note_decision_reach(agent, "planning", CALLED)
    stored = json.loads(path.read_text(encoding="utf-8"))["hours"][hour]["planning"]
    assert stored == {"todo_count": 7, CALLED: 1}
    assert counts._PENDING.get(str(path), {}) == {}


def test_old_hours_are_pruned_and_broken_files_count_as_empty(tmp_path):
    agent = _agent(tmp_path)
    path = agent.home_paths.owner_decision_reach_counts_json
    path.parent.mkdir(parents=True)
    old = int((time.time() - 8 * 86400) // 3600) * 3600
    path.write_text(json.dumps({"schema": counts.SCHEMA, "hours": {str(old): {"recall": {"memory_count": 3}}}}), encoding="utf-8")
    note_decision_reach(agent, "recall", "memory_count")
    hours = json.loads(path.read_text(encoding="utf-8"))["hours"]
    assert str(old) not in hours and sum(point["recall"]["memory_count"] for point in hours.values()) == 1
    path.write_text("{not json", encoding="utf-8")
    assert decision_reach_summary(agent.home_paths, since=0)["points"] == {}


def test_switch_off_or_missing_path_records_nothing(tmp_path):
    agent = _agent(tmp_path, enabled=False)
    note_decision_reach(agent, "planning", "todo_count")
    assert not agent.home_paths.owner_decision_reach_counts_json.exists()
    assert counts._PENDING == {}
    note_decision_reach(SimpleNamespace(home_paths=SimpleNamespace(), config=None), "planning", "todo_count")
    assert counts._PENDING == {}
    assert decision_reach_summary(SimpleNamespace(), since=0) == {"available": False, "points": {}, "coverage_since": None}


def test_write_failure_keeps_counts_for_the_next_flush(tmp_path, monkeypatch):
    agent = _agent(tmp_path)
    original = counts._merge_file

    def broken(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(counts, "_merge_file", broken)
    note_decision_reach(agent, "curator", "nothing_to_label")
    assert sum(counts._PENDING[str(agent.home_paths.owner_decision_reach_counts_json)].values()) == 1
    monkeypatch.setattr(counts, "_merge_file", original)
    flush_decision_reach_counts()
    assert counts._PENDING.get(str(agent.home_paths.owner_decision_reach_counts_json), {}) == {}
    assert decision_reach_summary(agent.home_paths, since=0)["points"]["curator"]["reached"] == 1


def test_stage_reasons_follow_structured_stage_fields():
    stage = SimpleNamespace(error_code="", enabled_points=("planning",), run_id="run-1")
    assert stage_miss_reason(stage, "planning", "run-1") == ""
    assert stage_miss_reason(stage, "recall") == "point_off"
    assert stage_miss_reason(stage, "planning", "run-2") == "run_mismatch"
    assert stage_miss_reason(SimpleNamespace(error_code="admin_disabled", enabled_points=()), "planning") == "admin_disabled"


def test_labels_are_plain_language_and_unknown_codes_pass_through():
    assert "测试" in miss_reason_label("not_test_command")
    assert miss_reason_label("brand_new_code") == "其它原因（brand_new_code）"
    assert all(label and not label.isascii() for label in counts._LABELS.values())


def test_diagnostics_mark_uncovered_points_and_enabled_state(tmp_path):
    agent = _agent(tmp_path)
    for reason in ("focus_count", "focus_count", "nothing_to_review", CALLED):
        note_decision_reach(agent, "delivery_quality", reason)
    reach = decision_reach_summary(agent.home_paths, since=0)
    rows = decision_point_diagnostics(("delivery_quality", "model_selection", "planning"),
                                      {"delivery_quality": "apply", "model_selection": "off"}, reach)
    assert rows["delivery_quality"]["enabled"] is True and rows["delivery_quality"]["reached"] == 4
    assert [item["reason"] for item in rows["delivery_quality"]["not_called"]] == ["focus_count", "nothing_to_review"]
    assert rows["model_selection"]["covered"] is False and rows["model_selection"]["enabled"] is False
    assert rows["planning"]["covered"] is True and rows["planning"]["reached"] == 0 and rows["planning"]["mode"] == "unknown"
    unknown = decision_point_diagnostics(("planning",), None, reach)["planning"]
    assert unknown["enabled"] is False and unknown["mode"] == "unknown"


# LLM: 各点位测试共用的读数口；只隔离进程内计数并给替身宿主补上规范路径（真实宿主已有该字段，保持不动），不改点位行为。
# 函数用途: 让测试宿主开始计数，返回“读出某点位 ({原因: 次数}, 调用次数)”的函数。
def reach_counter(host, monkeypatch, point, tmp_path):
    monkeypatch.setattr(counts, "_PENDING", {})
    monkeypatch.setattr(counts, "_LAST_FLUSH", {})
    home = getattr(host, "home_paths", None)
    if getattr(home, "owner_decision_reach_counts_json", None) is None:
        fields = vars(home) if isinstance(home, SimpleNamespace) else {}
        monkeypatch.setattr(host, "home_paths", SimpleNamespace(
            **fields, owner_decision_reach_counts_json=tmp_path / "decision" / "reach_counts.json"), raising=False)

    def read():
        row = decision_reach_summary(host.home_paths, since=0)["points"].get(point) or {"not_called": [], "called": 0}
        return {item["reason"]: item["count"] for item in row["not_called"]}, row["called"]

    return read


def test_counted_material_counts_input_errors_but_not_privacy_skips(tmp_path):
    agent = _agent(tmp_path)

    def fail(error):
        def build():
            raise error
        return build

    with pytest.raises(DecisionInputError):
        counts.counted_material(agent, "recall", fail(DecisionInputError("too large")))
    with pytest.raises(DecisionPrivacySkip):
        counts.counted_material(agent, "recall", fail(DecisionPrivacySkip("url")))
    with pytest.raises(RuntimeError):
        counts.counted_material(agent, "recall", fail(RuntimeError("bug")))
    assert counts.counted_material(agent, "recall", lambda: "material") == "material"
    row = decision_reach_summary(agent.home_paths, since=0)["points"]["recall"]
    assert (row["reached"], [(item["reason"], item["count"]) for item in row["not_called"]]) == (1, [("bad_material", 1)])
