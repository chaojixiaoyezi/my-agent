
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
