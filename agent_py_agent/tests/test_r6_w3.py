from threading import Event, Thread

import pytest

from agent_py_agent.agent.agent_core.model.call_runtime import model_call_ledger
from agent_py_agent.agent.contracts.model_call_ledger import (
    ModelCallFinishParams,
    ModelCallFirstTokenParams,
    ModelCallLedger,
    ModelCallLedgerContext,
    ModelCallLedgerOptions,
)
from agent_py_agent.tests.test_model_call_ledger_partitions import (
    _event,
    _other_scoped_calls,
    _start,
)
from agent_py_agent.tests.test_provider_transient_auto_resume import (
    AgentConfig,
    SimpleAgent,
    _assert_single_logical_turn,
    _TransientThenOkBackend,
    _UsageStreamIncompleteThenOkBackend,
    provider_transient_auto_resume,
)


def test_ten_thousand_closed_groups_match_untrimmed_reference():
    now = [100.0]
    context = ModelCallLedgerContext(now=lambda: now[0])
    ledger = ModelCallLedger(context=context)
    reference = ModelCallLedger(ModelCallLedgerOptions(max_records=30000), context)
    for target in (ledger, reference):
        _start(target, "old-attempt", logical_call_id="still-retryable")
        target.finished(ModelCallFinishParams("old-attempt", output_tokens=1))
    for turn in range(10000):
        purpose = ("main", "auxiliary", "decision", "probe:tool_capability")[turn % 4]
        identity = dict(request_id="request", run_id="run", purpose=purpose, logical_call_id=f"turn-{turn}")
        for attempt in range(1 + turn % 3):
            call_id = f"{turn}:{attempt}"
            for target in (ledger, reference):
                _start(target, call_id, purpose=purpose, auxiliary=purpose == "auxiliary", logical_call_id=identity["logical_call_id"])
            now[0] += 1
            for target in (ledger, reference):
                target.first_token(ModelCallFirstTokenParams(call_id, 1))
            now[0] += 1
            for target in (ledger, reference):
                target.finished(ModelCallFinishParams(call_id, input_tokens=77, output_tokens=attempt, cached_input_tokens=5))
        ledger.close_logical_call(**identity)
        ledger.close_logical_call(**identity)
    for aggregate in ledger._scope_aggregates.values():
        assert aggregate.logical_call_counts == {("main", "still-retryable"): 1}
    for target in (ledger, reference):
        _start(target, "late-retry", logical_call_id="still-retryable")
        target.finished(ModelCallFinishParams("late-retry", output_tokens=1))
    ledger.close_logical_call(request_id="request", run_id="run", purpose="main", logical_call_id="still-retryable")
    for scope in ({"request_id": "request"}, {"run_id": "run"}):
        actual, expected = ledger.cumulative_summary(**scope), reference.cumulative_summary(**scope)
        actual.pop("usage_scope_id"), expected.pop("usage_scope_id")
        assert actual == expected
    assert len(ledger.records()) == 128
    assert all(not bucket.logical_call_counts for aggregate in ledger._scope_aggregates.values()
               for bucket in (aggregate, *aggregate.purpose_totals.values()))


@pytest.mark.parametrize("late", [("first_token", "finished", "timed_out"), ("timed_out", "first_token", "finished")])
def test_close_keeps_late_worker_residency_and_terminal_accounting(late):
    ledger = ModelCallLedger(ModelCallLedgerOptions(max_records=1))
    _start(ledger, logical_call_id="group")
    token = ledger.retain_call("call")
    ready, report, observed, exit_worker = (Event() for _ in range(4))
    errors = []

    def worker():
        try:
            with token:
                ready.set()
                assert report.wait(5)
                for event in late:
                    _event(ledger, event)
                observed.set()
                assert exit_worker.wait(5)
        except BaseException as error:
            errors.append(error)

    thread = Thread(target=worker)
    thread.start()
    try:
        assert ready.wait(5)
        ledger.close_logical_call(request_id="request", run_id="run", purpose="main", logical_call_id="group")
        report.set()
        assert observed.wait(5)
        _other_scoped_calls(ledger)
        assert "call" in ledger._retained_calls and thread.is_alive()
        summary = ledger.cumulative_summary(run_id="run")
        assert summary["logical_model_turn_count"] == summary["physical_model_attempt_count"] == 1
        assert summary["status_counts"]["finished" if late[0] == "first_token" else "timed_out"] == 1
        assert not ledger._scope_aggregates[("run", "run")].logical_call_counts
    finally:
        report.set()
        exit_worker.set()
        thread.join(5)
    assert not thread.is_alive() and not errors and not ledger._retained_calls
    assert [record.call_id for record in ledger.records()] == ["other-3"]


@pytest.mark.parametrize("incomplete", [False, True])
def test_real_outer_retry_closes_only_after_complete_response(tmp_path, monkeypatch, incomplete):
    monkeypatch.setattr(provider_transient_auto_resume, "wait_interruptibly", lambda _: None)
    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    agent.backend = _UsageStreamIncompleteThenOkBackend(with_usage=True) if incomplete else _TransientThenOkBackend(failures=1)
    agent.run("完成一次调用", save=False)
    _assert_single_logical_turn(agent, backend_name=agent.backend.name, expect_calibration=incomplete)
    ledger = model_call_ledger(agent)
    assert all(not bucket.logical_call_counts for aggregate in ledger._scope_aggregates.values()
               for bucket in (aggregate, *aggregate.purpose_totals.values()))
    assert all(not aggregate.retry_logical_call_ids for aggregate in ledger._scope_aggregates.values())
    identity = dict(request_id="request", run_id="run", purpose="main", identity="same-input")
    first = ledger.open_logical_call(**identity)
    assert ledger.open_logical_call(**identity) == first
    ledger.close_logical_call(request_id="request", run_id="run", purpose="main", logical_call_id=first)
    assert ledger.open_logical_call(**identity) != first
