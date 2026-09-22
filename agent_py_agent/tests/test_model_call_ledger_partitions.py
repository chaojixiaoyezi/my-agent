from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.model.call_runtime import (
    model_call_ledger,
    model_call_summary,
    record_model_call_finished,
)
from agent_py_agent.agent.backends.decision_protocol import DecisionBinding, DecisionResponse
from agent_py_agent.agent.contracts.model_call_ledger import (
    ModelCallActivityParams,
    ModelCallFailureParams,
    ModelCallFinishParams,
    ModelCallFirstTokenParams,
    ModelCallLedger,
    ModelCallLedgerContext,
    ModelCallLedgerOptions,
    ModelCallProviderAttemptParams,
    ModelCallStartedParams,
    ModelCallTimeoutParams,
)
from agent_py_agent.agent.memory_archive import estimate_tokens

FIELDS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_write_input_tokens")


def _start(ledger: ModelCallLedger, call_id: str = "call", **metadata) -> None:
    ledger.started(ModelCallStartedParams(
        call_id, "fake", "fake-model", 77, request_id="request", run_id="run", metadata=metadata,
    ))


def _event(ledger: ModelCallLedger, name: str):
    if name == "finished":
        return ledger.finished(ModelCallFinishParams("call", output_tokens=4, input_tokens=3))
    if name == "failed":
        return ledger.failed(ModelCallFailureParams("call", "Cancelled", "CANCELLED"))
    if name == "timed_out":
        return ledger.timeout(ModelCallTimeoutParams("call", 0.01, "wall_clock"))
    if name == "first_token":
        return ledger.first_token(ModelCallFirstTokenParams("call", 9))
    if name == "activity":
        return ledger.activity(ModelCallActivityParams("call", 9))
    return ledger.started(ModelCallStartedParams("call", "replacement", "other-model", 999))


@pytest.mark.parametrize("terminal", ["finished", "failed", "timed_out"])
@pytest.mark.parametrize("late", ["finished", "failed", "timed_out", "first_token", "activity", "started"])
def test_first_terminal_snapshot_is_immutable_and_duplicate_events_are_idempotent(terminal, late):
    now = [100.0]
    ledger = ModelCallLedger(context=ModelCallLedgerContext(now=lambda: now[0]))
    _start(ledger)
    now[0] += 1
    original = _event(ledger, terminal)
    original_summary = ledger.cumulative_summary(run_id="run")
    now[0] += 30

    assert _event(ledger, late) is original
    assert _event(ledger, "first_token") is original
    assert _event(ledger, "finished") is original
    assert ledger.records() == (original,)
    assert ledger.cumulative_summary(run_id="run") == original_summary


@pytest.mark.parametrize("terminal", ["finished", "failed", "timed_out"])
def test_late_worker_http_facts_do_not_reopen_terminal_or_change_usage(terminal):
    now = [100.0]
    ledger = ModelCallLedger(context=ModelCallLedgerContext(now=lambda: now[0]))
    _start(ledger, purpose="decision", auxiliary=True)
    ready, release = threading.Event(), threading.Event()
    errors = []

    def worker():
        try:
            ready.set()
            assert release.wait(2)
            for index in range(2):
                ledger.provider_attempt(ModelCallProviderAttemptParams("call", f"http-{index}", "started"))
                ledger.provider_attempt(ModelCallProviderAttemptParams("call", f"http-{index}", "failed", error_type="Closed"))
            _event(ledger, "first_token")
            _event(ledger, "finished")
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=worker)
    thread.start()
    try:
        assert ready.wait(2)
        now[0] = 101
        terminal_record = _event(ledger, terminal)
        now[0] = 150
        release.set()
        thread.join(2)
        assert not thread.is_alive()
        assert not errors
        record = ledger.records()[0]
        assert record.status == terminal
        assert record.last_activity_at == terminal_record.last_activity_at
        assert record.total_latency_seconds == terminal_record.total_latency_seconds
        assert record.accounted_input_tokens == terminal_record.accounted_input_tokens
        assert record.output_tokens == terminal_record.output_tokens
        assert record.provider_attempt_count == 2
        summary = ledger.cumulative_summary(run_id="run")
        decision = summary["purpose_breakdown"]["decision"]
        assert summary["provider_http_attempt_count"] == decision["provider_http_attempt_count"] == 2
        assert summary["provider_http_retry_count"] == decision["provider_http_retry_count"] == 1
        assert summary["status_counts"][terminal] == decision["status_counts"][terminal] == 1
        assert summary["logical_model_turn_count"] == 1
    finally:
        release.set()
        thread.join(2)


@pytest.mark.parametrize(("usage", "input_value", "output_value", "reported"), [
    ({"output_tokens": 4}, None, 4, ("output_tokens",)),
    ({"input_tokens": 0, "output_tokens": 0}, 0, 0, ("input_tokens", "output_tokens")),
    ({"prompt_tokens": 0, "completion_tokens": 0, "prompt_tokens_details": {"cached_tokens": 0},
      "cache_creation_input_tokens": 0}, 0, 0, FIELDS),
    ({"input_tokens": 5, "output_tokens": 0, "cache_read_input_tokens": 3,
      "cache_creation_input_tokens": 2}, 10, 0, FIELDS),
    ({"prompt_tokens": 7, "completion_tokens": 5,
      "input_tokens_details": {"cache_read_tokens": 0, "cache_write_tokens": 0}}, 7, 5, FIELDS),
    ({"cache_read_input_tokens": 2}, None, None, ("cache_read_input_tokens",)),
    ({"cost_usd": 1}, None, None, ()),
    ({}, None, None, ()),
])
def test_usage_provenance_keeps_missing_fields_separate_from_true_zero(usage, input_value, output_value, reported):
    ledger = ModelCallLedger()
    _start(ledger)
    text = "本地估算的输出内容"
    record_model_call_finished(ledger, "call", SimpleNamespace(text=text, usage=usage))
    record = ledger.records()[0]
    assert record.provider_usage_reported == bool(usage)
    assert record.provider_usage_fields == reported
    assert record.to_dict()["provider_usage_fields"] == list(reported)
    summary = ledger.cumulative_summary(run_id="run")
    provider = summary["usage_breakdown"]["provider"]
    estimated = summary["usage_breakdown"]["estimated"]
    assert provider["input_tokens"] == (input_value or 0)
    assert provider["output_tokens"] == (output_value or 0)
    assert estimated["input_tokens"] == (77 if input_value is None else 0)
    assert estimated["output_tokens"] == (estimate_tokens(text) if output_value is None else 0)
    assert record.accounted_input_tokens == (77 if input_value is None else input_value)
    assert provider["call_count"] == int(bool(usage))
    assert estimated["call_count"] == int(not usage)
    for name in FIELDS:
        assert provider[f"{name}_reported_call_count"] == int(name in reported)


@pytest.mark.parametrize("legacy_reported", [True, False])
def test_legacy_field_provenance_preserves_v1_envelope_interpretation(legacy_reported):
    ledger = ModelCallLedger()
    _start(ledger)
    ledger.finished(ModelCallFinishParams("call", output_tokens=4, provider_usage_reported=legacy_reported))
    record = ledger.records()[0]
    assert record.provider_usage_fields is None
    assert record.to_dict()["provider_usage_fields"] is None
    breakdown = ledger.cumulative_summary(run_id="run")["usage_breakdown"]
    old_bucket = breakdown["provider" if legacy_reported else "estimated"]
    assert old_bucket["input_tokens"] == 77
    assert old_bucket["output_tokens"] == 4
    assert old_bucket["call_count"] == 1
    assert all(breakdown["provider"][f"{name}_reported_call_count"] == 0 for name in FIELDS)


@pytest.mark.parametrize(("usage", "fields"), [(b'{"output_tokens":4}', ("output_tokens",)), (b'{}', ())])
def test_decision_response_uses_original_usage_path_without_generation_interface(usage, fields):
    ledger = ModelCallLedger()
    _start(ledger, purpose="decision", auxiliary=True)
    response = DecisionResponse(
        DecisionBinding("test", "owner", "operation", "policy", "candidates"),
        "digest", "requested-model", "actual-model", (), usage, usage_reported=usage != b'{}',
    )
    assert not hasattr(response, "text")
    assert not hasattr(response, "generate")
    record_model_call_finished(ledger, "call", response)
    assert ledger.records()[0].provider_usage_fields == fields
    decision = ledger.cumulative_summary(run_id="run")["purpose_breakdown"]["decision"]
    assert decision["usage_breakdown"]["provider"]["input_tokens"] == 0
    assert decision["usage_breakdown"]["estimated"]["input_tokens"] == 77
    assert decision["usage_breakdown"]["provider"]["output_tokens"] == (4 if fields else 0)
    assert decision["usage_breakdown"]["provider"]["input_tokens_reported_call_count"] == 0
    assert decision["usage_breakdown"]["estimated"]["output_tokens"] == 0
    assert decision["usage_breakdown"]["provider"]["output_tokens_reported_call_count"] == int(bool(fields))


def test_generation_empty_text_keeps_original_estimate_when_output_usage_is_missing():
    ledger = ModelCallLedger()
    _start(ledger)
    record_model_call_finished(ledger, "call", SimpleNamespace(text="", usage={}))
    assert ledger.records()[0].output_tokens == estimate_tokens("")
    assert ledger.records()[0].provider_usage_fields == ()


def test_purpose_totals_survive_detail_pruning_and_namespace_shared_logical_ids():
    ledger = ModelCallLedger(options=ModelCallLedgerOptions(max_records=2))
    purposes = ({}, {"auxiliary": True, "purpose": "compact"}, {"auxiliary": True, "purpose": "decision"})
    for bucket_index, metadata in enumerate(purposes):
        for retry in range(2):
            call_id = f"{bucket_index}-{retry}"
            _start(ledger, call_id, **metadata, logical_call_id="same-logical-id")
            for attempt in range(retry + 1):
                ledger.provider_attempt(ModelCallProviderAttemptParams(call_id, f"http-{attempt}", "response_opened"))
            record_model_call_finished(ledger, call_id, SimpleNamespace(usage={"output_tokens": 4}, text=""))
    assert len(ledger.records()) == 2
    summary = ledger.cumulative_summary(run_id="run")
    partitions = summary["purpose_breakdown"]
    assert summary["logical_model_turn_count"] == 3
    assert summary["physical_model_attempt_count"] == 6
    assert summary["model_retry_count"] == 3
    assert summary["provider_http_attempt_count"] == 9
    for name in ("main", "auxiliary", "decision"):
        part = partitions[name]
        assert part["logical_model_turn_count"] == 1
        assert part["physical_model_attempt_count"] == 2
        assert part["model_retry_count"] == 1
        assert part["provider_http_attempt_count"] == 3
        assert part["provider_http_retry_count"] == 1
        assert part["usage_breakdown"]["provider"]["output_tokens"] == 8
        assert part["usage_breakdown"]["provider"]["output_tokens_reported_call_count"] == 2
        assert part["usage_breakdown"]["estimated"]["input_tokens"] == 154
        assert part["status_counts"]["finished"] == 2
        assert "purpose_breakdown" not in part
        assert "usage_scope_id" not in part
    for key in ("logical_model_turn_count", "physical_model_attempt_count", "model_retry_count", "total_tokens",
                "provider_http_attempt_count", "provider_http_retry_count"):
        assert summary[key] == sum(partitions[name][key] for name in ("main", "auxiliary", "decision"))
    request_summary = ledger.cumulative_summary(request_id="request")
    assert request_summary["purpose_breakdown"] == partitions


def test_retained_and_empty_summaries_reuse_field_and_purpose_rules():
    ledger = ModelCallLedger()
    _start(ledger, purpose="decision", auxiliary=True)
    record_model_call_finished(ledger, "call", SimpleNamespace(usage={"output_tokens": 4}))
    agent = SimpleNamespace(_model_call_ledger=ledger)
    original = model_call_summary(agent, run_id="run")
    original.pop("usage_scope_id")
    ledger._scope_aggregates.clear()
    assert model_call_summary(agent, run_id="run") == original
    for empty in (model_call_summary(SimpleNamespace()), model_call_summary(agent, run_id="other")):
        assert empty["status_counts"] == {}
        for name in ("main", "auxiliary", "decision"):
            bucket = empty["purpose_breakdown"][name]
            assert bucket["physical_model_attempt_count"] == 0
            assert bucket["usage_breakdown"]["provider"]["input_tokens_reported_call_count"] == 0


def _other_scoped_calls(ledger: ModelCallLedger, count: int = 4) -> None:
    for index in range(count):
        call_id = f"other-{index}"
        ledger.started(ModelCallStartedParams(
            call_id, "fake", "other-model", 2, request_id=f"request-{index}", run_id=f"run-{index}",
        ))
        ledger.finished(ModelCallFinishParams(call_id, input_tokens=2, output_tokens=1))


def test_worker_retention_survives_detail_and_scope_pruning_then_releases_on_actual_exit():
    ledger = ModelCallLedger(options=ModelCallLedgerOptions(max_records=1))
    _start(ledger, purpose="decision", auxiliary=True)
    scope_id = ledger.cumulative_summary(run_id="run")["usage_scope_id"]
    caller_token, worker_token = ledger.retain_call("call"), ledger.retain_call("call")
    ready, observed, send_attempt, exit_worker = (threading.Event() for _ in range(4))
    errors = []

    def worker():
        try:
            with worker_token:
                ready.set()
                assert send_attempt.wait(2)
                ledger.provider_attempt(ModelCallProviderAttemptParams("call", "late-http", "failed"))
                observed.set()
                assert exit_worker.wait(2)
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=worker)
    thread.start()
    try:
        assert ready.wait(2)
        _event(ledger, "timed_out")
        caller_token.release()
        _other_scoped_calls(ledger)
        assert {record.call_id for record in ledger.records()} == {"call", "other-3"}
        assert ledger.cumulative_summary(run_id="run")["usage_scope_id"] == scope_id
        assert len(ledger._scope_aggregates) == 4
        send_attempt.set()
        assert observed.wait(2)
        summary = ledger.cumulative_summary(run_id="run")
        assert summary["usage_scope_id"] == scope_id
        assert summary["physical_model_attempt_count"] == 1
        assert summary["logical_model_turn_count"] == 1
        assert summary["provider_http_attempt_count"] == 1
        assert summary["status_counts"]["timed_out"] == 1
        assert summary["purpose_breakdown"]["decision"]["provider_http_attempt_count"] == 1
        exit_worker.set()
        thread.join(2)
        assert not thread.is_alive()
        assert not errors
        assert [record.call_id for record in ledger.records()] == ["other-3"]
        assert ledger._retained_calls == {}
        assert len(ledger._scope_aggregates) == 2
    finally:
        caller_token.release()
        send_attempt.set()
        exit_worker.set()
        thread.join(2)


def test_retention_context_exception_and_duplicate_release_restore_original_limits():
    ledger = ModelCallLedger(options=ModelCallLedgerOptions(max_records=1))
    _start(ledger)
    token = ledger.retain_call("call")
    with pytest.raises(LookupError, match="worker failed"):
        with token:
            _other_scoped_calls(ledger)
            assert len(ledger.records()) == 2
            raise LookupError("worker failed")
    token.release()
    token.release()
    assert [record.call_id for record in ledger.records()] == ["other-3"]
    assert ledger._retained_calls == {}
    assert len(ledger._scope_aggregates) == 2
    with pytest.raises(RuntimeError, match="已释放"):
        with token:
            pytest.fail("released token re-entered")


def test_retention_tokens_release_only_their_own_holder():
    ledger = ModelCallLedger(options=ModelCallLedgerOptions(max_records=1))
    _start(ledger)
    caller, worker = ledger.retain_call("call"), ledger.retain_call("call")
    _other_scoped_calls(ledger)
    worker.release()
    worker.release()
    assert {record.call_id for record in ledger.records()} == {"call", "other-3"}
    _event(ledger, "finished")
    assert ledger.cumulative_summary(run_id="run")["status_counts"]["finished"] == 1
    caller.release()
    worker.release()
    assert ledger._retained_calls == {}
    assert len(ledger.records()) == 1


def test_retain_unknown_call_fails_without_recreating_trimmed_records():
    ledger = ModelCallLedger(options=ModelCallLedgerOptions(max_records=1))
    with pytest.raises(KeyError, match="unknown model call id"):
        ledger.retain_call("missing")
    _start(ledger)
    _other_scoped_calls(ledger)
    with pytest.raises(KeyError, match="unknown model call id"):
        ledger.retain_call("call")
    with pytest.raises(KeyError, match="unknown model call id"):
        ledger.provider_attempt(ModelCallProviderAttemptParams("call", "late", "failed"))
    assert ledger._retained_calls == {}
    assert len(ledger.records()) == 1
    assert len(ledger._scope_aggregates) == 2
    assert ledger.cumulative_summary(run_id="run") is None


def test_started_retained_holds_caller_record_before_any_worker_exists():
    ledger = ModelCallLedger(options=ModelCallLedgerOptions(max_records=1))
    record, token = ledger.started_retained(ModelCallStartedParams("call", "fake", "model", 4))
    assert record.status == "started" and ledger._retained_calls == {"call": {token}}
    _other_scoped_calls(ledger)
    assert "call" in {item.call_id for item in ledger.records()}
    token.release()
    assert ledger._retained_calls == {} and len(ledger.records()) == 1


def test_concurrent_first_calls_initialize_one_agent_ledger(monkeypatch):
    original_init = ModelCallLedger.__init__
    first_constructing, second_starting = threading.Event(), threading.Event()
    second_constructed, release = threading.Event(), threading.Event()
    constructed, results, errors = [], [], []

    def slow_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        constructed.append(self)
        if len(constructed) == 1:
            first_constructing.set()
            assert release.wait(2)
        else:
            second_constructed.set()

    monkeypatch.setattr(ModelCallLedger, "__init__", slow_init)
    agent = SimpleNamespace()

    def get_ledger(*, second=False):
        if second:
            second_starting.set()
        try:
            results.append(model_call_ledger(agent))
        except BaseException as exc:
            errors.append(exc)

    first = threading.Thread(target=get_ledger)
    second = threading.Thread(target=get_ledger, kwargs={"second": True})
    first.start()
    try:
        assert first_constructing.wait(2)
        second.start()
        assert second_starting.wait(2)
        # 首个构造保持未完成，让第二个实际 caller 有机会竞争同一初始化入口。
        second_constructed.wait(0.05)
    finally:
        release.set()
        first.join(2)
        if second.ident is not None:
            second.join(2)
    assert not errors and len(results) == 2 and len(constructed) == 1
    assert results[0] is results[1] is agent._model_call_ledger
