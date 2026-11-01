"""索引优化按实际重建次数验收，不用抖动的墙钟断言或裁掉活调用换性能。"""
from dataclasses import replace

import pytest

from agent_py_agent.agent.contracts.model_call_ledger import (
    ModelCallActivityParams,
    ModelCallFinishParams,
    ModelCallLedger,
    ModelCallLedgerContext,
    ModelCallLedgerOptions,
    ModelCallProviderAttemptParams,
    ModelCallStartedParams,
    ModelCallTimeoutParams,
)


def started(ledger, call_id):
    return ledger.started(ModelCallStartedParams(call_id, "fake", "model", 10,
                                                request_id="request", run_id="run"))


def rebuild_spy(ledger, monkeypatch):
    calls = []
    original = ledger._rebuild_index

    def rebuild():
        calls.append(len(ledger.records()))
        return original()

    monkeypatch.setattr(ledger, "_rebuild_index", rebuild)
    return calls


def assert_index(ledger):
    records = ledger.records()
    assert ledger._index == {record.call_id: index for index, record in enumerate(records)}
    assert all(ledger._require_record(record.call_id) is record for record in records)


def test_default_window_append_does_not_rebuild_index(monkeypatch):
    ledger = ModelCallLedger()
    calls = rebuild_spy(ledger, monkeypatch)
    for index in range(128):
        started(ledger, str(index))
        ledger.finished(ModelCallFinishParams(str(index), input_tokens=10, output_tokens=1))
    assert len(ledger.records()) == ledger.options.max_records == 128
    assert_index(ledger)
    assert calls == []


@pytest.mark.parametrize("pinned", [False, True])
def test_many_live_or_pinned_records_never_trigger_noop_rebuild(monkeypatch, pinned):
    ledger = ModelCallLedger()
    calls = rebuild_spy(ledger, monkeypatch)
    tokens = []
    try:
        for index in range(384):
            call_id = str(index)
            started(ledger, call_id)
            if pinned:
                tokens.append(ledger.retain_call(call_id))
                ledger.finished(ModelCallFinishParams(call_id, input_tokens=10, output_tokens=1))
        assert len(ledger.records()) == 384
        assert_index(ledger)
        assert calls == []
    finally:
        for token in tokens:
            token.release()
    if pinned:
        assert len(ledger.records()) == 128
        assert len(calls) == 256
        assert_index(ledger)


def test_rebuild_happens_only_after_real_eviction_and_totals_survive(monkeypatch):
    ledger = ModelCallLedger(options=ModelCallLedgerOptions(max_records=4))
    calls = rebuild_spy(ledger, monkeypatch)
    for index in range(20):
        started(ledger, str(index))
        ledger.finished(ModelCallFinishParams(str(index), input_tokens=10, output_tokens=2))
        assert_index(ledger)
    assert len(calls) == 16
    assert [record.call_id for record in ledger.records()] == [str(index) for index in range(16, 20)]
    summary = ledger.cumulative_summary(request_id="request")
    assert summary["physical_model_attempt_count"] == summary["status_counts"]["finished"] == 20
    assert summary["accounted_input_tokens"] == 200 and summary["output_tokens"] == 40


def test_upsert_replacement_updates_one_index_and_usage_delta_once(monkeypatch):
    ledger = ModelCallLedger()
    calls = rebuild_spy(ledger, monkeypatch)
    original = started(ledger, "same")
    replacement = replace(original, accounted_input_tokens=123, output_tokens=7)
    with ledger._lock:
        ledger._append_or_replace(replacement)
        ledger._append_or_replace(replacement)
    assert ledger.records() == (replacement,)
    summary = ledger.cumulative_summary(run_id="run")
    assert summary["physical_model_attempt_count"] == 1
    assert summary["accounted_input_tokens"] == 123 and summary["output_tokens"] == 7
    assert calls == []
    assert_index(ledger)


def test_stale_cleanup_keeps_active_refresh_and_retention_then_trims_once(monkeypatch):
    now = [10.0]
    ledger = ModelCallLedger(options=ModelCallLedgerOptions(max_records=2, open_call_stale_seconds=20),
                             context=ModelCallLedgerContext(now=lambda: now[0]))
    calls = rebuild_spy(ledger, monkeypatch)
    for call_id in ("stale", "live", "pinned"):
        started(ledger, call_id)
    token = ledger.retain_call("pinned")
    try:
        now[0] = 29
        ledger.activity(ModelCallActivityParams("live", 2))
        now[0] = 31
        started(ledger, "next")
        assert {record.call_id for record in ledger.records()} == {"live", "pinned", "next"}
        assert ledger.stale_open_calls_trimmed() == 1
        assert calls == [3]
        ledger.timeout(ModelCallTimeoutParams("pinned", 1, "wall_clock"))
        ledger.provider_attempt(ModelCallProviderAttemptParams("pinned", "late", "failed"))
        assert ledger._require_record("pinned").status == "timed_out"
        assert ledger.cumulative_summary(run_id="run")["provider_http_attempt_count"] == 1
        assert_index(ledger)
    finally:
        token.release()
    assert_index(ledger)
