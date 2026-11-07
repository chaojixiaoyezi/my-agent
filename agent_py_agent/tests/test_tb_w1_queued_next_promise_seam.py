"""TB-W1 reference: the ingress promise "queued next" survives the dispatcher.

Real C HTTP /ask -> real claimed runner -> real dispatch_pending_requests (coordinator gate), plus the real
Count0 dispatcher. Must FAIL at 173ccafe7 (EXPIRED_TARGET_TURN) and PASS after the TB-W1 fix.
Only the pytest temp home and a loopback port are used; the echo backend is patched; no real model.
"""

import pytest

from agent_py_agent.agent.backends import get_backend
from agent_py_agent.agent.backends.base import EchoBackend, ModelResponse
from agent_py_agent.agent.backends.request_scope import provider_request_budget
from agent_py_agent.agent.gateway_parts import request_execution
from agent_py_agent.agent.gateway_parts import request_worker as worker
from agent_py_agent.agent.gateway_parts.io import write_json_file_atomic
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.gateway_parts.request_errors import (
    gateway_client_error_message,
)
from agent_py_agent.agent.gateway_parts.request_worker import (
    _process_claimed_gateway_request_path,
)
from agent_py_agent.cli.chat_client_context import (
    ActiveTurnInputDelivery,
    _active_turn_input_result,
)
from agent_py_agent.tests.test_gateway_active_input_transport import (
    AdmissionLimits,
    OwnerIdentity,
    c_http,
    claim_request,
    coordinator_execution_scope,
    freeze_coordinator_claim,
    inputs,
    read_json_file,
    request,
    session_scope_for_request,
)
from agent_py_agent.tests.test_v13_w4_conversation_steer_stop import (
    _count0_steer,
    _terminal_target,
)

local_headers = {"X-User-Id": "local-agent", "X-Channel": "chat"}


def _run_first_turn(gate, agent, server, monkeypatch, *, late_input=None):
    agent.backend = get_backend("echo", agent.config)
    monkeypatch.setattr(EchoBackend, "generate", lambda _b, _p, **_o: ModelResponse(text="上一轮完成。", backend="echo"))
    code, accepted = request(server, "POST", "/ask", {
        "goal": "nihao", "conversation_id": "active-cid", "metadata": {"message_id": "turn-one"}}, headers=local_headers)
    assert code == 202 and accepted["input_state"] == "queued"
    receipt = inputs.read_gateway_input_receipt(gate.paths, accepted["request_id"])
    path = gate.paths.inbox / (receipt.request_id + ".json")
    decision = gate.admit(path, (OwnerIdentity.local_main(), session_scope_for_request(receipt.prepared_request)),
                          AdmissionLimits(8, 8))
    claim = claim_request(gate.paths, path, coordinator_snapshot=freeze_coordinator_claim(gate, decision))
    assert claim
    if late_input is not None:
        late_input(claim)
    with coordinator_execution_scope(gate, claim, agent):
        assert _process_claimed_gateway_request_path(agent, gate.paths, claim.path, claim.lease_owner, claim=claim) is True
    terminal = read_json_file(gate.paths.terminal / (claim.request_id + ".json"))
    assert terminal["terminal_response"]["ok"] is True and terminal["status"] == "done"
    return claim


def _dispatch(gate):
    submitted = []
    worker.dispatch_pending_requests(gate.paths, AdmissionLimits(8, 8), lambda *args: submitted.append(args),
                                     admission_gate=gate)
    return submitted


def _assert_promoted(gate, request_id):
    terminal = gate.paths.terminal / (request_id + ".json")
    if terminal.exists():
        assert read_json_file(terminal)["terminal_response"].get("error_code") != "EXPIRED_TARGET_TURN"
    assert (gate.paths.inbox / (request_id + ".json")).exists() or (gate.paths.processing / (request_id + ".json")).exists()


# P-a (window W4): the turn finished `done`; the next Enter still carries the finished turn id (stale client
# projection or an HTTP POST racing the closeout). route_fresh answers queued -> the dispatcher must run it.
def test_post_done_input_with_finished_turn_id_runs_as_next_turn(tmp_path, monkeypatch):
    with c_http(tmp_path, monkeypatch) as (agent, _backend, gate, _control, _runtime, server, _close):
        claim = _run_first_turn(gate, agent, server, monkeypatch)
        code, routed = request(server, "POST", "/ask", {
            "goal": "看下现在几点了", "conversation_id": "active-cid",
            "metadata": {"message_id": "steer-next", "expected_turn_id": claim.request_id}}, headers=local_headers)
        assert code == 202 and routed["input_state"] == "queued"
        assert _active_turn_input_result(code, routed).delivery is ActiveTurnInputDelivery.QUEUED
        queued = inputs.read_gateway_input_receipt(gate.paths, routed["request_id"])
        assert queued.prepared_request["metadata"]["expected_turn_id"] == claim.request_id  # producer unchanged
        _dispatch(gate)
        _assert_promoted(gate, routed["request_id"])


# P-c (window W3): the steer reaches the live closeout window; finish() -> _queue_unseen queues it for the next
# turn with the original prepared request. The dispatcher must run it.
def test_closeout_unseen_input_runs_as_next_turn(tmp_path, monkeypatch):
    with c_http(tmp_path, monkeypatch) as (agent, _backend, gate, _control, _runtime, server, _close):
        ingress = []
        original_settle = request_execution._settle_pending_gateway_guidance

        def arm(claim):
            def late_input(actual_agent, request_id, failure=None, *, request_kind="ask"):
                code, incoming = request(server, "POST", "/ask", {
                    "goal": "再补一句", "conversation_id": "active-cid",
                    "metadata": {"message_id": "steer-closeout", "expected_turn_id": claim.request_id}}, headers=local_headers)
                assert code == 202 and incoming["input_state"] == "pending"
                ingress.append(incoming["request_id"])
                with provider_request_budget(0.5):
                    return original_settle(actual_agent, request_id, failure, request_kind=request_kind)

            monkeypatch.setattr(request_execution, "_settle_pending_gateway_guidance", late_input)

        claim = _run_first_turn(gate, agent, server, monkeypatch, late_input=arm)
        queued = inputs.read_gateway_input_receipt(gate.paths, ingress[0])
        assert queued.state == "queued"
        assert queued.prepared_request["metadata"]["expected_turn_id"] == claim.request_id
        _dispatch(gate)
        _assert_promoted(gate, ingress[0])


@pytest.mark.parametrize("status", ["done", "failed"])
def test_count0_self_closed_target_dispatches(tmp_path, monkeypatch, status):
    paths = gateway_paths_from_root(tmp_path / "gateway")
    _terminal_target(paths, status)
    write_json_file_atomic(paths.inbox / "ordinary-id.json", _count0_steer())
    monkeypatch.setattr(worker, "admission", worker.GatewayAdmission())
    calls = []
    assert worker.dispatch_pending_requests(paths, worker.AdmissionLimits(1, 1), lambda *args: calls.append(args)) == 1
    assert len(calls) == 1 and calls[0][0].request_id == "ordinary-id"
    assert not (paths.terminal / "ordinary-id.json").exists()


@pytest.mark.parametrize("status", ["cancelled", "stopped", "interrupted"])
def test_stop_family_still_expires_with_typed_client_message(tmp_path, monkeypatch, status):
    paths = gateway_paths_from_root(tmp_path / "gateway")
    _terminal_target(paths, status)
    write_json_file_atomic(paths.inbox / "ordinary-id.json", _count0_steer())
    monkeypatch.setattr(worker, "admission", worker.GatewayAdmission())
    assert worker.dispatch_pending_requests(paths, worker.AdmissionLimits(1, 1), lambda *_: pytest.fail("ran")) == 0
    assert read_json_file(paths.terminal / "ordinary-id.json")["terminal_response"]["error_code"] == "EXPIRED_TARGET_TURN"
    message = gateway_client_error_message("EXPIRED_TARGET_TURN")
    assert "原状态、版本或目标" not in message and "重新发送" in message
    assert "已被停止" in message and "没有送入" in message and "没有开始新回合" in message
