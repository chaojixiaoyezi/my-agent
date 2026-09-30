"""唤醒毒丸第 2 步：尝试账、结案、重放与发布层第三位置，全部走真实 ConversationStore。

不接调度、不调模型。正向对照与反向断言成对：结案前后信封冻结内容不变、同键再发布不复活、关联观察同步结掉、
坏账不静默清零、重放拒绝时不改任何文件。
"""
from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import replace

import pytest

from agent_py_agent.agent.conversation.models import ConversationThread, WakeSignal
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.conversation.store_wake_attempts import (
    WAKE_REPLAY_DOMAIN_TERMINAL,
    WAKE_REPLAY_NOT_FOUND,
    WAKE_REPLAY_PENDING_CONFLICT,
    WAKE_REPLAY_SOURCE_UNREADABLE,
    WakeAttemptStart,
)
from agent_py_agent.agent.conversation.store_wake_publication import _stable_signal
from agent_py_agent.agent.conversation.wake_poison import (
    WAKE_ATTEMPT_NEUTRAL,
    WAKE_ATTEMPT_SUCCESS,
    WAKE_POISON_SAME_CAUSE_LIMIT,
    WAKE_REASON_ATTEMPT_ABANDONED,
    WAKE_REASON_GATEWAY_STOPPED,
    WAKE_REASON_LEDGER_CORRUPT,
    WAKE_STATUS_FAILED_PERMANENTLY,
    WAKE_UNCOUNTED_STALL_SECONDS,
    WAKE_VERDICT_FAILURE,
    WAKE_VERDICT_NEUTRAL,
    WakeAttemptVerdict,
    WakePoisonState,
    ledger_corrupt_decision,
)
from agent_py_agent.agent.gateway_parts.daemon_metadata import build_process_identity
from agent_py_agent.agent.memory_store.retention_scan import _conversation_related_paths
from agent_py_agent.agent.runtime_errors import DataCorruptionError

BUG = WakeAttemptVerdict(WAKE_VERDICT_FAILURE, "error:SKILL_TASK_BINDING_INVALID")


def _store(tmp_path) -> ConversationStore:
    store = ConversationStore(tmp_path / "conv")
    for thread_id in ("thread-a", "thread-b"):
        store.threads.write(ConversationThread(thread_id=thread_id, canonical_user_id="u"))
    return store


def _raise(store, *, thread="thread-a", key="", reason="session_task") -> WakeSignal:
    return store.wakes.raise_signal({"thread_id": thread, "reason": reason, "summary": "派活正文摘要",
                                     "metadata": {"session_task_id": "stask-1"}, "dedupe_key": key, "now": 10.0})


def _with_observation(store):
    return store.wakes.append_observation(
        {"thread_id": "thread-a", "event_type": "subagent_runner_finished", "summary": "子任务完成",
         "requires_main_agent": True, "now": 20.0},
        {"thread_id": "thread-a", "reason": "subagent_runner_finished", "dedupe_key": "child-1:DONE", "now": 20.1},
    )


def _poison(store, signal, *, verdict=BUG):
    outcome = None
    for attempt in range(WAKE_POISON_SAME_CAUSE_LIMIT):
        store.wakes.attempts.begin(signal, WakeAttemptStart(f"claim-{attempt}"), now=100.0 + attempt)
        outcome = store.wakes.attempts.record(signal, verdict, now=100.5 + attempt, error=RuntimeError("boom"))
    return outcome


def _dead_pid() -> int:
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait(timeout=10)
    return child.pid


def _json(path):
    return json.loads(path.read_text(encoding="utf-8"))


class TestLedger:
    def test_failures_accumulate_and_decide_at_the_limit(self, tmp_path):
        store = _store(tmp_path)
        signal = _raise(store)
        for attempt in range(WAKE_POISON_SAME_CAUSE_LIMIT - 1):
            store.wakes.attempts.begin(signal, WakeAttemptStart("c"), now=float(attempt))
            outcome = store.wakes.attempts.record(signal, BUG, now=float(attempt), error=RuntimeError("x"))
            assert outcome.decision is None
        outcome = _poison(store, signal)
        assert outcome.decision is not None and outcome.decision.reason_code == BUG.reason_code
        ledger = _json(store.storage.wake_attempt_path(signal.wake_signal_id))
        assert ledger["in_flight"] is None and ledger["last_error"]["type"] == "RuntimeError"
        assert ledger["thread_id"] == "thread-a"
        state, error = store.wakes.attempts.state_report(signal.wake_signal_id)
        assert error is None and state.total_count == 2 * WAKE_POISON_SAME_CAUSE_LIMIT - 1

    def test_success_deletes_the_ledger_and_neutral_only_clears_in_flight(self, tmp_path):
        store = _store(tmp_path)
        signal = _raise(store)
        store.wakes.attempts.begin(signal, WakeAttemptStart("c1"), now=1.0)
        store.wakes.attempts.record(signal, BUG, now=2.0)
        store.wakes.attempts.begin(signal, WakeAttemptStart("c2"), now=3.0)
        store.wakes.attempts.record(signal, WAKE_ATTEMPT_NEUTRAL, now=4.0)
        ledger = _json(store.storage.wake_attempt_path(signal.wake_signal_id))
        assert ledger["in_flight"] is None and ledger["state"]["same_cause_count"] == 1
        store.wakes.attempts.record(signal, WAKE_ATTEMPT_SUCCESS, now=5.0)
        assert not store.storage.wake_attempt_path(signal.wake_signal_id).exists()
        assert store.wakes.attempts.state_report(signal.wake_signal_id) == (WakePoisonState(), None)

    def test_dead_in_flight_attempt_is_counted_as_abandoned_but_a_live_one_is_not(self, tmp_path):
        store = _store(tmp_path)
        signal = _raise(store)
        path = store.storage.wake_attempt_path(signal.wake_signal_id)
        store.wakes.attempts.begin(signal, WakeAttemptStart("live"), now=1.0)
        live = store.wakes.attempts.begin(signal, WakeAttemptStart("again"), now=2.0)
        assert live.abandoned is False and live.state == WakePoisonState()
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.3)"])
        dead = build_process_identity(child.pid)
        child.wait(timeout=10)
        ledger = _json(path)
        ledger["in_flight"]["owner_process"] = dead
        path.write_text(json.dumps(ledger), encoding="utf-8")
        outcome = store.wakes.attempts.begin(signal, WakeAttemptStart("after-crash"), now=3.0)
        assert outcome.abandoned is True
        assert (outcome.state.reason_code, outcome.state.same_cause_count) == ("attempt:abandoned", 1)
        assert _json(path)["in_flight"]["claim_id"] == "after-crash"

    def test_a_batch_that_died_mid_attempt_only_isolates_its_members(self, tmp_path):
        store = _store(tmp_path)
        signal = _raise(store)
        path = store.storage.wake_attempt_path(signal.wake_signal_id)
        store.wakes.attempts.begin(signal, WakeAttemptStart("batch", batch_size=3), now=1.0)
        ledger = _json(path)
        assert ledger["in_flight"]["batch_size"] == 3
        ledger["in_flight"]["owner_process"] = {**ledger["in_flight"]["owner_process"], "pid": _dead_pid()}
        path.write_text(json.dumps(ledger), encoding="utf-8")
        outcome = store.wakes.attempts.begin(signal, WakeAttemptStart("alone"), now=2.0)
        assert outcome.abandoned is True and outcome.decision is None
        assert (outcome.state.batch_failures, outcome.state.total_count) == (1, 0)

    def test_unknowable_owner_identity_is_not_treated_as_dead(self, tmp_path):
        store = _store(tmp_path)
        signal = _raise(store)
        path = store.storage.wake_attempt_path(signal.wake_signal_id)
        store.wakes.attempts.begin(signal, WakeAttemptStart("elsewhere"), now=1.0)
        ledger = _json(path)
        ledger["in_flight"]["owner_process"] = {**ledger["in_flight"]["owner_process"], "host_id": "another-host"}
        path.write_text(json.dumps(ledger), encoding="utf-8")
        outcome = store.wakes.attempts.begin(signal, WakeAttemptStart("here"), now=2.0)
        assert outcome.abandoned is False and outcome.state == WakePoisonState()

    def test_uncounted_stall_alert_is_persisted_once_per_window(self, tmp_path):
        store = _store(tmp_path)
        signal = _raise(store)
        wait = WakeAttemptVerdict(WAKE_VERDICT_NEUTRAL, "error:PROVIDER_CONNECTION_FAILED")
        day = WAKE_UNCOUNTED_STALL_SECONDS
        first = store.wakes.attempts.record(signal, wait, now=0.0)
        assert first.stall_alert is None and first.state.uncounted_count == 1
        due = store.wakes.attempts.record(signal, wait, now=day)
        assert due.stall_alert is not None and due.stall_alert.alert_number == 1
        assert _json(store.storage.wake_attempt_path(signal.wake_signal_id))["state"]["stall_alerts"] == 1
        assert store.wakes.attempts.record(signal, wait, now=day + 60).stall_alert is None
        again = store.wakes.attempts.record(signal, wait, now=2 * day)
        assert again.stall_alert is not None and again.stall_alert.alert_number == 2
        assert again.decision is None

    def test_a_60_second_clock_rollback_keeps_the_ledger_readable(self, tmp_path):
        store = _store(tmp_path)
        signal = _raise(store)
        store.wakes.attempts.begin(signal, WakeAttemptStart("c1"), now=1000.0)
        store.wakes.attempts.record(signal, BUG, now=1000.0)
        store.wakes.attempts.begin(signal, WakeAttemptStart("c2"), now=940.0)
        store.wakes.attempts.record(signal, BUG, now=940.0)
        state, error = store.wakes.attempts.state_report(signal.wake_signal_id)
        assert error is None and (state.same_cause_count, state.first_failed_at, state.last_failed_at) == (2, 1000.0, 1000.0)
        store.wakes.attempts.begin(signal, WakeAttemptStart("c3"), now=900.0)
        assert store.wakes.attempts.record(signal, BUG, now=900.0).state.same_cause_count == 3

    def test_an_empty_reason_is_rejected_and_the_ledger_is_untouched(self, tmp_path):
        store = _store(tmp_path)
        signal = _raise(store)
        store.wakes.attempts.record(signal, BUG, now=1.0)
        path = store.storage.wake_attempt_path(signal.wake_signal_id)
        before = path.read_bytes()
        with pytest.raises(ValueError):
            store.wakes.attempts.record(signal, WakeAttemptVerdict(WAKE_VERDICT_FAILURE, ""), now=2.0)
        assert path.read_bytes() == before

    # 写账前按读回同一口径校验：判定层即使算出自相矛盾的状态，也不会被写成一份读不回来的坏账。
    @pytest.mark.parametrize("entry", ["record", "begin"])
    def test_a_contradictory_state_is_never_written(self, tmp_path, monkeypatch, entry):
        from agent_py_agent.agent.conversation import store_wake_attempts

        store = _store(tmp_path)
        signal = _raise(store)
        path = store.storage.wake_attempt_path(signal.wake_signal_id)
        store.wakes.attempts.begin(signal, WakeAttemptStart("batch", batch_size=1), now=1.0)
        ledger = _json(path)
        ledger["in_flight"]["owner_process"] = {**ledger["in_flight"]["owner_process"], "pid": _dead_pid()}
        path.write_text(json.dumps(ledger), encoding="utf-8")
        before = path.read_bytes()
        broken = WakePoisonState(reason_code="", same_cause_count=1, total_count=1, first_failed_at=2.0, last_failed_at=2.0)
        monkeypatch.setattr(store_wake_attempts, "next_poison_state", lambda *_args, **_kwargs: broken)
        with pytest.raises(ValueError):
            if entry == "record":
                store.wakes.attempts.record(signal, BUG, now=2.0)
            else:
                store.wakes.attempts.begin(signal, WakeAttemptStart("after-crash"), now=2.0)
        assert path.read_bytes() == before

    def test_corrupt_ledger_is_reported_not_silently_reset(self, tmp_path):
        store = _store(tmp_path)
        signal = _raise(store)
        path = store.storage.wake_attempt_path(signal.wake_signal_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"schema": "wake-attempts.v1", "state": {"same_cause_count": "four"}}', encoding="utf-8")
        state, error = store.wakes.attempts.state_report(signal.wake_signal_id)
        assert state == WakePoisonState() and error is not None and error["category"] == "data_corruption"
        with pytest.raises(DataCorruptionError):
            store.wakes.attempts.record(signal, BUG, now=1.0)
        assert "four" in path.read_text(encoding="utf-8")


class TestQuarantine:
    def test_settles_out_of_pending_with_frozen_content_intact(self, tmp_path):
        store = _store(tmp_path)
        signal = _raise(store)
        outcome = _poison(store, signal)
        result = store.wakes.attempts.quarantine(signal.wake_signal_id, outcome.decision, now=200.0)
        settled = result.settled
        assert settled is not None and settled.status == WAKE_STATUS_FAILED_PERMANENTLY
        assert (result.source_unreadable, result.ledger_preserved_at) == (False, "")
        assert store.wakes.pending_one(signal.wake_signal_id) is None
        assert signal.wake_signal_id not in {item.wake_signal_id for item in store.wakes.pending(limit=0)}
        assert not store.storage.wake_attempt_path(signal.wake_signal_id).exists()
        record = _json(store.storage.wake_quarantine_path(signal.wake_signal_id))
        assert _stable_signal(WakeSignal.from_dict(record)) == _stable_signal(signal)
        assert record["quarantine"]["decision"]["reason_code"] == BUG.reason_code
        assert record["quarantine"]["last_error"]["type"] == "RuntimeError"
        again = store.wakes.attempts.quarantine(signal.wake_signal_id, outcome.decision, now=201.0)
        assert again.settled is None and again.source_unreadable is False

    def test_linked_observation_is_closed_so_the_fallback_lane_cannot_rerun_it(self, tmp_path):
        store = _store(tmp_path)
        observation, signal = _with_observation(store)
        assert observation.observation_id in {item.observation_id for item in store.observations.unhandled_requiring_main()}
        store.wakes.attempts.quarantine(signal.wake_signal_id, _poison(store, signal).decision, now=200.0)
        assert observation.observation_id not in {
            item.observation_id for item in store.observations.unhandled_requiring_main()}

    def test_republishing_the_same_key_does_not_revive_a_quarantined_event(self, tmp_path):
        store = _store(tmp_path)
        signal = _raise(store, key="stask-1:body")
        store.wakes.attempts.quarantine(signal.wake_signal_id, _poison(store, signal).decision, now=200.0)
        again = _raise(store, key="stask-1:body")
        assert again.wake_signal_id == signal.wake_signal_id and again.status == WAKE_STATUS_FAILED_PERMANENTLY
        assert store.wakes.pending(limit=0) == []
        assert store.wakes.delivery_receipt("thread-a", "stask-1:body") == WAKE_STATUS_FAILED_PERMANENTLY

    def test_positive_control_a_handled_event_does_start_a_new_generation(self, tmp_path):
        store = _store(tmp_path)
        signal = _raise(store, key="goal:tick")
        store.wakes.mark_handled(signal.wake_signal_id, now=50.0)
        again = _raise(store, key="goal:tick")
        assert again.wake_signal_id != signal.wake_signal_id and again.status == "pending"

    def test_listing_shows_structured_fields_only(self, tmp_path):
        store = _store(tmp_path)
        signal = _raise(store)
        store.wakes.attempts.quarantine(signal.wake_signal_id, _poison(store, signal).decision, now=200.0)
        broken = store.storage.wake_quarantine_dir / "broken.json"
        broken.write_text("{not json", encoding="utf-8")
        rows, errors = store.wakes.attempts.quarantined()
        assert rows == [{"wake_signal_id": signal.wake_signal_id, "thread_id": "thread-a", "reason": "session_task",
                         "reason_code": BUG.reason_code, "same_cause_count": WAKE_POISON_SAME_CAUSE_LIMIT,
                         "total_count": WAKE_POISON_SAME_CAUSE_LIMIT, "mixed_causes": False,
                         "quarantined_at": 200.0, "replay_count": 0}]
        assert len(errors) == 1 and "派活正文摘要" not in json.dumps(rows, ensure_ascii=False)


class TestReplay:
    def test_replay_restores_the_frozen_signal_and_archives_the_record(self, tmp_path):
        store = _store(tmp_path)
        signal = _raise(store, key="stask-1:body")
        store.wakes.attempts.quarantine(signal.wake_signal_id, _poison(store, signal).decision, now=200.0)
        # 结案与删账之间崩溃会留下旧尝试账；重放必须清掉它，否则重放后第一次失败就接着旧计数结案。
        stale = store.storage.wake_attempt_path(signal.wake_signal_id)
        stale.write_text(json.dumps({"schema": "wake-attempts.v1", "state": {"same_cause_count": 4, "total_count": 4}}),
                         encoding="utf-8")
        result = store.wakes.attempts.replay(signal.wake_signal_id, now=300.0, domain_terminal=lambda _s: False)
        assert result.ok is True and result.error_code == ""
        restored = store.wakes.pending_one(signal.wake_signal_id)
        assert restored is not None and _stable_signal(restored) == _stable_signal(signal)
        assert restored.status == "pending" and restored.handled_at == 0.0
        assert not store.storage.wake_quarantine_path(signal.wake_signal_id).exists()
        archive = store.storage.wake_replayed_dir / signal.wake_signal_id / "1.json"
        assert _json(archive)["quarantine"]["replayed_at"] == 300.0
        assert store.wakes.attempts.state_report(signal.wake_signal_id) == (WakePoisonState(), None)
        assert store.wakes.delivery_receipt("thread-a", "stask-1:body") == "pending"

    def test_a_replayed_wake_that_fails_again_is_settled_again_and_counted(self, tmp_path):
        store = _store(tmp_path)
        signal = _raise(store)
        for round_number in (1, 2):
            store.wakes.attempts.quarantine(signal.wake_signal_id, _poison(store, signal).decision, now=200.0)
            if round_number == 1:
                assert store.wakes.attempts.replay(signal.wake_signal_id, now=300.0).ok
        rows, _errors = store.wakes.attempts.quarantined()
        assert [row["replay_count"] for row in rows] == [1]
        assert store.wakes.attempts.replay(signal.wake_signal_id, now=400.0).ok
        archive = store.storage.wake_replayed_dir / signal.wake_signal_id
        assert sorted(path.name for path in archive.glob("*.json")) == ["1.json", "2.json"]

    @pytest.mark.parametrize("case", ["domain_terminal", "pending_conflict", "unknown"])
    def test_refusals_change_nothing(self, tmp_path, case):
        store = _store(tmp_path)
        signal = _raise(store)
        store.wakes.attempts.quarantine(signal.wake_signal_id, _poison(store, signal).decision, now=200.0)
        quarantine = store.storage.wake_quarantine_path(signal.wake_signal_id)
        before = quarantine.read_text(encoding="utf-8")
        if case == "pending_conflict":
            pending = replace(signal, status="pending")
            store.storage.wake_signal_path(pending).write_text(json.dumps(pending.to_dict()), encoding="utf-8")
        target = "no-such-wake" if case == "unknown" else signal.wake_signal_id
        result = store.wakes.attempts.replay(target, now=300.0, domain_terminal=lambda _s: case == "domain_terminal")
        expected = {"domain_terminal": WAKE_REPLAY_DOMAIN_TERMINAL, "pending_conflict": WAKE_REPLAY_PENDING_CONFLICT,
                    "unknown": WAKE_REPLAY_NOT_FOUND}[case]
        assert (result.ok, result.error_code) == (False, expected)
        assert quarantine.read_text(encoding="utf-8") == before
        assert not (store.storage.wake_replayed_dir / signal.wake_signal_id).exists()


def test_thread_deletion_collects_this_threads_poison_files_only(tmp_path):
    store = _store(tmp_path)
    mine, other = _raise(store), _raise(store, thread="thread-b")
    store.wakes.attempts.quarantine(mine.wake_signal_id, _poison(store, mine).decision, now=200.0)
    store.wakes.attempts.replay(mine.wake_signal_id, now=300.0)
    store.wakes.attempts.begin(mine, WakeAttemptStart("c"), now=400.0)
    store.wakes.attempts.record(mine, BUG, now=401.0)
    store.wakes.attempts.begin(other, WakeAttemptStart("c"), now=400.0)
    store.wakes.attempts.record(other, BUG, now=401.0)
    paths, errors = _conversation_related_paths(store.storage.root, store.threads.load("thread-a"))
    names = {path.relative_to(store.storage.root.resolve()).as_posix() for path in paths}
    assert errors == []
    assert f"wake_queue/attempts/{mine.wake_signal_id}.json" in names
    assert f"wake_queue/quarantine/replayed/{mine.wake_signal_id}/1.json" in names
    assert f"wake_queue/attempts/{other.wake_signal_id}.json" not in names


def _kill_in_flight(path, *, stopping_at=None):
    """把尝试账里的 in_flight 改成已死进程；stopping_at 非空时再打上停机标记。"""
    ledger = _json(path)
    ledger["in_flight"]["owner_process"] = {**ledger["in_flight"]["owner_process"], "pid": _dead_pid()}
    if stopping_at is not None:
        ledger["in_flight"]["stopping_at"] = stopping_at
    path.write_text(json.dumps(ledger), encoding="utf-8")


# 第 3 步 C1：preflight / mark_stopping / discard / has_ledger（存储层，不接线）。
class TestPreflightAndStopping:
    def test_no_ledger_means_empty_state_and_nothing_is_written(self, tmp_path):
        store = _store(tmp_path)
        signal = _raise(store)
        assert store.wakes.attempts.has_ledger(signal.wake_signal_id) is False
        outcome = store.wakes.attempts.preflight(signal, now=1.0)
        assert (outcome.state, outcome.decision, outcome.abandoned) == (WakePoisonState(), None, False)
        assert not store.storage.wake_attempt_path(signal.wake_signal_id).exists()

    def test_a_live_in_flight_attempt_is_left_untouched(self, tmp_path):
        store = _store(tmp_path)
        signal = _raise(store)
        path = store.storage.wake_attempt_path(signal.wake_signal_id)
        store.wakes.attempts.begin(signal, WakeAttemptStart("live"), now=1.0)
        before = path.read_bytes()
        outcome = store.wakes.attempts.preflight(signal, now=2.0)
        assert outcome.abandoned is False and outcome.state == WakePoisonState()
        assert path.read_bytes() == before and store.wakes.attempts.has_ledger(signal.wake_signal_id)

    def test_a_dead_attempt_with_a_stopping_mark_is_not_counted(self, tmp_path):
        store = _store(tmp_path)
        signal = _raise(store)
        path = store.storage.wake_attempt_path(signal.wake_signal_id)
        store.wakes.attempts.begin(signal, WakeAttemptStart("deploy"), now=1.0)
        assert store.wakes.attempts.mark_stopping(signal.wake_signal_id, "deploy", now=5.0) is True
        assert _json(path)["in_flight"]["stopping_at"] == 5.0
        _kill_in_flight(path, stopping_at=5.0)
        outcome = store.wakes.attempts.preflight(signal, now=9.0)
        assert outcome.abandoned is True and outcome.decision is None
        assert (outcome.state.same_cause_count, outcome.state.total_count) == (0, 0)
        assert (outcome.state.uncounted_reason_code, outcome.state.uncounted_count) == (WAKE_REASON_GATEWAY_STOPPED, 1)
        assert _json(path)["in_flight"] is None

    def test_a_dead_attempt_without_a_mark_is_counted_as_abandoned(self, tmp_path):
        store = _store(tmp_path)
        signal = _raise(store)
        path = store.storage.wake_attempt_path(signal.wake_signal_id)
        store.wakes.attempts.begin(signal, WakeAttemptStart("kill-9"), now=1.0)
        _kill_in_flight(path)
        outcome = store.wakes.attempts.preflight(signal, now=9.0)
        assert outcome.abandoned is True
        assert (outcome.state.reason_code, outcome.state.same_cause_count) == (WAKE_REASON_ATTEMPT_ABANDONED, 1)
        assert _json(path)["in_flight"] is None
        # 再来一次 preflight：没有 in_flight 就不再补记，也不写盘
        before = path.read_bytes()
        again = store.wakes.attempts.preflight(signal, now=10.0)
        assert again.abandoned is False and again.state.same_cause_count == 1 and path.read_bytes() == before

    def test_a_dead_batch_with_a_mark_stays_uncounted_and_a_dead_batch_without_isolates(self, tmp_path):
        store = _store(tmp_path)
        signal = _raise(store)
        path = store.storage.wake_attempt_path(signal.wake_signal_id)
        store.wakes.attempts.begin(signal, WakeAttemptStart("batch", batch_size=3), now=1.0)
        _kill_in_flight(path)
        outcome = store.wakes.attempts.preflight(signal, now=2.0)
        assert (outcome.state.batch_failures, outcome.state.total_count) == (1, 0)
        store.wakes.attempts.begin(signal, WakeAttemptStart("batch-2", batch_size=3), now=3.0)
        _kill_in_flight(path, stopping_at=4.0)
        outcome = store.wakes.attempts.preflight(signal, now=5.0)
        assert (outcome.state.batch_failures, outcome.state.total_count, outcome.state.uncounted_reason_code) == (
            1, 0, WAKE_REASON_GATEWAY_STOPPED)

    def test_begin_also_honours_the_stopping_mark(self, tmp_path):
        store = _store(tmp_path)
        signal = _raise(store)
        path = store.storage.wake_attempt_path(signal.wake_signal_id)
        store.wakes.attempts.begin(signal, WakeAttemptStart("deploy"), now=1.0)
        _kill_in_flight(path, stopping_at=2.0)
        outcome = store.wakes.attempts.begin(signal, WakeAttemptStart("after-restart"), now=3.0)
        assert outcome.abandoned is True and outcome.state.total_count == 0
        assert outcome.state.uncounted_reason_code == WAKE_REASON_GATEWAY_STOPPED
        assert _json(path)["in_flight"]["claim_id"] == "after-restart"

    @pytest.mark.parametrize("case", ["claim_mismatch", "other_process", "no_ledger", "no_in_flight"])
    def test_mark_stopping_only_marks_this_processes_own_attempt(self, tmp_path, case):
        store = _store(tmp_path)
        signal = _raise(store)
        path = store.storage.wake_attempt_path(signal.wake_signal_id)
        claim = "mine"
        if case != "no_ledger":
            store.wakes.attempts.begin(signal, WakeAttemptStart("mine"), now=1.0)
        if case == "no_in_flight":
            store.wakes.attempts.record(signal, BUG, now=2.0)
        if case == "other_process":
            ledger = _json(path)
            ledger["in_flight"]["owner_process"] = {**ledger["in_flight"]["owner_process"], "host_id": "another-host"}
            path.write_text(json.dumps(ledger), encoding="utf-8")
        if case == "claim_mismatch":
            claim = "theirs"
        before = path.read_bytes() if path.exists() else None
        assert store.wakes.attempts.mark_stopping(signal.wake_signal_id, claim, now=5.0) is False
        assert (path.read_bytes() if path.exists() else None) == before

    def test_mark_stopping_on_a_corrupt_ledger_is_false_and_leaves_it_for_preflight(self, tmp_path):
        store = _store(tmp_path)
        signal = _raise(store)
        path = store.storage.wake_attempt_path(signal.wake_signal_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        assert store.wakes.attempts.mark_stopping(signal.wake_signal_id, "mine", now=5.0) is False
        assert path.read_text(encoding="utf-8") == "{not json"
        with pytest.raises(DataCorruptionError):
            store.wakes.attempts.preflight(signal, now=6.0)

    def test_stopping_mark_does_not_replace_the_result_a_worker_records_before_exit(self, tmp_path):
        store = _store(tmp_path)
        signal = _raise(store)
        path = store.storage.wake_attempt_path(signal.wake_signal_id)
        store.wakes.attempts.begin(signal, WakeAttemptStart("deploy"), now=1.0)
        store.wakes.attempts.mark_stopping(signal.wake_signal_id, "deploy", now=2.0)
        store.wakes.attempts.record(signal, BUG, now=3.0, error=RuntimeError("late"))
        ledger = _json(path)
        assert ledger["in_flight"] is None and ledger["state"]["same_cause_count"] == 1
        assert store.wakes.attempts.preflight(signal, now=4.0).abandoned is False

    def test_discard_deletes_without_reading_and_tolerates_absence(self, tmp_path):
        store = _store(tmp_path)
        signal = _raise(store)
        path = store.storage.wake_attempt_path(signal.wake_signal_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\xff\xfe not even text")
        store.wakes.attempts.discard(signal.wake_signal_id)
        assert not path.exists()
        store.wakes.attempts.discard(signal.wake_signal_id)


# 第 3 步 C1：结案时信封读不出、尝试账读不出。
class TestQuarantineUnreadableAndCorrupt:
    def test_unreadable_envelope_is_moved_aside_byte_for_byte_and_settled(self, tmp_path):
        store = _store(tmp_path)
        observation, signal = _with_observation(store)
        pending = store.storage.wake_signal_path(signal)
        garbage = b"{not json \xe4\xb8\xad"
        pending.write_bytes(garbage)
        store.wakes.attempts.begin(signal, WakeAttemptStart("c"), now=1.0)
        result = store.wakes.attempts.quarantine(signal.wake_signal_id, _poison(store, signal).decision, now=200.0)
        assert (result.settled, result.source_unreadable) == (None, True)
        moved = store.storage.wake_quarantine_unreadable_path(signal.wake_signal_id)
        assert moved.read_bytes() == garbage and not pending.exists()
        assert not store.storage.wake_attempt_path(signal.wake_signal_id).exists()
        assert not store.storage.wake_quarantine_path(signal.wake_signal_id).exists()
        assert store.wakes.pending(limit=0) == []
        assert observation.observation_id not in {
            item.observation_id for item in store.observations.unhandled_requiring_main()}
        rows, errors = store.wakes.attempts.quarantined()
        assert rows == [] and [error["wake_signal_id"] for error in errors] == [signal.wake_signal_id]
        assert errors[0]["error_code"] == WAKE_REPLAY_SOURCE_UNREADABLE and errors[0]["path"] == str(moved)
        replay = store.wakes.attempts.replay(signal.wake_signal_id, now=300.0)
        assert (replay.ok, replay.error_code) == (False, WAKE_REPLAY_SOURCE_UNREADABLE)
        assert moved.read_bytes() == garbage

    def test_an_unreadable_quarantine_record_is_refused_on_replay_not_raised(self, tmp_path):
        store = _store(tmp_path)
        signal = _raise(store)
        store.wakes.attempts.quarantine(signal.wake_signal_id, _poison(store, signal).decision, now=200.0)
        record = store.storage.wake_quarantine_path(signal.wake_signal_id)
        record.write_text("{not json", encoding="utf-8")
        replay = store.wakes.attempts.replay(signal.wake_signal_id, now=300.0)
        assert (replay.ok, replay.error_code) == (False, WAKE_REPLAY_SOURCE_UNREADABLE)
        assert record.read_text(encoding="utf-8") == "{not json"

    @pytest.mark.parametrize("damage", ["bad_envelope", "id_mismatch"])
    def test_a_record_that_does_not_restore_to_the_same_wake_is_refused_on_replay(self, tmp_path, damage):
        # 结案记录读得出，但还原不成信封，或还原出的是别的 ID：重放和预览都拒绝，不抛异常、不改文件（be 第 4 步建议 1）。
        store = _store(tmp_path)
        signal = _raise(store)
        store.wakes.attempts.quarantine(signal.wake_signal_id, _poison(store, signal).decision, now=200.0)
        record = store.storage.wake_quarantine_path(signal.wake_signal_id)
        payload = json.loads(record.read_text(encoding="utf-8"))
        if damage == "bad_envelope":
            payload["created_at"] = "not-a-number"
        else:
            payload["wake_signal_id"] = "wake-someone-else"
        record.write_text(json.dumps(payload), encoding="utf-8")
        before = record.read_bytes()
        assert store.wakes.attempts.replay_source(signal.wake_signal_id) == (WAKE_REPLAY_SOURCE_UNREADABLE, {})
        replay = store.wakes.attempts.replay(signal.wake_signal_id, now=300.0)
        assert (replay.ok, replay.error_code) == (False, WAKE_REPLAY_SOURCE_UNREADABLE)
        assert record.read_bytes() == before and store.wakes.pending(limit=0) == []

    def test_a_corrupt_ledger_is_preserved_next_to_the_record_not_reset(self, tmp_path):
        store = _store(tmp_path)
        signal = _raise(store)
        attempts = store.storage.wake_attempt_path(signal.wake_signal_id)
        attempts.parent.mkdir(parents=True, exist_ok=True)
        corrupt = b'{"schema": "wake-attempts.v1", "state": {"same_cause_count": "four"}}'
        attempts.write_bytes(corrupt)
        result = store.wakes.attempts.quarantine(signal.wake_signal_id, ledger_corrupt_decision(), now=200.0)
        preserved = store.storage.wake_quarantine_ledger_path(signal.wake_signal_id)
        assert result.settled is not None and result.ledger_preserved_at == str(preserved)
        assert preserved.read_bytes() == corrupt and not attempts.exists()
        record = _json(store.storage.wake_quarantine_path(signal.wake_signal_id))
        assert record["quarantine"]["decision"]["reason_code"] == WAKE_REASON_LEDGER_CORRUPT
        assert record["quarantine"]["attempts"] == {} and record["quarantine"]["ledger_preserved_at"] == str(preserved)
        assert record["quarantine"]["decision"]["same_cause_count"] == 0
        rows, errors = store.wakes.attempts.quarantined()
        assert errors == [] and rows[0]["reason_code"] == WAKE_REASON_LEDGER_CORRUPT

    def test_an_unreadable_ledger_file_is_preserved_too(self, tmp_path):
        store = _store(tmp_path)
        signal = _raise(store)
        attempts = store.storage.wake_attempt_path(signal.wake_signal_id)
        attempts.parent.mkdir(parents=True, exist_ok=True)
        attempts.write_bytes(b"\x00\x01 garbage")
        result = store.wakes.attempts.quarantine(signal.wake_signal_id, ledger_corrupt_decision(), now=200.0)
        assert result.ledger_preserved_at.endswith(f"/ledger/{signal.wake_signal_id}.json")
        assert store.storage.wake_quarantine_ledger_path(signal.wake_signal_id).read_bytes() == b"\x00\x01 garbage"

    def test_a_healthy_ledger_is_recorded_inline_and_not_preserved(self, tmp_path):
        store = _store(tmp_path)
        signal = _raise(store)
        result = store.wakes.attempts.quarantine(signal.wake_signal_id, _poison(store, signal).decision, now=200.0)
        assert result.ledger_preserved_at == ""
        assert not store.storage.wake_quarantine_ledger_path(signal.wake_signal_id).exists()
        record = _json(store.storage.wake_quarantine_path(signal.wake_signal_id))
        assert record["quarantine"]["attempts"]["same_cause_count"] == WAKE_POISON_SAME_CAUSE_LIMIT


    def test_crash_between_record_and_ledger_move_keeps_the_record_and_never_resets_the_count(self, tmp_path, monkeypatch):
        """结案记录先落盘、坏账后移：两步之间崩溃时记录已在、坏账原地未动；重跑同一结案收口，不会从零重新计数。"""
        from agent_py_agent.agent.conversation import store_wake_attempts

        store = _store(tmp_path)
        signal = _raise(store)
        attempts = store.storage.wake_attempt_path(signal.wake_signal_id)
        attempts.parent.mkdir(parents=True, exist_ok=True)
        corrupt = b'{"schema": "wake-attempts.v1", "state": {"same_cause_count": "four"}}'
        attempts.write_bytes(corrupt)
        pending = store.storage.wake_signal_path(signal)
        real_move = store_wake_attempts._move_aside

        def crash_before_move(source, target):
            raise OSError("process died between writing the record and moving the ledger")

        monkeypatch.setattr(store_wake_attempts, "_move_aside", crash_before_move)
        with pytest.raises(OSError):
            store.wakes.attempts.quarantine(signal.wake_signal_id, ledger_corrupt_decision(), now=200.0)
        record_path = store.storage.wake_quarantine_path(signal.wake_signal_id)
        preserved = store.storage.wake_quarantine_ledger_path(signal.wake_signal_id)
        assert record_path.exists() and pending.exists() and attempts.read_bytes() == corrupt
        assert _json(record_path)["quarantine"]["ledger_preserved_at"] == str(preserved) and not preserved.exists()
        monkeypatch.setattr(store_wake_attempts, "_move_aside", real_move)
        result = store.wakes.attempts.quarantine(signal.wake_signal_id, ledger_corrupt_decision(), now=201.0)
        assert result.settled is not None and result.ledger_preserved_at == str(preserved)
        assert preserved.read_bytes() == corrupt and not attempts.exists() and not pending.exists()
        assert _json(record_path)["quarantine"]["decision"]["reason_code"] == WAKE_REASON_LEDGER_CORRUPT
        assert store.wakes.attempts.state_report(signal.wake_signal_id) == (WakePoisonState(), None)
