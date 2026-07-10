"""两层限流(每用户小坑 + 全局大坑):记账 / 用户键 / 派发扫描的排队与公平。"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from agent.gateway_parts import request_worker as rw
from agent.gateway_parts.paths import GatewayPaths


def _paths(tmp_path: Path) -> GatewayPaths:
    root = tmp_path / "gw"
    return GatewayPaths(
        root=root,
        pid=root / "gateway.pid",
        adapter_pid=root / "adapter.pid",
        state=root / "gateway_state.json",
        heartbeat=root / "gateway_heartbeat.json",
        stop_request=root / "gateway_stop.request",
        log=root / "gateway.log",
        inbox=root / "requests" / "pending",
        processing=root / "requests" / "processing",
        done=root / "requests" / "done",
        failed=root / "requests" / "failed",
        responses=root / "responses",
        history=root / "history.jsonl",
    )


@pytest.fixture()
def fresh_admission(monkeypatch):
    admission = rw.GatewayAdmission()
    monkeypatch.setattr(rw, "admission", admission)
    return admission


def _enqueue(paths: GatewayPaths, request_id: str, user: str, created_at: float) -> None:
    paths.inbox.mkdir(parents=True, exist_ok=True)
    payload = {"id": request_id, "kind": "ask", "goal": "g", "user_id": user, "created_at": created_at}
    (paths.inbox / f"{request_id}.json").write_text(json.dumps(payload), encoding="utf-8")


class TestAdmissionAccounting:
    def test_user_and_global_caps_and_release(self, fresh_admission):
        admission = fresh_admission
        assert all(admission.try_acquire("u1", 2, 3) for _ in range(2))
        assert not admission.try_acquire("u1", 2, 3)  # 小坑满
        assert admission.try_acquire("u2", 2, 3)
        assert not admission.try_acquire("u3", 2, 3)  # 大坑满
        assert admission.snapshot() == {"total": 3, "per_user": {"u1": 2, "u2": 1}}
        admission.release("u1")
        assert admission.try_acquire("u3", 2, 3)  # 释放即补位
        admission.release("u9")  # 异常路径的多余释放不把账搞负
        assert admission.snapshot()["total"] == 2

    def test_request_user_key_extraction(self):
        assert rw.request_user_key({"user_id": "u-a"}) == "u-a"
        assert rw.request_user_key({"conversation": {"canonical_user_id": "u-b"}}) == "u-b"
        assert rw.request_user_key({}) == "anonymous"
        assert rw.request_user_key({"user_id": "", "conversation": {}}) == "anonymous"


class TestDispatchPending:
    def test_per_user_cap_queues_excess_and_serves_other_users(self, tmp_path, fresh_admission):
        paths = _paths(tmp_path)
        base = time.time() - 100
        for index in range(12):  # 用户 A 猛甩 12 个(都比 B 早)
            _enqueue(paths, f"a{index:02d}", "user-a", base + index)
        for index in range(2):
            _enqueue(paths, f"b{index}", "user-b", base + 50 + index)
        submitted: list[tuple[str, str]] = []
        limits = rw.AdmissionLimits(user_inflight=8, global_inflight=500)
        claimed = rw.dispatch_pending_requests(paths, limits, lambda p, u: submitted.append((p.stem, u)))
        assert claimed == 10  # A 只占到小坑 8,B 的 2 个不被 A 的积压饿死
        users = [user for _rid, user in submitted]
        assert users.count("user-a") == 8 and users.count("user-b") == 2
        assert len(list(paths.inbox.glob("*.json"))) == 4  # A 超限的 4 个留在 pending 排队
        assert len(list(paths.processing.glob("*.json"))) == 10

        # 坑释放后,下一轮扫描按顺序补位
        fresh_admission.release("user-a")
        fresh_admission.release("user-a")
        claimed_again = rw.dispatch_pending_requests(paths, limits, lambda p, u: submitted.append((p.stem, u)))
        assert claimed_again == 2
        assert len(list(paths.inbox.glob("*.json"))) == 2  # 还有 2 个等下一批坑

    def test_global_cap_binds(self, tmp_path, fresh_admission):
        paths = _paths(tmp_path)
        for index in range(5):
            _enqueue(paths, f"r{index}", f"user-{index}", time.time() - 10 + index)
        limits = rw.AdmissionLimits(user_inflight=8, global_inflight=3)
        claimed = rw.dispatch_pending_requests(paths, limits, lambda p, u: None)
        assert claimed == 3
        assert fresh_admission.snapshot()["total"] == 3

    def test_blocked_requests_force_rescan_despite_unchanged_inbox(self, tmp_path, fresh_admission):
        paths = _paths(tmp_path)
        _enqueue(paths, "r0", "user-a", time.time() - 5)
        _enqueue(paths, "r1", "user-a", time.time() - 4)
        gate = rw.GatewayInboxScanGate()
        limits = rw.AdmissionLimits(user_inflight=1, global_inflight=10)
        assert rw.dispatch_pending_requests(paths, limits, lambda p, u: None, scan_gate=gate) == 1
        time.sleep(0.05)
        # inbox 没有新写入,但还有被限流的请求在排队:扫描门必须放行(否则排队永久卡死)
        assert gate.should_scan(paths.inbox) is True
        fresh_admission.release("user-a")
        assert rw.dispatch_pending_requests(paths, limits, lambda p, u: None, scan_gate=gate) == 1

    def test_submit_failure_releases_slot(self, tmp_path, fresh_admission):
        paths = _paths(tmp_path)
        _enqueue(paths, "r0", "user-a", time.time() - 5)

        def boom(_path, _user):
            raise RuntimeError("executor down")

        with pytest.raises(RuntimeError):
            rw.dispatch_pending_requests(paths, rw.AdmissionLimits(8, 500), boom)
        assert fresh_admission.snapshot()["total"] == 0  # 提交失败不漏坑

    def test_limits_from_config_defaults_and_bad_values(self):
        class Cfg:
            gateway_user_inflight_limit = 8
            gateway_global_inflight_limit = 500

        limits = rw.AdmissionLimits.from_config(Cfg())
        assert (limits.user_inflight, limits.global_inflight) == (8, 500)

        class Bad:
            gateway_user_inflight_limit = "x"
            gateway_global_inflight_limit = 0

        fallback = rw.AdmissionLimits.from_config(Bad())
        assert (fallback.user_inflight, fallback.global_inflight) == (8, 500)


if __name__ == "__main__":
    import unittest

    unittest.main()
