"""原 ModelCallLedger 的 E1 预留原语；测试给定上界只是合同夹具，不证明 Jev/tokenizer 的输入上界。"""
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from agent_py_agent.agent.contracts.model_call_budget import (
    ModelCallBudgetError,
    ModelCallInputBudget,
)
from agent_py_agent.agent.contracts.model_call_ledger import (
    ModelCallFailureParams,
    ModelCallFinishParams,
    ModelCallLedger,
    ModelCallLedgerContext,
    ModelCallLedgerOptions,
    ModelCallProviderAttemptParams,
    ModelCallStartedParams,
    ModelCallTimeoutParams,
)


def _ledger(*, max_http=2, max_input=100, max_records=128):
    now = [10.0]
    ledger = ModelCallLedger(ModelCallLedgerOptions(max_records=max_records), ModelCallLedgerContext(now=lambda: now[0]))
    limits = ModelCallInputBudget("authorization", ledger.ledger_id, "owner", "thread", "task", "run", "attempt", "request",
                                  deadline=20, max_http_requests=max_http, max_input_tokens=max_input)
    ledger.begin_input_budget(limits)
    return ledger, limits, now


def _params(call_id="call"):
    return ModelCallStartedParams(call_id, "fixture", "decision-model", 999, request_id="request", run_id="run",
        metadata={"purpose": "decision", "owner_ref": "owner", "thread_id": "thread", "task_id": "task", "attempt_id": "attempt"})


def _reserve(ledger, call_id="call", bound=60):
    return ledger.reserve_input_budget(_params(call_id), budget_id="authorization", input_token_upper_bound=bound)


def _finish(ledger, call_id="call", actual=30, fields=("input_tokens",), http_count=1):
    for index in range(http_count):
        ledger.provider_attempt(ModelCallProviderAttemptParams(call_id, f"http-{index}", "started"))
        ledger.provider_attempt(ModelCallProviderAttemptParams(call_id, f"http-{index}", "response_opened"))
    return ledger.finished(ModelCallFinishParams(call_id, input_tokens=actual, cached_input_tokens=20,
        output_tokens=10000, provider_usage_reported=True, provider_usage_fields=fields))


def test_success_settles_provider_full_input_without_cache_or_output_discount():
    ledger, limits, _now = _ledger()
    _record, token = _reserve(ledger)
    with token:
        before = ledger.input_budget_snapshot(limits.budget_id)
        assert before["charged_input_tokens"] == 60 and before["provider_input_tokens"] == 0
        _finish(ledger)
        result = ledger.settle_input_budget("call")
        assert result["charged_input_tokens"] == result["provider_input_tokens"] == 30
        assert result["reserved_http_requests"] == 1 and result["unknown_usage_calls"] == 0
        assert ledger.settle_input_budget("call") == result
        assert ledger.records()[0].accounted_input_tokens == 30
        assert ledger.records()[0].metadata["input_budget_id"] == limits.budget_id
    result["status"] = "changed-view"
    assert ledger.input_budget_snapshot(limits.budget_id)["status"] == "active"


def test_concurrent_reservations_have_one_winner_and_no_extra_call_record():
    ledger, _limits, _now = _ledger(max_http=1)
    barrier = threading.Barrier(2)

    def reserve(index):
        barrier.wait()
        try:
            record, token = _reserve(ledger, f"call-{index}")
            token.release()
            return record.call_id
        except ModelCallBudgetError as exc:
            return exc.reason

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(reserve, range(2)))
    assert results.count("budget_busy") == 1
    assert len(ledger.records()) == 1
    assert ledger.input_budget_snapshot("authorization")["reserved_http_requests"] == 1


@pytest.mark.parametrize("status", ["failed", "timed_out", "usage_missing", "no_http", "extra_http"])
def test_unknown_or_unreserved_transmission_never_refunds_to_zero_or_reopens(status):
    ledger, _limits, _now = _ledger()
    _record, token = _reserve(ledger)
    with token:
        if status == "failed":
            ledger.failed(ModelCallFailureParams("call", "OSError"))
        elif status == "timed_out":
            ledger.timeout(ModelCallTimeoutParams("call", 1, "wall_clock"))
        else:
            _finish(ledger, actual=0, fields=() if status == "usage_missing" else ("input_tokens",),
                    http_count=0 if status == "no_http" else 2 if status == "extra_http" else 1)
        result = ledger.settle_input_budget("call")
        assert result["status"] == "usage_unknown" and result["charged_input_tokens"] == 60
        assert result["reserved_http_requests"] == 1 and result["unknown_usage_calls"] == 1
        with pytest.raises(ModelCallBudgetError, match="budget_usage_unknown"):
            _reserve(ledger, "later")
        _finish(ledger, actual=0)
        assert ledger.settle_input_budget("call") == result


@pytest.mark.parametrize("max_http,max_input,bound,reason", [(1, 100, 20, "http_budget_exhausted"), (3, 50, 21, "input_budget_exhausted")])
def test_independent_http_and_full_input_limits(max_http, max_input, bound, reason):
    ledger, _limits, _now = _ledger(max_http=max_http, max_input=max_input)
    _record, token = _reserve(ledger, bound=40)
    with token:
        _finish(ledger, actual=30)
        ledger.settle_input_budget("call")
    with pytest.raises(ModelCallBudgetError, match=reason):
        _reserve(ledger, "next", bound)
    assert len(ledger.records()) == 1


def test_input_exact_boundary_and_reported_zero_are_distinct_from_missing():
    ledger, _limits, _now = _ledger(max_input=60)
    _record, token = _reserve(ledger)
    with token:
        _finish(ledger, actual=0)
        result = ledger.settle_input_budget("call")
        assert result["charged_input_tokens"] == 0 and result["unknown_usage_calls"] == 0
    _record, token = _reserve(ledger, "next", 60)
    token.release()


@pytest.mark.parametrize("bound", [0, -1, True, None, 1.2, "20", 10**1000])
def test_no_bound_or_invalid_bound_has_no_reservation(bound):
    ledger, _limits, _now = _ledger()
    with pytest.raises(ModelCallBudgetError, match="input_bound_unavailable"):
        _reserve(ledger, bound=bound)
    assert not ledger.records()
    assert ledger.input_budget_snapshot("authorization")["reserved_http_requests"] == 0


def test_expired_budget_and_wrong_execution_identity_cannot_create_record():
    ledger, _limits, now = _ledger()
    with pytest.raises(ModelCallBudgetError, match="budget_identity_mismatch"):
        ledger.reserve_input_budget(replace(_params(), run_id="other"), budget_id="authorization", input_token_upper_bound=1)
    now[0] = 20
    with pytest.raises(ModelCallBudgetError, match="budget_expired"):
        _reserve(ledger)
    assert not ledger.records()


def test_revoke_and_reserve_share_original_lock_and_never_reset_consumption():
    ledger, limits, _now = _ledger()
    barrier = threading.Barrier(2)

    def reserve():
        barrier.wait()
        try:
            _record, token = _reserve(ledger)
            token.release()
        except ModelCallBudgetError as exc:
            assert exc.reason == "budget_revoked"

    def revoke():
        barrier.wait()
        ledger.revoke_input_budget("authorization")

    with ThreadPoolExecutor(2) as pool:
        list(pool.map(lambda f: f(), (reserve, revoke)))
    before = ledger.input_budget_snapshot("authorization")
    assert before["status"] == "revoked" and before["reserved_http_requests"] <= 1
    assert ledger.begin_input_budget(limits) == before
    with pytest.raises(ModelCallBudgetError, match="budget_revoked"):
        _reserve(ledger, "next")


def test_original_retention_preserves_budget_until_deadline_and_new_ledger_cannot_resume():
    ledger, limits, now = _ledger(max_records=1)
    _record, token = _reserve(ledger)
    with token:
        _finish(ledger)
        result = ledger.settle_input_budget("call")
    for index in range(12):
        ledger.started(ModelCallStartedParams(f"ordinary-{index}", "base", "main", 1, run_id=f"other-{index}"))
    assert ledger.input_budget_snapshot("authorization") == result
    assert ledger.begin_input_budget(limits) == result
    with pytest.raises(ModelCallBudgetError, match="budget_ledger_mismatch"):
        ModelCallLedger().begin_input_budget(limits)
    now[0] = 21
    for index in range(12, 20):
        ledger.started(ModelCallStartedParams(f"ordinary-{index}", "base", "main", 1, run_id=f"other-{index}"))
    with pytest.raises(ModelCallBudgetError, match="budget_missing"):
        ledger.input_budget_snapshot("authorization")
    with pytest.raises(ModelCallBudgetError, match="budget_expired"):
        ledger.begin_input_budget(limits)


def test_explicit_budget_does_not_evict_existing_ordinary_usage_scope_capacity():
    ledger = ModelCallLedger(ModelCallLedgerOptions(max_records=1), ModelCallLedgerContext(now=lambda: 10))
    ledger.started(_params("ordinary"))
    original_request = ledger.cumulative_summary(request_id="request")
    original_run = ledger.cumulative_summary(run_id="run")
    limits = ModelCallInputBudget("authorization", ledger.ledger_id, "owner", "thread", "task", "run", "attempt", "request",
                                  deadline=20, max_http_requests=1, max_input_tokens=100)
    ledger.begin_input_budget(limits)
    assert ledger.cumulative_summary(request_id="request") == original_request
    assert ledger.cumulative_summary(run_id="run") == original_run


def test_provider_exceeding_asserted_bound_is_visible_and_closes_budget():
    ledger, _limits, _now = _ledger()
    _record, token = _reserve(ledger, bound=20)
    with token:
        _finish(ledger, actual=30)
        result = ledger.settle_input_budget("call")
    assert result["charged_input_tokens"] == result["provider_input_tokens"] == 30
    assert result["status"] == "input_bound_violated"
    with pytest.raises(ModelCallBudgetError, match="budget_input_bound_violated"):
        _reserve(ledger, "next")
