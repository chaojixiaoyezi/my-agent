# LLM: 仅使用原模型账本、保留令牌和受控线程验证停机观察；不把逻辑终态当物理退出，不建立执行或网络替身。
# 模块用途: 验证只读 drain 快照保留准确对象和独立持有者，且不改变原准入、裁剪及用量事实。
from __future__ import annotations

import gc
import threading
import weakref

import pytest

from agent_py_agent.agent.contracts import model_call_ledger as ledger_module
from agent_py_agent.agent.contracts.model_call_ledger import (
    ModelCallAdmissionClosure,
    ModelCallFailureParams,
    ModelCallFinishParams,
    ModelCallFirstTokenParams,
    ModelCallLedger,
    ModelCallLedgerContext,
    ModelCallLedgerOptions,
    ModelCallStartedParams,
    close_model_call_admission,
    model_call_admission_closure,
    model_call_inflight_snapshot,
)


# LLM: 建立真实 ledger 明细，不替换 registry、记录或保留集合；调用编号仅为隔离测试事实。
# 函数用途: 生成无网络模型调用，供快照身份和终态合同复用。
def _start(ledger: ModelCallLedger, call_id: str):
    return ledger.started(ModelCallStartedParams(
        call_id=call_id, backend="fake", model="fake", input_tokens=7,
        request_id=f"request-{call_id}", run_id=f"run-{call_id}",
    ))


def test_empty_snapshot_does_not_create_ledger_or_close_admission():
    registry = ledger_module._ADMISSION_REGISTRY
    assert tuple(registry.ledgers) == ()
    assert ledger_module.model_call_drain_snapshot() == ()
    assert tuple(registry.ledgers) == ()
    empty_ledger = ModelCallLedger()
    assert ledger_module.model_call_drain_snapshot() == ()
    assert set(registry.ledgers) == {empty_ledger}
    assert model_call_admission_closure() is None


@pytest.mark.parametrize("status", ["started", "first_token"])
@pytest.mark.parametrize("retained", [False, True])
def test_active_snapshot_returns_original_record_and_actual_retention(status, retained):
    ledger = ModelCallLedger()
    _start(ledger, "active")
    if status == "first_token":
        ledger.first_token(ModelCallFirstTokenParams("active"))
    token = ledger.retain_call("active") if retained else None
    before = ledger.records()[0]
    summary_before = ledger.cumulative_summary(request_id="request-active")
    try:
        rows = ledger_module.model_call_drain_snapshot()
        assert isinstance(rows, tuple) and len(rows) == 1
        original_ledger, record, holders = rows[0]
        assert original_ledger is ledger and record is before
        assert isinstance(holders, tuple)
        assert holders == ((token,) if retained else ())
        assert ledger.records()[0] is before
        assert ledger.cumulative_summary(request_id="request-active") == summary_before
        assert model_call_admission_closure() is None
    finally:
        if token is not None:
            token.release()


@pytest.mark.parametrize("status", ["failed", "finished"])
def test_terminal_call_stays_visible_until_each_actual_holder_releases(status):
    ledger = ModelCallLedger()
    _start(ledger, "terminal")
    caller = ledger.retain_call("terminal")
    worker = ledger.retain_call("terminal")
    try:
        if status == "failed":
            ledger.failed(ModelCallFailureParams("terminal", "FakeFailure", "FAKE_FAILURE"))
        else:
            ledger.finished(ModelCallFinishParams("terminal", output_tokens=3))
        record = ledger.records()[0]
        assert model_call_inflight_snapshot()[0] == 0
        rows = ledger_module.model_call_drain_snapshot()
        assert rows[0][0] is ledger and rows[0][1] is record
        assert set(rows[0][2]) == {caller, worker}
        caller.release()
        caller.release()
        assert ledger_module.model_call_drain_snapshot() == ((ledger, record, (worker,)),)
        assert worker.__enter__() is worker
        worker.release()
        assert ledger_module.model_call_drain_snapshot() == ()
        assert ledger.records()[0] is record
    finally:
        caller.release()
        worker.release()


def test_old_active_record_is_observed_without_age_trim_or_clock_read():
    now = [1.0]
    ledger = ModelCallLedger(
        options=ModelCallLedgerOptions(max_records=1, open_call_stale_seconds=1),
        context=ModelCallLedgerContext(now=lambda: now[0]),
    )
    record = _start(ledger, "old")
    now[0] = 100_000.0
    before = ledger.stale_open_calls_trimmed()
    assert ledger_module.model_call_drain_snapshot() == ((ledger, record, ()),)
    assert ledger.records() == (record,)
    assert ledger.stale_open_calls_trimmed() == before == 0


def test_shutdown_failed_call_retains_worker_responsibility():
    ledger = ModelCallLedger()
    _start(ledger, "shutdown")
    holder = ledger.retain_call("shutdown")
    closure = ModelCallAdmissionClosure("FakeShutdown", "FAKE_SHUTDOWN")
    try:
        settled = close_model_call_admission(closure)
        assert len(settled) == 1 and settled[0].status == "failed"
        assert ledger_module.model_call_drain_snapshot() == ((ledger, settled[0], (holder,)),)
        assert model_call_admission_closure() is closure
        assert close_model_call_admission(closure) == ()
        holder.release()
        assert ledger_module.model_call_drain_snapshot() == ()
    finally:
        holder.release()


def test_snapshot_does_not_turn_weak_registry_into_persistent_ownership():
    ledger = ModelCallLedger()
    _start(ledger, "weak")
    reference = weakref.ref(ledger)
    rows = ledger_module.model_call_drain_snapshot()
    del ledger
    gc.collect()
    assert reference() is rows[0][0]
    del rows
    gc.collect()
    assert reference() is None
    assert ledger_module.model_call_drain_snapshot() == ()


def test_different_threads_keep_original_ledgers_and_release_independently():
    ready = threading.Barrier(3)
    release = threading.Event()
    facts = []
    failures = []

    def run(call_id):
        ledger = ModelCallLedger()
        _start(ledger, call_id)
        holder = ledger.retain_call(call_id)
        try:
            record = ledger.failed(ModelCallFailureParams(call_id, "FakeFailure"))
            facts.append((ledger, record, holder))
            ready.wait(timeout=2)
            assert release.wait(2)
        except BaseException as exc:
            failures.append(exc)
        finally:
            holder.release()

    threads = [threading.Thread(target=run, args=(name,)) for name in ("one", "two")]
    try:
        for thread in threads:
            thread.start()
        ready.wait(timeout=2)
        rows = ledger_module.model_call_drain_snapshot()
        assert len(rows) == 2
        by_call = {record.call_id: (ledger, record, holders) for ledger, record, holders in rows}
        for ledger, record, holder in facts:
            row = by_call[record.call_id]
            assert row[0] is ledger and row[1] is record and row[2] == (holder,)
    finally:
        release.set()
        for thread in threads:
            thread.join(timeout=2)
        assert all(not thread.is_alive() for thread in threads)
    assert failures == []
    assert ledger_module.model_call_drain_snapshot() == ()
