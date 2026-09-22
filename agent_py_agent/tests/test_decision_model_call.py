"""实际决策调用的 worker、原账本和传输观察组合；只使用 fake 后端及本地 HTTP。"""
from __future__ import annotations

import contextvars
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.backends import bounded_call, gateway_helpers
from agent_py_agent.agent.backends.base import BackendOptions
from agent_py_agent.agent.backends.bounded_call import (
    BoundedCallBusyError,
    BoundedCallStillRunningError,
)
from agent_py_agent.agent.backends.decision_protocol import DecisionRequest, DecisionResponse
from agent_py_agent.agent.backends.errors import (
    ProviderResponseError,
    ProviderTimeoutError,
    ProviderTransientError,
)
from agent_py_agent.agent.backends.provider_headers import provider_runtime_scope, request_headers
from agent_py_agent.agent.backends.typesafe_decision import TypesafeDecisionBackend
from agent_py_agent.agent.concurrency.interrupt import InterruptHandle, is_interrupted
from agent_py_agent.agent.contracts.model_call_ledger import (
    ModelCallFinishParams,
    ModelCallLedger,
    ModelCallLedgerOptions,
    ModelCallStartedParams,
)
from agent_py_agent.agent.conversation import decision_model_call as service
from agent_py_agent.agent.llm_scale import hot_path
from agent_py_agent.tests.test_decision_protocol import binding, questions, response


def _agent(*, max_records=128):
    return SimpleNamespace(
        backend=SimpleNamespace(name="ordinary-backend", model_name="ordinary-model"),
        home_paths=SimpleNamespace(owner_provider="local", owner_kind="user", owner_id="owner-header"),
        config=SimpleNamespace(my_agent_owner_id="owner-cost"),
        _model_call_ledger=ModelCallLedger(ModelCallLedgerOptions(max_records=max_records)),
    )


def _params():
    return SimpleNamespace(request_id="request", run_id="run", task_id="task", thread_id="fallback-thread",
                           task_attributes={"agent_thread_id": "agent-thread"})


def _request():
    return DecisionRequest(binding(run_id="run", task_id="task", thread_id="agent-thread"), {"fact": "私有材料"}, questions())


def _result(request):
    return DecisionResponse(request.binding, request.input_digest, "decision-model", "resolved-model", (),
                            b'{"input_tokens":21,"output_tokens":4}', usage_reported=True)


class _Backend:
    name = "decision-backend"
    model_name = "decision-model"

    def __init__(self, operation=None):
        self.operation = operation
        self.calls = []
        self.threads = []

    def decide(self, request, *, deadline):
        self.calls.append((request, deadline))
        self.threads.append(threading.current_thread())
        return self.operation(request, deadline) if self.operation else _result(request)

    def generate(self, *args, **kwargs):
        pytest.fail("决策不能进入生成或 fallback")


@pytest.fixture(autouse=True)
def _admission(monkeypatch):
    monkeypatch.setenv("LLM_MAX_INFLIGHT", "2")
    monkeypatch.setenv("LLM_ADMISSION_WAIT_SECONDS", "0")
    hot_path.reset_hot_path_admission_for_test()
    yield
    hot_path.reset_hot_path_admission_for_test()


@pytest.fixture(autouse=True)
def metrics(monkeypatch):
    rows = {"inflight": [], "calls": [], "cost": [], "run_cost": []}
    monkeypatch.setattr(service, "llm_inflight", lambda value: rows["inflight"].append((value, threading.get_ident())))
    monkeypatch.setattr(service.llm_metrics, "record_llm_call", lambda label, duration, result, *, ok:
                        rows["calls"].append((label, duration, result, ok)))
    monkeypatch.setattr(service.llm_metrics, "record_llm_cost", lambda model, result: rows["cost"].append((model, result)))
    monkeypatch.setattr(service.llm_metrics, "record_run_cost", lambda owner, run, model, result:
                        rows["run_cost"].append((owner, run, model, result)))
    return rows


def _invoke(agent, backend, *, deadline=None, resource="resource", handle=None, request=None):
    return service.invoke_decision_model_call(agent, _params(), request or _request(), backend,
        deadline=deadline if deadline is not None else time.monotonic() + 2,
        resource_key=resource, interrupt_handle=handle)


def test_worker_installs_http_observer_copies_context_and_uses_decision_identity(metrics):
    agent, params, request = _agent(), _params(), _request()
    marker = contextvars.ContextVar("decision-test-marker", default="")
    marker_token = marker.set("host-context")
    worker_facts, parent_events = [], []
    with provider_runtime_scope(agent, params):
        expected_header = request_headers({}, {}, "x-session")["x-session"]

    def operation(actual, deadline):
        worker_facts.append((threading.get_ident(), marker.get(), request_headers({}, {}, "x-session")["x-session"]))
        assert gateway_helpers._PROVIDER_ATTEMPT_OBSERVER.cache_diagnostics is False
        gateway_helpers._emit_provider_attempt({"attempt_id": "http", "status": "started", "method": "POST", "path": "/v1/systemone"})
        gateway_helpers._emit_provider_attempt({"attempt_id": "http", "status": "response_opened", "http_status": 200})
        return _result(actual)

    backend = _Backend(operation)
    try:
        with gateway_helpers.provider_attempt_observer(parent_events.append, cache_diagnostics=True):
            result = _invoke(agent, backend, request=request)
            gateway_helpers._emit_provider_attempt({"after": "caller"})
    finally:
        marker.reset(marker_token)
    assert parent_events == [{"after": "caller"}]
    assert worker_facts == [(backend.threads[0].ident, "host-context", expected_header)]
    assert backend.threads[0].ident != threading.get_ident()
    record = agent._model_call_ledger.records()[0]
    assert record.status == "finished" and record.model == "decision-model"
    assert record.backend == "decision-backend" and record.provider_attempt_count == 1
    assert record.metadata["purpose"] == "decision" and record.metadata["auxiliary"] is True
    assert record.metadata["thread_id"] == "agent-thread" and record.metadata["task_id"] == "task"
    assert "request_surface" not in record.metadata
    assert "私有材料" not in json.dumps(record.to_dict(), ensure_ascii=False)
    assert record.accounted_input_tokens == 21 and record.output_tokens == 4
    assert agent._model_call_ledger._retained_calls == {}
    assert metrics["calls"][0][0] == "decision-backend" and metrics["calls"][0][3] is True
    assert metrics["cost"] == [("decision-model", result)]
    assert metrics["run_cost"] == [("owner-cost", "run", "decision-model", result)]
    assert metrics["inflight"] == [(1, backend.threads[0].ident), (-1, backend.threads[0].ident)]


def test_real_local_http_is_observed_from_actual_worker_with_host_header(monkeypatch):
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            seen.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"]))), self.headers["x-session"]))
            encoded = json.dumps(response()).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    agent, request = _agent(), _request()
    backend = TypesafeDecisionBackend(BackendOptions(f"http://127.0.0.1:{server.server_port}/v1", "fake-key", "jev-test", session_header="x-session"))
    try:
        result = _invoke(agent, backend, request=request)
        assert result.usage == {"input_tokens": 120, "output_tokens": 30}
        assert seen[0][:2] == ("/v1/systemone", request.payload("jev-test"))
        with provider_runtime_scope(agent, _params()):
            assert seen[0][2] == request_headers({}, {}, "x-session")["x-session"]
        record = agent._model_call_ledger.records()[0]
        assert record.provider_attempt_count == 1
        assert record.provider_attempts[0]["status"] == "response_opened"
        assert record.provider_attempts[0]["http_status"] == 200
        assert record.provider_attempts[0]["method"] == "POST"
        assert "request_surface" not in record.metadata
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(2)


def test_same_operation_and_digest_share_logical_call_but_not_physical_id():
    agent, backend, request = _agent(), _Backend(), _request()
    for _ in range(2):
        _invoke(agent, backend, request=request)
        backend.threads[-1].join(2)
    first, second = agent._model_call_ledger.records()
    assert first.call_id != second.call_id
    assert first.metadata["logical_call_id"] == second.metadata["logical_call_id"]
    summary = agent._model_call_ledger.cumulative_summary(run_id="run")["purpose_breakdown"]["decision"]
    assert summary["logical_model_turn_count"] == 1 and summary["physical_model_attempt_count"] == 2
    assert summary["model_retry_count"] == 1


def test_admission_rejection_never_calls_backend_or_counts_inflight(monkeypatch, metrics):
    monkeypatch.setenv("LLM_MAX_INFLIGHT", "1")
    hot_path.reset_hot_path_admission_for_test()
    agent, backend = _agent(), _Backend()
    with pytest.raises(ProviderTransientError):
        _invoke(agent, backend)
    assert backend.calls == [] and metrics["inflight"] == [] and metrics["cost"] == []
    assert len(metrics["calls"]) == 1 and metrics["calls"][0][3] is False
    record = agent._model_call_ledger.records()[0]
    assert record.status == "failed" and record.provider_attempt_count == 0
    assert agent._model_call_ledger._retained_calls == {}


def test_timeout_keeps_worker_admission_and_ledger_until_late_http_and_actual_exit(metrics):
    entered, allow_http, late_http, allow_exit = (threading.Event() for _ in range(4))

    def operation(request, deadline):
        gateway_helpers._emit_provider_attempt({"attempt_id": "http", "status": "started"})
        entered.set()
        assert allow_http.wait(3)
        gateway_helpers._emit_provider_attempt({"attempt_id": "http", "status": "response_opened", "http_status": 200})
        late_http.set()
        assert allow_exit.wait(3)
        return _result(request)

    agent, backend = _agent(max_records=1), _Backend(operation)
    ledger = agent._model_call_ledger
    try:
        with pytest.raises(BoundedCallStillRunningError):
            _invoke(agent, backend, deadline=time.monotonic() + 0.1)
        assert entered.is_set() and backend.threads[0].is_alive()
        call_id = ledger.records()[0].call_id
        assert ledger.records()[0].status == "timed_out"
        assert hot_path._limiter().in_flight() == 1
        assert len(metrics["inflight"]) == 1
        with pytest.raises(BoundedCallBusyError):
            _invoke(agent, backend)
        with pytest.raises(ProviderTransientError):
            _invoke(agent, backend, resource="other-decision")
        assert len(backend.calls) == 1
        with hot_path.global_llm_admission_slot():
            assert hot_path._limiter().in_flight() == 2
        for index in range(4):
            ledger.started(ModelCallStartedParams(f"normal-{index}", "normal", "normal", 2, request_id="request", run_id="run"))
            ledger.finished(ModelCallFinishParams(f"normal-{index}", output_tokens=1))
        assert {record.call_id for record in ledger.records()} == {call_id, "normal-3"}
        allow_http.set()
        assert late_http.wait(2)
        record = next(record for record in ledger.records() if record.call_id == call_id)
        assert record.status == "timed_out" and record.provider_attempts[0]["http_status"] == 200
        assert record.finished_at is None
        assert hot_path._limiter().in_flight() == 1
    finally:
        allow_http.set()
        allow_exit.set()
        for thread in backend.threads:
            thread.join(3)
    assert not backend.threads[0].is_alive()
    assert hot_path._limiter().in_flight() == 0
    assert ledger._retained_calls == {}
    assert [record.call_id for record in ledger.records()] == ["normal-3"]
    assert [row[0] for row in metrics["inflight"]] == [1, -1]
    assert metrics["cost"] == []
    decision = ledger.cumulative_summary(run_id="run")["purpose_breakdown"]["decision"]
    assert decision["status_counts"]["timed_out"] == 1 and decision["status_counts"]["finished"] == 0
    assert decision["provider_http_attempt_count"] == 1


def test_external_cancel_wakes_caller_without_releasing_worker_or_parent_flags(metrics):
    entered, release = threading.Event(), threading.Event()

    def operation(request, deadline):
        entered.set()
        assert release.wait(3)
        return _result(request)

    agent, backend, handle = _agent(), _Backend(operation), InterruptHandle()
    errors = []

    def caller():
        try:
            _invoke(agent, backend, handle=handle)
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=caller)
    thread.start()
    try:
        assert entered.wait(2)
        handle.cancel()
        thread.join(2)
        assert not thread.is_alive() and len(errors) == 1
        assert isinstance(errors[0], InterruptedError)
        assert backend.threads[0].is_alive() and hot_path._limiter().in_flight() == 1
        assert not is_interrupted()
        record = agent._model_call_ledger.records()[0]
        assert record.status == "failed" and record.error_code == "DECISION_CANCELLED"
    finally:
        release.set()
        thread.join(3)
        for worker in backend.threads:
            worker.join(3)
    assert agent._model_call_ledger._retained_calls == {}
    assert hot_path._limiter().in_flight() == 0
    assert len(metrics["calls"]) == 1 and metrics["calls"][0][3] is False


@pytest.mark.parametrize("exception", [InterruptedError("private"), KeyboardInterrupt("private"), SystemExit("private"),
                                     ProviderResponseError("private-body", error_code="private-code")])
def test_original_exception_propagates_and_ledger_never_copies_private_error_content(exception):
    def operation(request, deadline):
        raise exception

    agent, backend = _agent(), _Backend(operation)
    with pytest.raises(type(exception)) as caught:
        _invoke(agent, backend)
    assert caught.value is exception and len(backend.calls) == 1
    record = agent._model_call_ledger.records()[0]
    assert record.status == "failed"
    assert "private" not in json.dumps(record.to_dict())
    assert agent._model_call_ledger._retained_calls == {}


def test_provider_timeout_preserves_original_stage():
    exception = ProviderTimeoutError("private", stage="provider_declared")

    def operation(request, deadline):
        raise exception

    agent = _agent()
    with pytest.raises(ProviderTimeoutError) as caught:
        _invoke(agent, _Backend(operation))
    assert caught.value is exception
    record = agent._model_call_ledger.records()[0]
    assert record.status == "timed_out" and record.timeout_stage == "provider_declared"


def test_late_caller_handoff_does_not_finish_an_on_time_worker_result(monkeypatch):
    original = service.call_with_deadline

    def delayed_handoff(*args, **kwargs):
        result = original(*args, **kwargs)
        threading.Event().wait(max(0, kwargs["deadline"] - time.monotonic()) + 0.01)
        return result

    monkeypatch.setattr(service, "call_with_deadline", delayed_handoff)
    agent = _agent()
    with pytest.raises(ProviderTimeoutError):
        _invoke(agent, _Backend(), deadline=time.monotonic() + 0.05)
    record = agent._model_call_ledger.records()[0]
    assert record.status == "timed_out" and record.finished_at is None


def test_cancel_after_worker_return_still_wins_before_caller_finish(monkeypatch):
    original, handle = service.call_with_deadline, InterruptHandle()

    def cancelled_handoff(*args, **kwargs):
        result = original(*args, **kwargs)
        handle.cancel()
        return result

    monkeypatch.setattr(service, "call_with_deadline", cancelled_handoff)
    agent = _agent()
    with pytest.raises(InterruptedError):
        _invoke(agent, _Backend(), handle=handle)
    assert agent._model_call_ledger.records()[0].status == "failed"


@pytest.mark.parametrize("metric", ["llm_inflight", "record_llm_call", "record_llm_cost", "record_run_cost"])
def test_metric_failure_does_not_change_success_or_retention(metric, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("metric unavailable")

    monkeypatch.setattr(service if metric == "llm_inflight" else service.llm_metrics, metric, fail)
    agent = _agent()
    assert isinstance(_invoke(agent, _Backend()), DecisionResponse)
    assert agent._model_call_ledger.records()[0].status == "finished"
    assert agent._model_call_ledger._retained_calls == {}


def test_worker_construction_failure_releases_only_caller_token(monkeypatch):
    exception = RuntimeError("thread allocation unavailable")

    def fail(*args, **kwargs):
        raise exception

    monkeypatch.setattr(bounded_call.threading, "Thread", fail)
    agent, backend = _agent(), _Backend()
    with pytest.raises(RuntimeError) as caught:
        _invoke(agent, backend)
    assert caught.value is exception
    assert backend.calls == []
    assert agent._model_call_ledger._retained_calls == {}
    assert agent._model_call_ledger.records()[0].status == "failed"


def test_cancel_before_start_never_allocates_worker_or_leaks_pin():
    agent, backend, handle = _agent(), _Backend(), InterruptHandle()
    handle.cancel()
    with pytest.raises(InterruptedError):
        _invoke(agent, backend, handle=handle)
    assert backend.calls == []
    assert agent._model_call_ledger._retained_calls == {}
    assert agent._model_call_ledger.records()[0].error_code == "DECISION_CANCELLED"


def test_non_decision_response_is_failure_without_generate_or_fallback():
    agent, backend = _agent(), _Backend(lambda request, deadline: SimpleNamespace(text="not a decision", usage={}))
    with pytest.raises(ProviderResponseError):
        _invoke(agent, backend)
    assert len(backend.calls) == 1
    assert agent._model_call_ledger.records()[0].error_code == "DECISION_RESPONSE_INVALID"


@pytest.mark.parametrize("deadline", [True, float("inf"), float("nan"), pytest.param(10 ** 1000, id="oversized-integer")])
def test_invalid_deadline_never_calls_provider_or_leaks_pin(deadline):
    agent, backend = _agent(), _Backend()
    with pytest.raises(ValueError):
        _invoke(agent, backend, deadline=deadline)
    assert backend.calls == [] and agent._model_call_ledger._retained_calls == {}


def test_expired_deadline_records_timeout_without_provider_call():
    agent, backend = _agent(), _Backend()
    with pytest.raises(TimeoutError):
        _invoke(agent, backend, deadline=0)
    assert backend.calls == [] and agent._model_call_ledger._retained_calls == {}
    assert agent._model_call_ledger.records()[0].status == "timed_out"


def test_caller_timeout_before_worker_retain_and_detail_pruning_never_sends(monkeypatch):
    agent, backend = _agent(max_records=1), _Backend()
    ledger = agent._model_call_ledger
    original_retain = ledger.retain_call
    reached_retain, release_retain = threading.Event(), threading.Event()
    worker = []
    caller_thread = threading.current_thread()

    def delayed_worker_retain(call_id):
        if threading.current_thread() is not caller_thread:
            worker.append(threading.current_thread())
            reached_retain.set()
            assert release_retain.wait(3)
        return original_retain(call_id)

    monkeypatch.setattr(ledger, "retain_call", delayed_worker_retain)
    try:
        with pytest.raises(BoundedCallStillRunningError):
            _invoke(agent, backend, deadline=time.monotonic() + 0.1)
        assert reached_retain.is_set() and ledger._retained_calls == {}
        ledger.started(ModelCallStartedParams("next", "normal", "normal", 2))
        assert [record.call_id for record in ledger.records()] == ["next"]
        with bounded_call._CALL_LOCK:
            retained_call = bounded_call._CALLS["resource"]
    finally:
        release_retain.set()
        for thread in worker:
            thread.join(3)
    assert backend.calls == [] and ledger._retained_calls == {}
    assert retained_call.result[0] is False
    assert isinstance(retained_call.result[1], InterruptedError)
