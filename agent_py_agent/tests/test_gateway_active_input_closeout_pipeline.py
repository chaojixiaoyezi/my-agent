"""实际claimed runner的正常封口/未知出口；fake provider与临时localhost，不是native验收。"""

import pytest

from agent_py_agent.agent.backends.base import EchoBackend, ModelResponse
from agent_py_agent.agent.backends.request_scope import provider_request_budget
from agent_py_agent.agent.common.request_execution_ports import (
    RequestExecutionOutcomeUncertainError,
)
from agent_py_agent.agent.gateway_parts import request_execution
from agent_py_agent.agent.gateway_parts.multiprocess_active_input import OriginalPendingTurnInput
from agent_py_agent.agent.gateway_parts.multiprocess_admission import reservation_file
from agent_py_agent.agent.gateway_parts.request_worker import _process_claimed_gateway_request_path
from agent_py_agent.tests.test_gateway_active_input_publication import held_lock
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
    send,
    session_scope_for_request,
)


@pytest.mark.parametrize("release_busy", [True, False])
def test_actual_finally_closeout_resumes_or_preserves_unknown_and_same_p3(
    tmp_path, monkeypatch, release_busy
):
    with c_http(tmp_path, monkeypatch) as (
        agent,
        _backend,
        gate,
        _control,
        _runtime,
        server,
        _close,
    ):
        code, accepted = request(
            server,
            "POST",
            "/ask",
            {
                "goal": "合成前台任务",
                "conversation_id": "active-cid",
                "metadata": {"message_id": "closeout-original"},
            },
            headers={"X-User-Id": "local-agent", "X-Channel": "chat"},
        )
        assert code == 202 and accepted["input_state"] == "queued"
        receipt = inputs.read_gateway_input_receipt(gate.paths, accepted["request_id"])
        path = gate.paths.inbox / (receipt.request_id + ".json")
        decision = gate.admit(
            path,
            (OwnerIdentity.local_main(), session_scope_for_request(receipt.prepared_request)),
            AdmissionLimits(8, 8),
        )
        claim = claim_request(
            gate.paths, path, coordinator_snapshot=freeze_coordinator_claim(gate, decision)
        )
        assert claim
        p3 = reservation_file(gate.paths, claim.request_id)
        original_p3 = p3.read_bytes()
        calls, ingress, errors, busy = [], [], [], []
        holder = {}
        original_settle = request_execution._settle_pending_gateway_guidance
        original_queue = OriginalPendingTurnInput._queue_unseen

        def generate(_backend, _prompt, **_options):
            calls.append(1)
            return ModelResponse(text="合成任务处理完毕。", backend="echo")

        monkeypatch.setattr(EchoBackend, "generate", generate)

        def observe_queue(self, *args, **options):
            try:
                return original_queue(self, *args, **options)
            except BlockingIOError:
                busy.append(True)
                if release_busy:
                    holder["release"].set()
                raise

        monkeypatch.setattr(OriginalPendingTurnInput, "_queue_unseen", observe_queue)

        def late_input(actual_agent, request_id, failure=None, *, request_kind="ask"):
            assert actual_agent is agent and request_id == claim.request_id
            code, incoming = send(server, message="arrived-at-live-closeout")
            assert code == 202 and incoming["input_state"] == "pending"
            ingress.append(incoming["request_id"])
            with held_lock(inputs.gateway_input_transition(gate.paths, ingress[0])) as release:
                holder["release"] = release
                with provider_request_budget(0.2):
                    return original_settle(actual_agent, request_id, failure, request_kind=request_kind)

        monkeypatch.setattr(request_execution, "_settle_pending_gateway_guidance", late_input)
        finished = None
        try:
            with coordinator_execution_scope(gate, claim, agent):
                finished = _process_claimed_gateway_request_path(
                    agent, gate.paths, claim.path, claim.lease_owner, claim=claim
                )
        except BaseException as error:
            errors.append(error)
        assert busy and calls == [1]
        incoming = inputs.read_gateway_input_receipt(gate.paths, ingress[0])
        assert p3.read_bytes() == original_p3
        if release_busy:
            assert not errors and finished is True
            assert incoming.state == "queued"
            assert (
                read_json_file(gate.paths.inbox / (incoming.request_id + ".json"))
                == incoming.prepared_request
            )
            assert (
                read_json_file(gate.paths.terminal / (claim.request_id + ".json"))[
                    "terminal_response"
                ]["ok"]
                is True
            )
        else:
            assert len(errors) == 1 and type(errors[0]) is RequestExecutionOutcomeUncertainError
            assert errors[0].phase == "input.transport.retry_admission"
            current = read_json_file(claim.path)
            assert current["execution_outcome"]["status"] == "unknown"
            assert current["execution_outcome"]["phase"] == errors[0].phase
            assert not (gate.paths.terminal / (claim.request_id + ".json")).exists()
            assert (
                incoming.state == "pending"
                and not (gate.paths.inbox / (incoming.request_id + ".json")).exists()
            )
