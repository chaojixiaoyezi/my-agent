
from __future__ import annotations

"""按需唤醒门控单测 —— 确定性判断该不该拉短命 LLM 研判,跨 run 游标续接(无限期值守省烧地基)。"""

import json
from pathlib import Path

from agent_py_agent.agent.tooling.log_ops import wake
from agent_py_agent.agent.tooling.log_ops.store import LogOpsStore, build_source_specs
from agent_py_agent.agent.tooling.log_ops.tools_orchestration import LogWakeAckTool, LogWakeCheckTool


def _store(tmp_path: Path) -> LogOpsStore:
    store = LogOpsStore(tmp_path / ".log_ops", "default")
    store.write_config(build_source_specs(["/a.log"]), poll_interval_seconds=2.0)
    return store


def test_wake_no_work_when_empty(tmp_path: Path) -> None:
    d = wake.evaluate_wake(_store(tmp_path), now=1000.0)
    assert d.should_wake is False and d.reason == "no_work"


def test_wake_on_urgent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.append_urgent([{"severity": "high", "raw_line": "reverse shell"}])
    d = wake.evaluate_wake(store, now=1000.0, batch_threshold=999)  # 即使不够一批,高危也秒级唤醒
    assert d.should_wake is True and d.reason == "urgent_signal"


def test_wake_on_batch_full(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.append_candidates([{"raw_line": f"c{i}"} for i in range(20)])
    wake.write_review_state(store, cursor=0, reported=0, at=1000.0)  # 刚研判过,避开 idle 判据,只测 batch
    d = wake.evaluate_wake(store, now=1001.0, batch_threshold=20, max_idle_seconds=900)
    assert d.should_wake is True and d.reason == "batch_full"
    assert d.pending == 20


def test_wake_idle_and_no_work_cycle(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.append_candidates([{"raw_line": f"c{i}"} for i in range(5)])
    # 从没研判:有候选 → idle_timeout 保底唤醒(reviewed_at=0 → idle=inf)
    assert wake.evaluate_wake(store, now=1000.0, batch_threshold=50).reason == "idle_timeout"
    # 刚研判过且游标推进到全部 → pending=0 → no_work
    wake.write_review_state(store, cursor=5, reported=2, at=1000.0)
    assert wake.evaluate_wake(store, now=1100.0, batch_threshold=50, max_idle_seconds=900).reason == "no_work"
    # 又来 5 条但刚研判过(idle<max)且不够 batch → 不唤醒(省烧)
    store.append_candidates([{"raw_line": f"d{i}"} for i in range(5)])
    assert wake.evaluate_wake(store, now=1200.0, batch_threshold=50, max_idle_seconds=900).should_wake is False
    # 等久了(idle≥max)且有 pending → idle_timeout
    d3 = wake.evaluate_wake(store, now=2000.0, batch_threshold=50, max_idle_seconds=900)
    assert d3.should_wake is True and d3.reason == "idle_timeout"


def test_wake_tools_closed_loop(tmp_path: Path) -> None:
    """check→研判→ack 闭环:ack 推进游标+清紧急后,再 check 不重复唤醒(跨 run 续接,不重不漏)。"""
    ws = tmp_path
    store = _store(ws)
    store.append_candidates([{"raw_line": f"c{i}"} for i in range(25)])
    store.append_urgent([{"severity": "high", "raw_line": "boom"}])
    chk = json.loads(LogWakeCheckTool(ws).execute({}).output)
    assert chk["should_wake"] is True and chk["pending"] == 25
    ack = json.loads(LogWakeAckTool(ws).execute({"reported": 3}).output)
    assert ack["review_cursor"] == 25 and ack["reported"] == 3 and ack["urgent_cleared"] == 1
    chk2 = json.loads(LogWakeCheckTool(ws).execute({}).output)
    assert chk2["should_wake"] is False and chk2["reason"] == "no_work"  # pending=0 且紧急已清
