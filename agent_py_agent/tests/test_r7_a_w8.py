"""辅助调用的真实宿主收口：保留累计事实，只释放已完成的逻辑身份。"""
from __future__ import annotations

from contextvars import copy_context
from dataclasses import replace
from threading import Event, Thread
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.model import llm_metrics
from agent_py_agent.agent.agent_core.model.call_runtime import record_model_call_finished
from agent_py_agent.agent.backends import ModelResponse, gateway_helpers
from agent_py_agent.agent.common.cancellation import (
    CancellationToken,
    ToolCancelled,
    bind_cancellation_token,
)
from agent_py_agent.agent.contracts.model_call_ledger import ModelCallLedger
from agent_py_agent.agent.conversation import auxiliary_model_call as service
from agent_py_agent.agent.settings import AgentConfig


class _OfflineBackend:
    name = "offline-auxiliary"
    model_name = "offline-model"
    context_window_tokens = 4096
    max_tokens = 128

    def __init__(self):
        self.calls = 0
        self.operation = None
        self.response = ModelResponse(text="摘要", backend=self.name, usage={"input_tokens": 7, "output_tokens": 3})

    def generate(self, prompt, *, on_chunk=None, request_options=None):
        self.calls += 1
        gateway_helpers._emit_provider_attempt({"attempt_id": f"http-{self.calls}", "status": "started"})
        if on_chunk is not None:
            on_chunk("摘要")
        return self.operation(on_chunk) if self.operation is not None else self.response


@pytest.fixture
def aux_request(monkeypatch):
    # 仅替换遥测出口；生成、准入、预算、取消、调用账本和观察者都走真实实现。
    metrics = {"calls": [], "cost": [], "run_cost": []}
    monkeypatch.setattr(service, "record_llm_call", lambda *a, **kw: metrics["calls"].append(kw["ok"]))
    monkeypatch.setattr(llm_metrics, "record_llm_cost", lambda model, response: metrics["cost"].append((model, response)))
    monkeypatch.setattr(llm_metrics, "record_run_cost", lambda *args: metrics["run_cost"].append(args))
    agent = SimpleNamespace(
        backend=_OfflineBackend(),
        config=AgentConfig(enable_tools=False, model_name="offline-model", max_tokens=128, my_agent_owner_id="offline-owner",
                           model_context_window_tokens=4096, memory_compact_summary_max_output_tokens=64),
        _model_call_ledger=ModelCallLedger(),
        metrics=metrics,
    )
    return service.AuxiliaryModelCallRequest(agent, "离线摘要", request_id="request", run_id="run",
                                             purpose="conversation_compact_summary")


def _assert_closed(aux_request, count):
    ledger = aux_request.agent._model_call_ledger
    for aggregate in ledger._scope_aggregates.values():
        for bucket in (aggregate, aggregate.purpose_totals["auxiliary"]):
            assert not bucket.logical_call_counts
            assert bucket.closed_logical_call_count == count
        assert not aggregate.retry_logical_call_ids
    for scope in ({"request_id": aux_request.request_id}, {"run_id": aux_request.run_id}):
        if not next(iter(scope.values())):
            continue
        summary = ledger.cumulative_summary(**scope)
        if not count:
            assert summary is None
            continue
        assert summary["logical_model_turn_count"] == summary["physical_model_attempt_count"] == count
        assert summary["purpose_breakdown"]["auxiliary"]["logical_model_turn_count"] == count


def test_ten_thousand_actual_compact_dispatches_release_only_completed_keys(aux_request, record_property):
    backend, ledger = aux_request.agent.backend, aux_request.agent._model_call_ledger
    peaks, checkpoints = [], {}

    def observe_active(_chunk):
        peaks.append(max(len(aggregate.logical_call_counts) for aggregate in ledger._scope_aggregates.values()))
        return backend.response

    backend.operation = observe_active
    for number in range(1, 10001):
        assert service.generate_auxiliary_model_response(aux_request) is backend.response
        if number in (100, 1000, 10000):
            aggregate = ledger._scope_aggregates[("run", "run")]
            checkpoints[number] = (aggregate.closed_logical_call_count, len(aggregate.logical_call_counts))
    assert backend.calls == 10000
    assert max(peaks) == 1
    assert checkpoints == {number: (number, 0) for number in (100, 1000, 10000)}
    _assert_closed(aux_request, 10000)
    assert len(ledger.records()) == 128
    assert all(row.status == "finished" for row in ledger.records())
    assert ledger.cumulative_summary(run_id="run")["accounted_input_tokens"] == 70000
    record_property("closed_and_open_at_history_sizes", str(checkpoints))


def test_backend_failure_preserves_original_exception_and_observation(aux_request):
    error, observations = RuntimeError("offline failure"), []

    def fail(_chunk):
        raise error

    aux_request.agent.backend.operation = fail
    with pytest.raises(RuntimeError) as caught:
        service.generate_auxiliary_model_response(replace(aux_request, on_observation=observations.append))
    assert caught.value is error and caught.value.__cause__ is None
    assert len(observations) == 1 and observations[0].provider_http_attempt_count == 1
    assert aux_request.agent.metrics == {"calls": [False], "cost": [], "run_cost": []}
    _assert_closed(aux_request, 1)
    assert aux_request.agent._model_call_ledger.records()[0].status == "failed"


@pytest.mark.parametrize("before_start", [False, True])
def test_cancellation_keeps_the_original_boundary(aux_request, before_start):
    token = CancellationToken()

    def cancel(_chunk):
        token.cancel("offline cancellation")
        return aux_request.agent.backend.response

    aux_request.agent.backend.operation = cancel
    if before_start:
        token.cancel("offline cancellation")
    with bind_cancellation_token(token), pytest.raises(ToolCancelled, match="offline cancellation"):
        service.generate_auxiliary_model_response(aux_request)
    assert aux_request.agent.backend.calls == int(not before_start)
    _assert_closed(aux_request, int(not before_start))
    assert aux_request.agent.metrics["cost"] == []


def test_observer_exception_keeps_response_and_costs(aux_request, caplog):
    observations = []

    def broken_observer(observation):
        observations.append(observation)
        raise ValueError("observer unavailable")

    response = service.generate_auxiliary_model_response(replace(aux_request, on_observation=broken_observer))
    assert response is aux_request.agent.backend.response
    assert len(observations) == 1 and observations[0].provider_http_attempt_count == 1
    assert aux_request.agent.metrics == {"calls": [True], "cost": [("offline-model", response)],
                                         "run_cost": [("offline-owner", "run", "offline-model", response)]}
    assert "辅助模型调用观察暂不可用" in caplog.text
    _assert_closed(aux_request, 1)


def test_observer_interrupt_still_releases_completed_logical_identity(aux_request):
    error = KeyboardInterrupt("observer interrupted")

    def interrupted_observer(_observation):
        raise error

    with pytest.raises(KeyboardInterrupt) as caught:
        service.generate_auxiliary_model_response(replace(aux_request, on_observation=interrupted_observer))
    assert caught.value is error and caught.value.__cause__ is None
    _assert_closed(aux_request, 1)
    assert aux_request.agent._model_call_ledger.records()[0].status == "finished"
    assert aux_request.agent.metrics == {"calls": [], "cost": [], "run_cost": []}


@pytest.mark.parametrize(("request_id", "run_id"), [("request", ""), ("", "run"), ("", "")])
def test_missing_scope_does_not_create_another_authority(aux_request, request_id, run_id):
    aux_request = replace(aux_request, request_id=request_id, run_id=run_id)
    assert service.generate_auxiliary_model_response(aux_request) is aux_request.agent.backend.response
    _assert_closed(aux_request, 1)
    assert len(aux_request.agent._model_call_ledger._scope_aggregates) == bool(request_id) + bool(run_id)


def test_late_physical_outcome_cannot_reopen_closed_auxiliary_group(aux_request):
    ready, release = Event(), Event()
    errors, workers = [], []
    ledger = aux_request.agent._model_call_ledger

    def launch_late_worker(on_chunk):
        call_id = ledger.records()[0].call_id
        observe_http = gateway_helpers._PROVIDER_ATTEMPT_OBSERVER.callback

        def late():
            try:
                ready.set()
                assert release.wait(5)
                on_chunk("迟到输出")
                with gateway_helpers.provider_attempt_observer(observe_http):
                    gateway_helpers._emit_provider_attempt({"attempt_id": "late-http", "status": "failed"})
                record_model_call_finished(ledger, call_id, ModelResponse(text="迟到", backend="offline",
                                                                          usage={"input_tokens": 999, "output_tokens": 999}))
            except BaseException as exc:
                errors.append(exc)

        worker = Thread(target=copy_context().run, args=(late,))
        workers.append(worker)
        worker.start()
        assert ready.wait(5)
        return aux_request.agent.backend.response

    aux_request.agent.backend.operation = launch_late_worker
    try:
        service.generate_auxiliary_model_response(aux_request)
        _assert_closed(aux_request, 1)
        release.set()
    finally:
        release.set()
        for worker in workers:
            worker.join(5)
    assert not errors and all(not worker.is_alive() for worker in workers)
    _assert_closed(aux_request, 1)
    summary = ledger.cumulative_summary(run_id="run")
    assert summary["status_counts"]["finished"] == 1
    assert sum(summary["status_counts"].values()) == 1
    assert summary["provider_http_attempt_count"] == 2
    assert summary["accounted_input_tokens"] == 7 and summary["output_tokens"] == 3
