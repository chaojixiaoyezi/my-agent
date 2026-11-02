"""R9-R1 P1-R9-21：Count0 准入记账后，移交原 executor 前的每个出口都恰好释放一次。

真实文件队列、claim_request 与 GatewayAdmission；按默认配置容量派发。只在原 claim/submit 调用点模拟
一次失败，不重置全局计数，也不以过期替代释放证据。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from agent_py_agent.agent.gateway_parts import request_worker as rw
from agent_py_agent.agent.gateway_parts.paths import GatewayPaths
from agent_py_agent.agent.settings import AgentConfig

EMPTY = {"total": 0, "per_user": {}, "per_conversation": {}}


class _Halt(BaseException):
    pass


def _paths(tmp_path: Path) -> GatewayPaths:
    root = tmp_path / "gw"
    return GatewayPaths(root=root, pid=root / "gateway.pid", adapter_pid=root / "adapter.pid",
                        state=root / "gateway_state.json", heartbeat=root / "gateway_heartbeat.json",
                        stop_request=root / "gateway_stop.request", log=root / "gateway.log",
                        inbox=root / "requests" / "pending", processing=root / "requests" / "processing",
                        done=root / "requests" / "done", failed=root / "requests" / "failed",
                        responses=root / "responses", history=root / "history.jsonl")


def _enqueue(paths: GatewayPaths, request_id: str, *, conversation: str = "conv-r9") -> dict:
    paths.inbox.mkdir(parents=True, exist_ok=True)
    payload = {"id": request_id, "kind": "ask", "goal": "g", "user_id": "u-r9", "status": "queued",
               "created_at": time.time(),
               "conversation": {"canonical_user_id": "u-r9", "channel": "chat",
                                "channel_conversation_id": conversation, "channel_user_id": "u-r9"}}
    (paths.inbox / f"{request_id}.json").write_text(json.dumps(payload), encoding="utf-8")
    return payload


def _keys(payload: dict) -> tuple[str, str]:
    return rw.request_user_key(payload), rw.request_conversation_key(payload)


@pytest.fixture()
def admission(monkeypatch):
    fresh = rw.GatewayAdmission()
    monkeypatch.setattr(rw, "admission", fresh)
    return fresh


@pytest.fixture()
def limits():
    configured = rw.AdmissionLimits.from_config(AgentConfig())
    assert configured.user_inflight > 1 and configured.global_inflight > 1
    return configured


def _claim_failing_once(monkeypatch, error):
    real, calls = rw.claim_request, []

    def claim(paths, request_path, **kwargs):
        calls.append(request_path.name)
        if len(calls) == 1:
            raise error
        return real(paths, request_path, **kwargs)

    monkeypatch.setattr(rw, "claim_request", claim)
    return calls


@pytest.mark.parametrize("error", [OSError("simulated claim transition failure"), _Halt()],
                         ids=["claim-oserror", "claim-base-exception"])
def test_single_claim_error_releases_admission_then_later_dispatch_proceeds(tmp_path, monkeypatch, admission,
                                                                           limits, error):
    paths = _paths(tmp_path)
    payload = _enqueue(paths, "req-r9-claim")
    calls = _claim_failing_once(monkeypatch, error)
    submitted = []
    with pytest.raises(type(error)):
        rw.dispatch_pending_requests(paths, limits, lambda claim, user, conv: submitted.append((claim, user, conv)))
    assert admission.snapshot() == EMPTY and submitted == []
    assert (paths.inbox / "req-r9-claim.json").exists()
    # 同一会话的下一轮普通派发不被泄漏的会话/用户坑挡住，并把责任移交给原 executor。
    assert rw.dispatch_pending_requests(paths, limits, lambda claim, user, conv: submitted.append((claim, user, conv))) == 1
    assert calls == ["req-r9-claim.json", "req-r9-claim.json"]
    (claim, user_key, conversation_key), = submitted
    assert (user_key, conversation_key) == _keys(payload) and claim.path == paths.processing / "req-r9-claim.json"
    assert admission.snapshot()["total"] == 1
    admission.release(user_key, conversation_key=conversation_key)  # 原 executor 的执行收尾
    assert admission.snapshot() == EMPTY


@pytest.mark.parametrize("error", [RuntimeError("simulated submit failure"), _Halt()],
                         ids=["submit-error", "submit-base-exception"])
def test_submit_failure_releases_exactly_once(tmp_path, admission, limits, error):
    paths = _paths(tmp_path)
    other = {"conversation": {"canonical_user_id": "u-r9", "channel": "chat",
                              "channel_conversation_id": "conv-r9-other", "channel_user_id": "u-r9"},
             "user_id": "u-r9"}
    other_user, other_conversation = _keys(other)
    # 同一用户另一会话仍在运行：重复释放会把它的坑一起退掉。
    assert admission.try_acquire(other_user, limits.user_inflight, limits.global_inflight,
                                 conversation_key=other_conversation)
    held = admission.snapshot()
    _enqueue(paths, "req-r9-submit")

    def submit(_claim, _user, _conversation):
        raise error

    with pytest.raises(type(error)):
        rw.dispatch_pending_requests(paths, limits, submit)
    assert admission.snapshot() == held
    # 已认领请求留在 processing 交恢复扫描，不回到 pending 被重复提交。
    assert (paths.processing / "req-r9-submit.json").exists()
    assert not (paths.inbox / "req-r9-submit.json").exists()


def test_absent_claim_releases_once_and_successful_submit_transfers_ownership(tmp_path, monkeypatch, admission,
                                                                           limits):
    paths = _paths(tmp_path)
    payload = _enqueue(paths, "req-r9-raced")
    real, calls = rw.claim_request, []

    def claim(claim_paths, request_path, **kwargs):
        calls.append(request_path.name)  # 第一次模拟别的扫描已抢先认领
        return None if len(calls) == 1 else real(claim_paths, request_path, **kwargs)

    monkeypatch.setattr(rw, "claim_request", claim)
    assert rw.dispatch_pending_requests(paths, limits, lambda *_args: pytest.fail("raced claim submitted")) == 0
    assert admission.snapshot() == EMPTY
    submitted = []
    assert rw.dispatch_pending_requests(paths, limits, lambda claim, user, conv: submitted.append((user, conv))) == 1
    # 提交成功后本函数不再释放；坑由原 executor 持有直到其收尾。
    assert submitted == [_keys(payload)] and admission.snapshot()["total"] == 1
    user_key, conversation_key = _keys(payload)
    assert admission.snapshot()["per_conversation"] == {conversation_key: 1}
    admission.release(user_key, conversation_key=conversation_key)
    assert admission.snapshot() == EMPTY
