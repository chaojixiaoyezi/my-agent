"""真实临时 C/W claim 与原请求未知出口；不以错误码、假 scope 或文本授予执行身份。"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_py_agent.agent.common import request_execution_ports as ports
from agent_py_agent.agent.gateway_parts import request_errors, request_execution, request_worker
from agent_py_agent.agent.gateway_parts.io import read_json_file
from agent_py_agent.agent.gateway_parts.multiprocess_admission import reservation_file
from agent_py_agent.agent.gateway_parts.multiprocess_coordinator_execution import (
    coordinator_execution_scope,
)
from agent_py_agent.agent.gateway_parts.multiprocess_execution import worker_execution_scope
from agent_py_agent.agent.gateway_parts.request_transactions import request_transaction_ports_scope
from agent_py_agent.tests.test_gateway_claim_transition_scope import (
    actual as actual,
)
from agent_py_agent.tests.test_gateway_claim_transition_scope import (
    c_actual as c_actual,
)
from agent_py_agent.tests.test_gateway_claim_transition_scope import (
    context as context,
)


# LLM: 原错误必须从真实 claim 的持久 tuple 构造；这里只注入运输错误，不创建许可或替换最终门。
# 函数用途: 产生同一已认领执行的精确未进入事实，供请求出口反例使用。
def _error(claim):
    return ports.RequestExecutionOutcomeUncertainError(read_json_file(claim.path), "actual_short_write")


@pytest.mark.parametrize("fault", ["none", "id", "attempt", "epoch", "epoch_bool", "owner", "status", "expected_id", "expected_attempt", "expected_epoch", "expected_epoch_bool", "entered", "code_spoof"])
def test_unknown_match_requires_exact_original_typed_tuple(c_actual, fault):
    _gate, claim, _agent = c_actual
    payload = read_json_file(claim.path)
    expected = [claim.request_id, claim.execution_attempt_id, claim.lease_epoch]
    error = _error(claim)
    if fault in {"id", "attempt", "owner", "status"}:
        key = {"id": "id", "attempt": "execution_attempt_id", "owner": "lease_owner", "status": "status"}[fault]
        payload[key] = "foreign"
    elif fault == "epoch":
        payload["lease_epoch"] += 1
    elif fault == "epoch_bool":
        payload["lease_epoch"] = True
    elif fault == "expected_epoch_bool":
        expected[2] = True
    elif fault.startswith("expected_"):
        index = {"expected_id": 0, "expected_attempt": 1, "expected_epoch": 2}[fault]
        expected[index] = "foreign" if index < 2 else expected[index] + 1
    elif fault == "entered":
        error.operation_entered = True
    elif fault == "code_spoof":
        error = RuntimeError("synthetic ordinary failure")
        error.error_code = ports.RequestExecutionOutcomeUncertainError.error_code
        error.operation_entered = False
    assert request_errors.request_execution_outcome_uncertain(error, payload, tuple(expected)) is (fault == "none")


def test_precise_unknown_terminalizer_keeps_processing_and_original_p3(c_actual):
    gate, claim, agent = c_actual
    error = _error(claim)
    p3 = reservation_file(gate.paths, claim.request_id)
    original_p3 = p3.read_bytes()
    original = read_json_file(claim.path)
    request_worker.terminalize_unhandled_claimed_gateway_request(
        gate.paths, claim.path, error, agent=agent,
        expected_execution_attempt_id=claim.execution_attempt_id, expected_lease_epoch=claim.lease_epoch,
    )
    current = read_json_file(claim.path)
    observation = current.pop("execution_outcome")
    assert current == original and p3.read_bytes() == original_p3
    assert observation == {"schema_version": "gateway_execution_outcome.v1", "status": "unknown",
        "request_id": claim.request_id, "execution_attempt_id": claim.execution_attempt_id,
        "lease_epoch": claim.lease_epoch, "lease_owner": claim.lease_owner,
        "error_code": error.error_code, "phase": error.phase, "operation_entered": False}
    assert not (gate.paths.terminal / claim.path.name).exists()
    assert not (gate.paths.responses / claim.path.name).exists()
    assert gate.release_settled()["released"] == 0


@pytest.mark.parametrize("role", ["C", "W"])
def test_actual_invocation_installs_original_port_and_clears_it_after_error(request, tmp_path, monkeypatch, role):
    if role == "C":
        gate, claim, agent = request.getfixturevalue("c_actual")
        paths = gate.paths
        scope = coordinator_execution_scope(gate, claim, agent)
    else:
        from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root

        binding, _payload, agent = request.getfixturevalue("actual")
        claim = binding.claim
        paths = gateway_paths_from_root(binding.snapshot.bootstrap.launch.gateway_root)
        scope = worker_execution_scope(binding, agent)
    agent.config = SimpleNamespace(sqlite_connection_reuse=False)
    marker = tmp_path / (role + "-short-write")
    failure = _error(claim)
    p3 = reservation_file(paths, claim.request_id)
    before = p3.read_bytes()

    def body(actual_agent, path, **_kwargs):
        assert actual_agent is agent and path == claim.path
        assert ports._PORTS.get() is not None
        with ports.gateway_short_transaction("actual_body"):
            with ports.gateway_transaction_writer("actual_inner"):
                marker.write_bytes(b"actual")
        raise failure

    monkeypatch.setattr(request_worker, "_handle_gateway_request", body)
    with scope:
        with pytest.raises(ports.RequestExecutionOutcomeUncertainError) as caught:
            request_worker._process_claimed_gateway_request_path(agent, paths, claim.path, claim.lease_owner, claim=claim)
    assert caught.value is failure and marker.read_bytes() == b"actual"
    assert ports._PORTS.get() is None
    assert p3.read_bytes() == before and claim.path.exists()
    assert not (paths.terminal / claim.path.name).exists()


def test_handler_unknown_skips_guidance_finalization_and_turn_end(c_actual, monkeypatch, tmp_path):
    gate, claim, agent = c_actual
    payload = read_json_file(claim.path)
    failure = _error(claim)
    response = {"id": claim.request_id, "ok": True, "status": "done"}
    request_context = {"request": payload, "request_id": claim.request_id, "response": response, "request_path": claim.path}
    forbidden = []
    seen = []
    monkeypatch.setattr(request_execution, "_prepare_gateway_request_context", lambda *_args: request_context)
    monkeypatch.setattr(request_execution, "_executing_owner_id", lambda _agent: "local/main")
    monkeypatch.setattr(request_execution, "_start_gateway_request_lease", lambda *_args: (None, None))
    monkeypatch.setattr(request_execution, "_stop_gateway_request_lease", lambda *_args: None)
    monkeypatch.setattr(request_execution, "_gateway_request_interactive_approvals", lambda *_args: False)
    monkeypatch.setattr(request_execution, "turn_started", lambda *_args, **_kwargs: "actual-event")
    for name in ("_settle_pending_gateway_guidance", "_finalize_gateway_response", "_complete_gateway_request_audit", "turn_ended"):
        monkeypatch.setattr(request_execution, name, lambda *_args, _name=name, **_kwargs: forbidden.append(_name))

    def fail_body(context, writer):
        assert writer.im_approval_agent is agent
        seen.append(context["request_id"])
        raise failure

    monkeypatch.setattr(request_execution, "_execute_gateway_request_body", fail_body)
    with coordinator_execution_scope(gate, claim, agent), request_transaction_ports_scope(agent):
        with pytest.raises(ports.RequestExecutionOutcomeUncertainError) as caught:
            request_execution._handle_gateway_request(agent, claim.path)
    assert caught.value is failure and seen == [claim.request_id]
    assert forbidden == [] and response == {"id": claim.request_id, "ok": True, "status": "done", "owner_id": "local/main"}
    assert not (gate.paths.terminal / claim.path.name).exists()


@pytest.mark.parametrize("role", ["C", "W"])
def test_actual_bounded_unknown_keeps_call_and_same_claim_with_entered_unknown(request, monkeypatch, role):
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
    from agent_py_agent.tests.test_bounded_call_error_origin import held_call

    if role == "C":
        gate, claim, agent = request.getfixturevalue("c_actual")
        paths, scope = gate.paths, coordinator_execution_scope(gate, claim, agent)
    else:
        binding, _payload, agent = request.getfixturevalue("actual")
        claim = binding.claim
        paths = gateway_paths_from_root(binding.snapshot.bootstrap.launch.gateway_root)
        scope = worker_execution_scope(binding, agent)
    p3 = reservation_file(paths, claim.request_id)
    before = p3.read_bytes()
    payload = read_json_file(claim.path)
    expected = (claim.request_id, claim.execution_attempt_id, claim.lease_epoch)
    with scope:
        with held_call(monkeypatch, key="actual-bounded-" + role) as (error, call):
            assert request_errors.bounded_request_execution_outcome_uncertain(error, payload, expected)
            request_worker.terminalize_unhandled_claimed_gateway_request(
                paths, claim.path, error, agent=agent,
                expected_execution_attempt_id=claim.execution_attempt_id, expected_lease_epoch=claim.lease_epoch,
            )
            current = read_json_file(claim.path)
            observation = current.pop("execution_outcome")
            assert current == payload and p3.read_bytes() == before
            assert observation["operation_entered"] is None
            assert observation["call_id"] == error.call_id == call.call_id
            assert observation["reason"] == error.reason and observation["phase"] == "bounded_call_exit"
            assert observation["request_id"] == claim.request_id and observation["status"] == "unknown"
            assert call.retained() and call.worker.is_alive()
            assert not (paths.terminal / claim.path.name).exists()


@pytest.mark.parametrize("fault", ["id", "attempt", "epoch", "owner", "expected", "code_spoof", "copy", "same_id", "new_execution", "empty_context"])
def test_bounded_origin_cannot_be_rebound_to_foreign_or_forged_execution(c_actual, monkeypatch, fault):
    import contextvars

    from agent_py_agent.agent.backends.bounded_call import BoundedCallExitUnconfirmedError
    from agent_py_agent.tests.test_bounded_call_error_origin import held_call

    gate, claim, agent = c_actual
    payload = read_json_file(claim.path)
    expected = (claim.request_id, claim.execution_attempt_id, claim.lease_epoch)
    before = claim.path.read_bytes(), reservation_file(gate.paths, claim.request_id).read_bytes()
    with coordinator_execution_scope(gate, claim, agent):
        with held_call(monkeypatch, key="foreign-bounded") as (original_error, _call):
            error = original_error
            if fault in {"id", "attempt", "epoch", "owner"}:
                key = {"id": "id", "attempt": "execution_attempt_id", "epoch": "lease_epoch", "owner": "lease_owner"}[fault]
                payload[key] = payload[key] + 1 if fault == "epoch" else "foreign"
            elif fault == "expected":
                expected = (claim.request_id, claim.execution_attempt_id, claim.lease_epoch + 1)
            elif fault == "code_spoof":
                error = RuntimeError("ordinary")
                error.error_code = original_error.error_code
            elif fault == "copy":
                error = type(original_error).__new__(type(original_error))
                error.__dict__.update(original_error.__dict__)
            elif fault == "same_id":
                error = BoundedCallExitUnconfirmedError(original_error.call_id, original_error.reason)
            def operation():
                return request_errors.bounded_request_execution_outcome_uncertain(error, payload, expected)
            if fault == "new_execution":
                with coordinator_execution_scope(gate, claim, agent):
                    assert operation() is False
            elif fault == "empty_context":
                assert contextvars.Context().run(operation) is False
            else:
                assert operation() is False
    assert (claim.path.read_bytes(), reservation_file(gate.paths, claim.request_id).read_bytes()) == before


def test_bounded_origin_closed_lifetime_and_reaped_call_cannot_observe_claim(c_actual, monkeypatch):
    from contextvars import copy_context

    from agent_py_agent.tests.test_bounded_call_error_origin import held_call

    gate, claim, agent = c_actual
    payload = read_json_file(claim.path)
    expected = (claim.request_id, claim.execution_attempt_id, claim.lease_epoch)
    with held_call(monkeypatch, key="without-execution") as (error, _call):
        with coordinator_execution_scope(gate, claim, agent):
            assert request_errors.bounded_request_execution_outcome_uncertain(error, payload, expected) is False
    with coordinator_execution_scope(gate, claim, agent):
        with held_call(monkeypatch, key="reaped-execution") as (error, _call):
            captured = copy_context()
            assert request_errors.bounded_request_execution_outcome_uncertain(error, payload, expected)
        assert request_errors.bounded_request_execution_outcome_uncertain(error, payload, expected) is False
    assert captured.run(request_errors.bounded_request_execution_outcome_uncertain, error, payload, expected) is False


# LLM: 第二请求沿相同真实prepare/admit/claim写者产生；不复制P3或手改执行身份，所有文件仍在原临时home。
# 函数用途: 为旧错误不能转绑另一实际claim的反例准备完整对照输入。
def _second_actual_claim(gate):
    from agent_py_agent.agent.gateway_parts import input_delivery_service as delivery
    from agent_py_agent.agent.gateway_parts.multiprocess_admission import session_scope_for_request
    from agent_py_agent.agent.gateway_parts.multiprocess_coordinator_claim import (
        freeze_coordinator_claim,
    )
    from agent_py_agent.agent.gateway_parts.queue_service import claim_request
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity

    payload = {"id": "actual-c-second", "kind": "ask", "goal": "第二合成请求", "user_id": "local-agent",
               "metadata": {"channel": "chat", "user_id": "local-agent", "client_input_digest": "second-digest"}}
    with delivery.gateway_input_transition(gate.paths, payload["id"]):
        receipt, _ = delivery.load_or_prepare_gateway_input_locked(
            gate.paths, request_id=payload["id"], client_input_digest="second-digest",
            client_message_id="", guidance_dedupe_key="", prepared_request=payload,
        )
        delivery.queue_gateway_input_locked(gate.paths, receipt)
    pending = gate.paths.inbox / (payload["id"] + ".json")
    decision = gate.admit(pending, (OwnerIdentity.local_main(), session_scope_for_request(receipt.prepared_request)),
                          request_worker.AdmissionLimits(8, 8))
    assert decision["status"] == "admitted"
    claim = claim_request(gate.paths, pending, coordinator_snapshot=freeze_coordinator_claim(gate, decision))
    assert claim is not None
    return claim


def test_old_bounded_call_cannot_bind_second_actual_request_claim(c_actual, monkeypatch):
    from agent_py_agent.tests.test_bounded_call_error_origin import held_call

    gate, first, agent = c_actual
    second = _second_actual_claim(gate)
    before = first.path.read_bytes(), second.path.read_bytes()
    with coordinator_execution_scope(gate, first, agent):
        with held_call(monkeypatch, key="first-claim-origin") as (error, call):
            with coordinator_execution_scope(gate, second, agent):
                assert request_errors.bounded_request_execution_outcome_uncertain(
                    error, read_json_file(second.path), (second.request_id, second.execution_attempt_id, second.lease_epoch),
                ) is False
            assert request_errors.bounded_request_execution_outcome_uncertain(
                error, read_json_file(first.path), (first.request_id, first.execution_attempt_id, first.lease_epoch),
            )
            assert call.retained() and call.worker.is_alive()
    assert (first.path.read_bytes(), second.path.read_bytes()) == before


def test_captured_context_cannot_extend_closed_lifetime_while_original_call_retained(c_actual, monkeypatch):
    from contextlib import ExitStack
    from contextvars import copy_context

    from agent_py_agent.tests.test_bounded_call_error_origin import held_call

    gate, claim, agent = c_actual
    payload = read_json_file(claim.path)
    expected = (claim.request_id, claim.execution_attempt_id, claim.lease_epoch)
    with ExitStack() as lifetime:
        lifetime.enter_context(coordinator_execution_scope(gate, claim, agent))
        with held_call(monkeypatch, key="closed-request-origin") as (error, call):
            captured = copy_context()
            assert request_errors.bounded_request_execution_outcome_uncertain(error, payload, expected)
            lifetime.close()
            assert captured.run(request_errors.bounded_request_execution_outcome_uncertain, error, payload, expected) is False
            assert call.retained() and call.worker.is_alive()


def test_proven_bounded_unknown_stays_unknown_when_finally_closes_original_execution(c_actual, monkeypatch):
    from contextlib import ExitStack

    from agent_py_agent.tests.test_bounded_call_error_origin import held_call

    gate, claim, agent = c_actual
    payload = read_json_file(claim.path)
    response = {"id": claim.request_id, "ok": True, "status": "done"}
    context = {"request": payload, "request_id": claim.request_id, "response": response, "request_path": claim.path}
    finished = []
    monkeypatch.setattr(request_execution, "_prepare_gateway_request_context", lambda *_args: context)
    monkeypatch.setattr(request_execution, "_executing_owner_id", lambda _agent: "local/main")
    monkeypatch.setattr(request_execution, "_start_gateway_request_lease", lambda *_args: (None, None))
    monkeypatch.setattr(request_execution, "_gateway_request_interactive_approvals", lambda *_args: False)
    monkeypatch.setattr(request_execution, "turn_started", lambda *_args, **_kwargs: "actual-event")
    monkeypatch.setattr(request_execution, "_settle_pending_gateway_guidance", lambda *_args: finished.append("guidance"))
    with ExitStack() as lifetime:
        lifetime.enter_context(coordinator_execution_scope(gate, claim, agent))
        with held_call(monkeypatch, key="cleanup-closes-source") as (error, call):
            def body(*_args):
                raise error

            monkeypatch.setattr(request_execution, "_execute_gateway_request_body", body)
            monkeypatch.setattr(request_execution, "_stop_gateway_request_lease", lambda *_args: lifetime.close())
            with pytest.raises(type(error)) as caught:
                request_execution._handle_gateway_request(agent, claim.path)
            assert caught.value is error and call.retained()
            assert finished == []


@pytest.mark.parametrize("role", ["C", "W"])
def test_invocation_records_original_unknown_before_scope_exit_and_call_reap(request, monkeypatch, role):
    from agent_py_agent.agent.backends.bounded_call import bounded_call_error_origin
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
    from agent_py_agent.tests.test_bounded_call_error_origin import held_call

    if role == "C":
        gate, claim, agent = request.getfixturevalue("c_actual")
        paths, scope = gate.paths, coordinator_execution_scope(gate, claim, agent)
    else:
        binding, _payload, agent = request.getfixturevalue("actual")
        claim = binding.claim
        paths = gateway_paths_from_root(binding.snapshot.bootstrap.launch.gateway_root)
        scope = worker_execution_scope(binding, agent)
    agent.config = SimpleNamespace(sqlite_connection_reuse=False)
    p3 = reservation_file(paths, claim.request_id)
    before = p3.read_bytes()
    with scope:
        with held_call(monkeypatch, key="outgoing-unknown-" + role) as (error, _call):
            def body(*_args, **_kwargs):
                raise error

            monkeypatch.setattr(request_worker, "_handle_gateway_request", body)
            with pytest.raises(type(error)) as caught:
                request_worker._process_claimed_gateway_request_path(agent, paths, claim.path, claim.lease_owner, claim=claim)
            assert caught.value is error
            observed = read_json_file(claim.path)["execution_outcome"]
            assert observed["call_id"] == error.call_id and observed["operation_entered"] is None
    assert bounded_call_error_origin(error) is None
    original = claim.path.read_bytes()
    request_worker.terminalize_unhandled_claimed_gateway_request(
        paths, claim.path, error, agent=agent,
        expected_execution_attempt_id=claim.execution_attempt_id, expected_lease_epoch=claim.lease_epoch,
    )
    assert claim.path.read_bytes() == original and p3.read_bytes() == before
    assert not (paths.terminal / claim.path.name).exists()


def test_unattributed_exact_bounded_unknown_never_creates_new_event_or_failed_terminal(c_actual):
    from agent_py_agent.agent.backends.bounded_call import BoundedCallExitUnconfirmedError

    gate, claim, agent = c_actual
    before = claim.path.read_bytes(), reservation_file(gate.paths, claim.request_id).read_bytes()
    error = BoundedCallExitUnconfirmedError("bounded-call:unattributed", "worker_exit_unconfirmed")
    request_worker.terminalize_unhandled_claimed_gateway_request(
        gate.paths, claim.path, error, agent=agent,
        expected_execution_attempt_id=claim.execution_attempt_id, expected_lease_epoch=claim.lease_epoch,
    )
    assert (claim.path.read_bytes(), reservation_file(gate.paths, claim.request_id).read_bytes()) == before
    assert "execution_outcome" not in read_json_file(claim.path)
    assert not (gate.paths.terminal / claim.path.name).exists()


@pytest.mark.parametrize("request_kind", [None, "ask"])
def test_foreground_settle_keeps_finish_before_legacy_reject(monkeypatch, request_kind):
    from agent_py_agent.agent.gateway_parts import multiprocess_active_input

    seen = []
    failure = RuntimeError("原业务失败")
    agent = SimpleNamespace(conversation_store=SimpleNamespace(guidance=SimpleNamespace(
        recovery=SimpleNamespace(reject_pending=lambda request_id, **kw: seen.append(("reject", request_id, kw)))
    )))
    monkeypatch.setattr(multiprocess_active_input, "finish_pending_turn_inputs",
                        lambda actual, request_id: seen.append(("finish", actual, request_id)))
    options = {} if request_kind is None else {"request_kind": request_kind}
    request_execution._settle_pending_gateway_guidance(agent, "same-request", failure, **options)
    assert seen == [("finish", agent, "same-request"),
                    ("reject", "same-request", {"reject_reserved": True, "failure": failure})]


@pytest.mark.parametrize("explicit_ask", [False, True])
def test_foreground_settle_lost_actual_ports_keeps_unknown_before_reject(c_actual, explicit_ask):
    gate, claim, agent = c_actual
    rejected = []
    agent.conversation_store = SimpleNamespace(guidance=SimpleNamespace(
        recovery=SimpleNamespace(reject_pending=lambda *args, **kw: rejected.append((args, kw)))
    ))
    with coordinator_execution_scope(gate, claim, agent):
        with request_transaction_ports_scope(agent):
            original = ports.current_request_execution_source("fixture.foreground.original")
            assert original is not None
        options = {"request_kind": "ask"} if explicit_ask else {}
        with pytest.raises(ports.RequestExecutionOutcomeUncertainError) as caught:
            request_execution._settle_pending_gateway_guidance(agent, claim.request_id, **options)
    assert caught.value.phase == "input.transport.finish.capture"
    assert caught.value.request_id == claim.request_id
    assert caught.value.execution_attempt_id == claim.execution_attempt_id
    assert caught.value.lease_epoch == claim.lease_epoch
    assert rejected == []


def test_foreground_settle_preserves_same_actual_typed_object(c_actual, monkeypatch):
    from agent_py_agent.agent.gateway_parts import multiprocess_active_input

    gate, claim, agent = c_actual
    rejected = []
    agent.conversation_store = SimpleNamespace(guidance=SimpleNamespace(
        recovery=SimpleNamespace(reject_pending=lambda *args, **kw: rejected.append((args, kw)))
    ))
    with coordinator_execution_scope(gate, claim, agent):
        with request_transaction_ports_scope(agent):
            original = ports.current_request_execution_source("fixture.foreground.original")
        with pytest.raises(ports.RequestExecutionOutcomeUncertainError) as produced:
            original.require_original("fixture.foreground.closed")
        failure = produced.value

        def finish(actual, request_id):
            assert actual is agent and request_id == claim.request_id
            raise failure

        monkeypatch.setattr(multiprocess_active_input, "finish_pending_turn_inputs", finish)
        with pytest.raises(type(failure)) as caught:
            request_execution._settle_pending_gateway_guidance(agent, claim.request_id, request_kind="ask")
    assert caught.value is failure and rejected == []


@pytest.mark.parametrize("ordinary_failure", [False, True])
def test_background_settle_keeps_legacy_reject_after_actual_ports_exit(c_actual, ordinary_failure):
    gate, claim, agent = c_actual
    rejected = []
    failure = RuntimeError("原后台业务失败") if ordinary_failure else None
    agent.conversation_store = SimpleNamespace(guidance=SimpleNamespace(
        recovery=SimpleNamespace(reject_pending=lambda *args, **kw: rejected.append((args, kw)))
    ))
    with coordinator_execution_scope(gate, claim, agent):
        with request_transaction_ports_scope(agent):
            assert ports.current_request_execution_source("fixture.background.body") is not None
        # 端口退出后仍持原C执行；若误调前台finish，真实producer必须抛typed，不能伪装None。
        request_execution._settle_pending_gateway_guidance(
            agent, claim.request_id, failure, request_kind="background"
        )
    assert rejected == [((claim.request_id,), {"reject_reserved": True, "failure": failure})]


@pytest.mark.parametrize("request_kind", ["ask", "background"])
def test_settle_keeps_legacy_ordinary_reject_failure_best_effort(monkeypatch, request_kind):
    from agent_py_agent.agent.gateway_parts import multiprocess_active_input

    seen = []

    def reject(*_args, **_kw):
        seen.append("reject")
        raise OSError("原普通guidance失败")

    agent = SimpleNamespace(conversation_store=SimpleNamespace(guidance=SimpleNamespace(
        recovery=SimpleNamespace(reject_pending=reject)
    )))
    monkeypatch.setattr(multiprocess_active_input, "finish_pending_turn_inputs",
                        lambda *_args: seen.append("finish"))
    request_execution._settle_pending_gateway_guidance(agent, "same-request", request_kind=request_kind)
    assert seen == (["finish", "reject"] if request_kind == "ask" else ["reject"])
