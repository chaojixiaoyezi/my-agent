"""输入安全点普通资源忙的预算、停止与原typed优先；原真实scope/P3不伪造。"""

import threading
import time

import pytest

from agent_py_agent.agent.backends.request_scope import provider_request_budget
from agent_py_agent.agent.common.request_execution_ports import (
    RequestExecutionOutcomeUncertainError,
)
from agent_py_agent.agent.concurrency.interrupt import (
    interrupt_by_name,
    register_interruptible,
    set_interrupt,
)
from agent_py_agent.agent.gateway_parts.io import _locked_json_path
from agent_py_agent.agent.gateway_parts.multiprocess_admission import reservation_file
from agent_py_agent.tests.test_gateway_active_input_publication import held_lock
from agent_py_agent.tests.test_gateway_active_input_transport import (
    foreground,
    guidance,
    inputs,
    params_for,
    send,
)


def test_original_provider_budget_bounds_all_retries_without_ordinary_failed(tmp_path, monkeypatch):
    with foreground(tmp_path, monkeypatch) as (
        agent,
        backend,
        gate,
        _c,
        _r,
        server,
        claim,
        thread,
        _lane,
    ):
        code, incoming = send(server)
        assert code == 202
        path = inputs.gateway_input_receipt_path(gate.paths, incoming["request_id"])
        original = path.read_bytes()
        p3 = reservation_file(gate.paths, claim.request_id)
        original_p3 = p3.read_bytes()
        params = params_for(agent, claim, thread)
        with held_lock(_locked_json_path(path)) as release, provider_request_budget(0.15):
            started = time.monotonic()
            with pytest.raises(RequestExecutionOutcomeUncertainError) as caught:
                guidance.has_pending_turn_input(agent, params)
            assert 0.10 <= time.monotonic() - started < 1.5
            assert not release.is_set()
        assert caught.value.phase == "input.transport.retry_admission"
        assert path.read_bytes() == original and p3.read_bytes() == original_p3
        assert (
            not (gate.paths.terminal / (claim.request_id + ".json")).exists() and backend.calls == 0
        )


def test_stop_interrupts_same_wait_without_changing_input_or_p3(tmp_path, monkeypatch):
    with foreground(tmp_path, monkeypatch) as (
        agent,
        backend,
        gate,
        _c,
        _r,
        server,
        claim,
        thread,
        _lane,
    ):
        code, incoming = send(server)
        assert code == 202
        path = inputs.gateway_input_receipt_path(gate.paths, incoming["request_id"])
        params = params_for(agent, claim, thread)
        original, original_p3 = (
            path.read_bytes(),
            reservation_file(gate.paths, claim.request_id).read_bytes(),
        )
        source = params.pending_turn_input_source
        original_busy = source.require_retryable_busy
        stopped = threading.Event()

        def after_busy(actual_agent, error):
            original_busy(actual_agent, error)
            assert interrupt_by_name("active-input-test-stop")
            stopped.set()

        monkeypatch.setattr(source, "require_retryable_busy", after_busy)
        try:
            with (
                held_lock(_locked_json_path(path)),
                register_interruptible("active-input-test-stop"),
            ):
                started = time.monotonic()
                with pytest.raises(InterruptedError):
                    guidance.has_pending_turn_input(agent, params)
                assert stopped.is_set() and time.monotonic() - started < 1.0
        finally:
            set_interrupt(False)
        assert path.read_bytes() == original
        assert (
            reservation_file(gate.paths, claim.request_id).read_bytes() == original_p3
            and backend.calls == 0
        )


def test_actual_common_mi_busy_is_same_typed_error_not_a_retry(tmp_path, monkeypatch):
    with foreground(tmp_path, monkeypatch) as (
        agent,
        backend,
        _gate,
        _c,
        _r,
        _server,
        claim,
        thread,
        _lane,
    ):
        params = params_for(agent, claim, thread)
        source = params.pending_turn_input_source

        class CanonicalBusy(RequestExecutionOutcomeUncertainError, BlockingIOError):
            pass

        error = CanonicalBusy(
            {
                "id": claim.request_id,
                "execution_attempt_id": claim.execution_attempt_id,
                "lease_epoch": claim.lease_epoch,
                "lease_owner": claim.lease_owner,
            },
            "original-typed",
        )
        calls = []

        def fail(*_args, **_options):
            calls.append(1)
            raise error

        monkeypatch.setattr(source, "probe", fail)
        with pytest.raises(RequestExecutionOutcomeUncertainError) as caught:
            guidance.has_pending_turn_input(agent, params)
        assert caught.value is error and calls == [1] and backend.calls == 0


def test_ordinary_busy_error_code_does_not_sign_a_typed_source(tmp_path, monkeypatch):
    with foreground(tmp_path, monkeypatch) as (
        agent,
        backend,
        _gate,
        _c,
        _r,
        _server,
        claim,
        thread,
        _lane,
    ):
        params = params_for(agent, claim, thread)
        source = params.pending_turn_input_source
        calls = []
        error = BlockingIOError("synthetic ordinary resource")
        error.error_code = "ACTIVE_TURN_OUTCOME_UNCERTAIN"

        def once(*_args, **_options):
            calls.append(1)
            if len(calls) == 1:
                raise error
            return False

        monkeypatch.setattr(source, "probe", once)
        assert not guidance.has_pending_turn_input(agent, params)
        assert calls == [1, 1] and backend.calls == 0


def test_already_expired_provider_budget_uses_original_source_before_input_io(
    tmp_path, monkeypatch
):
    with foreground(tmp_path, monkeypatch) as (
        agent,
        backend,
        _gate,
        _c,
        _r,
        _server,
        claim,
        thread,
        _lane,
    ):
        params = params_for(agent, claim, thread)
        called = []
        monkeypatch.setattr(
            params.pending_turn_input_source, "probe", lambda *_: called.append(True)
        )
        with provider_request_budget(0):
            with pytest.raises(RequestExecutionOutcomeUncertainError) as caught:
                guidance.has_pending_turn_input(agent, params)
        assert (
            caught.value.phase == "input.transport.retry_admission"
            and called == []
            and backend.calls == 0
        )
