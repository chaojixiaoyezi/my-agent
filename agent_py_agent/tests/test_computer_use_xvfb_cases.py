"""J16 片 C 的 Linux 车道 Xvfb 集成用例（设计稿 8.3）：关掉再开、改内容后动作过期、闪动光标误判统计。
复用 test_computer_use_xvfb_lane 的桌面与注册夹具；只在车道容器里跑（MY_AGENT_XVFB_LANE=1）。"""
from __future__ import annotations

import json
import os
import time

import pytest

from agent_py_agent.agent.plugin_observation import OBSERVATION_KEY, OBSERVATION_STALE
from agent_py_agent.tests.test_computer_use_xvfb_lane import (
    _call,
    _candidate,
    _wait,
    desktop,
    lane,
)

# pytest 按名字发现导入的夹具；这里引用一次让 ruff 知道它们在用（否则用例参数同名会报 F811）
_FIXTURES = (desktop, lane)

pytestmark = pytest.mark.skipif(os.environ.get("MY_AGENT_XVFB_LANE") != "1", reason="只在 Linux 车道容器（MY_AGENT_XVFB_LANE=1）里跑")

# 闪动光标统计的"观察 → 点击"循环次数
CARET_CYCLES_COUNT = 20


def _observe(lane, operation_id, window=None):
    outcome = _call(lane, "mcp__computer_use__observe_window", {"window": window} if window else {}, operation_id)
    assert outcome.ok, outcome.output
    return outcome


def test_closing_and_reopening_the_window_invalidates_old_candidates(lane):
    first = _observe(lane, "op-1")
    submit = _candidate(first, "Submit")
    lane.desktop.commands.write_text("reopen")
    time.sleep(2.5)
    stale = _call(lane, "mcp__computer_use__click_candidate", {"candidate_id": submit["candidate_id"]}, "op-2")
    assert (stale.ok, stale.reported_error_code, stale.effect_outcome) == (False, OBSERVATION_STALE, "not_started"), stale.output
    assert not lane.desktop.clicks.exists(), "关掉再开后旧候选没有被点"
    again = _observe(lane, "op-3")
    payload = json.loads(again.output)["structuredContent"]
    assert payload["window"] != json.loads(first.output)["structuredContent"]["window"], "新窗口是新实例"
    lane.evidence["reopen"] = {"old": json.loads(first.output)["structuredContent"]["window"], "new": payload["window"], "stale": stale.reported_error_code}


def test_changed_content_makes_the_old_candidate_stale_and_a_fresh_observation_works(lane):
    first = _observe(lane, "op-1")
    submit = _candidate(first, "Submit")
    lane.desktop.commands.write_text("relabel")
    time.sleep(0.8)
    stale = _call(lane, "mcp__computer_use__click_candidate", {"candidate_id": submit["candidate_id"]}, "op-2")
    assert (stale.ok, stale.reported_error_code) == (False, OBSERVATION_STALE), stale.output
    assert not lane.desktop.clicks.exists(), "内容变了旧候选没有被点"
    fresh = _candidate(_observe(lane, "op-3"), "Submit!")
    clicked = _call(lane, "mcp__computer_use__click_candidate", {"candidate_id": fresh["candidate_id"]}, "op-4")
    assert clicked.ok, clicked.output
    _wait(lambda: lane.desktop.clicks.exists() and lane.desktop.clicks.read_text() == "1", 10, "新候选点中")
    lane.evidence["relabel"] = {"stale": stale.reported_error_code, "fresh_label": fresh["label"], "clicks": "1"}


def test_blinking_caret_false_stale_rate_is_measured_not_tuned(lane):
    first = _observe(lane, "op-0")
    name = _candidate(first, "name")
    focus = _call(lane, "mcp__computer_use__click_candidate", {"candidate_id": name["candidate_id"]}, "op-focus")
    assert focus.ok, focus.output  # 点进输入框：光标开始在候选区域里闪
    tally = {"cycles": 0, "ok": 0, "stale": 0, "other": 0}
    for cycle in range(CARET_CYCLES_COUNT):
        candidate = _candidate(_observe(lane, f"op-c{cycle}-observe"), "name")
        clicked = _call(lane, "mcp__computer_use__click_candidate", {"candidate_id": candidate["candidate_id"]}, f"op-c{cycle}-click")
        tally["cycles"] += 1
        tally["ok" if clicked.ok else "stale" if clicked.reported_error_code == OBSERVATION_STALE else "other"] += 1
    assert tally["cycles"] == CARET_CYCLES_COUNT and tally["other"] == 0, tally
    lane.evidence["blinking_caret"] = {**tally, "false_stale_rate": round(tally["stale"] / CARET_CYCLES_COUNT, 2)}


def test_stop_interrupts_a_slow_observation_and_the_adapter_stays_usable(lane):
    import threading

    from agent_py_agent.agent.common.cancellation import CancellationToken, bind_cancellation_token

    token, result = CancellationToken(), {}

    def run():
        with bind_cancellation_token(token):
            result["outcome"] = lane.registry.tools["mcp__computer_use__observe_window"].execute({"__run_scope": {"run_id": "run-1", "task_id": "task-1"}, "__operation_id": "op-stop"})

    worker = threading.Thread(target=run, daemon=True)
    started = time.monotonic()
    worker.start()
    time.sleep(0.15)
    token.cancel("stop")  # 相当于用户 /stop：宿主停止等待并发 notifications/cancelled
    worker.join(timeout=15)
    elapsed = round(time.monotonic() - started, 2)
    outcome = result.get("outcome")
    assert outcome is not None and not outcome.ok and outcome.error_code == "CANCELLED", getattr(outcome, "output", "no outcome")
    assert elapsed < 5, f"取消后不该等 OCR 跑完：{elapsed}s"
    after_started = time.monotonic()
    again = _observe(lane, "op-after-stop")  # 适配器还活着；这次观察要排在被丢弃的那次 OCR 之后才开始（有锁），量一下等了多久
    after_seconds = round(time.monotonic() - after_started, 2)
    assert json.loads(again.output)["structuredContent"]["candidate_count"] >= 2
    lane.evidence["stop"] = {"error_code": outcome.error_code, "elapsed_seconds": elapsed, "effect_outcome": outcome.effect_outcome,
                             "after_stop_observe_seconds": after_seconds}
