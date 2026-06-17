
from __future__ import annotations

"""职责台账单测 —— 登记/心跳/卡住检测/漏检/全局视图,多层代理值守审计性+续接的地基。"""

from pathlib import Path

from agent_py_agent.agent.tooling.log_ops.duty_registry import Assignment, DutyRegistry


def _reg(tmp_path: Path) -> DutyRegistry:
    return DutyRegistry(tmp_path / "mon")


def test_register_and_get(tmp_path: Path) -> None:
    reg = _reg(tmp_path)
    reg.register(Assignment("a1", "child-1", ["api-9001"], parent_agent_id="main"), now=100.0)
    got = reg.get("a1")
    assert got is not None
    assert got.agent_id == "child-1"
    assert got.targets == ["api-9001"]
    assert got.created_at == 100.0 and got.heartbeat_at == 100.0
    # 新对象读回(落盘续接)
    assert DutyRegistry(tmp_path / "mon").get("a1").agent_id == "child-1"


def test_heartbeat_updates_progress(tmp_path: Path) -> None:
    reg = _reg(tmp_path)
    reg.register(Assignment("a1", "child-1", ["api-9001"]), now=100.0)
    assert reg.heartbeat("a1", {"poll_cursor": 500, "alerts_found": 3}, now=200.0) is True
    got = reg.get("a1")
    assert got.heartbeat_at == 200.0
    assert got.progress["poll_cursor"] == 500 and got.progress["alerts_found"] == 3


def test_heartbeat_missing_returns_false(tmp_path: Path) -> None:
    assert _reg(tmp_path).heartbeat("nope", {}) is False


def test_stalled_detection(tmp_path: Path) -> None:
    reg = _reg(tmp_path)
    reg.register(Assignment("a1", "c1", ["api-9001"]), now=100.0)
    reg.register(Assignment("a2", "c2", ["api-9002"]), now=100.0)
    reg.heartbeat("a2", {}, now=1000.0)  # a2 心跳新
    stalled = reg.stalled(now=1000.0, stall_seconds=180.0)
    assert [a.assignment_id for a in stalled] == ["a1"]  # a1 心跳停在100,超时


def test_stalled_revives_on_heartbeat(tmp_path: Path) -> None:
    reg = _reg(tmp_path)
    reg.register(Assignment("a1", "c1", ["api-9001"], status="stalled"), now=100.0)
    reg.heartbeat("a1", {}, now=200.0)
    assert reg.get("a1").status == "active"  # 心跳回来 → 复活


def test_coverage_gaps(tmp_path: Path) -> None:
    reg = _reg(tmp_path)
    reg.register(Assignment("a1", "c1", ["api-9001", "api-9002"]), now=100.0)
    assert reg.coverage_gaps(["api-9001", "api-9002", "api-9003"]) == ["api-9003"]  # 9003 没人盯


def test_coverage_gaps_ignores_done(tmp_path: Path) -> None:
    reg = _reg(tmp_path)
    reg.register(Assignment("a1", "c1", ["api-9001"], status="done"), now=100.0)
    assert reg.coverage_gaps(["api-9001"]) == ["api-9001"]  # done 不算覆盖


def test_roster_global_view(tmp_path: Path) -> None:
    reg = _reg(tmp_path)
    reg.register(Assignment("a1", "c1", ["api-9001"], owner="userA"), now=100.0)
    reg.register(Assignment("a2", "c2", ["api-9002"]), now=100.0)
    reg.heartbeat("a2", {"alerts_found": 5}, now=1000.0)
    roster = reg.roster(["api-9001", "api-9002", "api-9003"], now=1000.0)
    assert roster["total"] == 2
    assert roster["stalled"] == ["a1"]  # a1 心跳停,超时
    assert roster["active"] == 1  # a2
    assert roster["coverage_gaps"] == ["api-9003"]
    a2 = next(e for e in roster["assignments"] if e["assignment_id"] == "a2")
    assert a2["progress"]["alerts_found"] == 5 and a2["heartbeat_age_seconds"] == 0.0


def test_all_empty(tmp_path: Path) -> None:
    assert _reg(tmp_path).all() == []


# --- 编排工具端到端(log_assign / log_heartbeat / log_duty_roster) ---

import json  # noqa: E402

from agent_py_agent.agent.tooling.log_ops.store import LogOpsStore, build_source_specs  # noqa: E402
from agent_py_agent.agent.tooling.log_ops.tools_orchestration import (  # noqa: E402
    LogAssignTool,
    LogDutyRosterTool,
    LogHeartbeatTool,
)


def test_orchestration_assign_heartbeat_roster(tmp_path: Path) -> None:
    ws = tmp_path
    store = LogOpsStore(ws / ".log_ops", "default")
    specs = build_source_specs(["http://127.0.0.1:9001/poll", "http://127.0.0.1:9002/poll"])
    store.write_config(specs, poll_interval_seconds=2.0)
    sid0, sid1 = specs[0].source_id, specs[1].source_id

    r = json.loads(
        LogAssignTool(ws)
        .execute({"agent_id": "child-1", "targets": ["http://127.0.0.1:9001/poll"], "role": "child", "parent_agent_id": "main", "owner": "userA"})
        .output
    )
    assert r["assignment_id"] == "child-1"

    hb = json.loads(LogHeartbeatTool(ws).execute({"assignment_id": "child-1", "progress": {"alerts_found": 3}}).output)
    assert hb["recorded"] is True
    assert LogHeartbeatTool(ws).execute({"assignment_id": "nope"}).ok is False  # 未登记的职责

    roster = json.loads(LogDutyRosterTool(ws).execute({}).output)
    assert roster["total"] == 1
    assert sid1 in roster["coverage_gaps"]  # 9002 没人盯 = 漏检
    assert sid0 not in roster["coverage_gaps"]  # 9001 有 child-1 盯


# --- 看门狗(扫台账→标记挂了的+重派/补派动作) ---

from agent_py_agent.agent.tooling.log_ops import watchdog  # noqa: E402
from agent_py_agent.agent.tooling.log_ops.tools_orchestration import LogWatchdogScanTool  # noqa: E402


def test_watchdog_scan_stalled_and_gaps(tmp_path: Path) -> None:
    store = LogOpsStore(tmp_path / ".log_ops", "default")
    specs = build_source_specs(["http://127.0.0.1:9001/poll", "http://127.0.0.1:9002/poll"])
    store.write_config(specs, poll_interval_seconds=2.0)
    sid0, sid1 = specs[0].source_id, specs[1].source_id
    reg = DutyRegistry(store.root)
    reg.register(Assignment("child-1", "child-1", [sid0], progress={"poll_cursor": 800}), now=100.0)  # 心跳停100
    result = watchdog.scan(store, now=1000.0, stall_seconds=180.0)
    assert not result.healthy
    assert result.stalled == ["child-1"]
    assert result.coverage_gaps == [sid1]  # sid1 没人盯
    reassign = next(a for a in result.actions if a["action"] == "reassign")
    assert reassign["agent_id"] == "child-1" and reassign["resume_from"] == {"poll_cursor": 800}  # 断点续接依据
    assert any(a["action"] == "assign_new" and a["target"] == sid1 for a in result.actions)
    assert reg.get("child-1").status == "stalled"  # 被标记


def test_watchdog_healthy(tmp_path: Path) -> None:
    store = LogOpsStore(tmp_path / ".log_ops", "default")
    specs = build_source_specs(["http://127.0.0.1:9001/poll"])
    store.write_config(specs, poll_interval_seconds=2.0)
    DutyRegistry(store.root).register(Assignment("c1", "c1", [specs[0].source_id]), now=1000.0)
    result = watchdog.scan(store, now=1000.0, stall_seconds=180.0)  # 心跳刚写,全覆盖
    assert result.healthy and result.actions == []


def test_watchdog_tool_emits_alert(tmp_path: Path) -> None:
    store = LogOpsStore(tmp_path / ".log_ops", "default")
    store.write_config(build_source_specs(["http://127.0.0.1:9001/poll"]), poll_interval_seconds=2.0)
    res = json.loads(LogWatchdogScanTool(tmp_path).execute({}).output)  # 没派代理 → 漏检 → 不健康
    assert res["healthy"] is False
    assert any(r.get("source") == "watchdog" for r in store.read_reports(min_level="P1"))  # 自动告警
