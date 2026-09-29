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
)
from agent_py_agent.agent.conversation.store_wake_publication import _stable_signal
from agent_py_agent.agent.conversation.wake_poison import (
    WAKE_ATTEMPT_NEUTRAL,
    WAKE_ATTEMPT_SUCCESS,
    WAKE_POISON_SAME_CAUSE_LIMIT,
    WAKE_STATUS_FAILED_PERMANENTLY,
    WAKE_VERDICT_FAILURE,
    WakeAttemptVerdict,
    WakePoisonState,
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
        store.wakes.attempts.begin(signal, claim_id=f"claim-{attempt}", now=100.0 + attempt)
        outcome = store.wakes.attempts.record(signal, verdict, now=100.5 + attempt, error=RuntimeError("boom"))
    return outcome


def _json(path):
    return json.loads(path.read_text(encoding="utf-8"))


class TestLedger:
    def test_failures_accumulate_and_decide_at_the_limit(self, tmp_path):
        store = _store(tmp_path)
        signal = _raise(store)
        for attempt in range(WAKE_POISON_SAME_CAUSE_LIMIT - 1):
            store.wakes.attempts.begin(signal, claim_id="c", now=float(attempt))
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
        store.wakes.attempts.begin(signal, claim_id="c1", now=1.0)
        store.wakes.attempts.record(signal, BUG, now=2.0)
        store.wakes.attempts.begin(signal, claim_id="c2", now=3.0)
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
        store.wakes.attempts.begin(signal, claim_id="live", now=1.0)
        live = store.wakes.attempts.begin(signal, claim_id="again", now=2.0)
        assert live.abandoned is False and live.state == WakePoisonState()
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.3)"])
        dead = build_process_identity(child.pid)
        child.wait(timeout=10)
        ledger = _json(path)
        ledger["in_flight"]["owner_process"] = dead
        path.write_text(json.dumps(ledger), encoding="utf-8")
        outcome = store.wakes.attempts.begin(signal, claim_id="after-crash", now=3.0)
        assert outcome.abandoned is True
        assert (outcome.state.reason_code, outcome.state.same_cause_count) == ("attempt:abandoned", 1)
        assert _json(path)["in_flight"]["claim_id"] == "after-crash"

    def test_unknowable_owner_identity_is_not_treated_as_dead(self, tmp_path):
        store = _store(tmp_path)
        signal = _raise(store)
        path = store.storage.wake_attempt_path(signal.wake_signal_id)
        store.wakes.attempts.begin(signal, claim_id="elsewhere", now=1.0)
        ledger = _json(path)
        ledger["in_flight"]["owner_process"] = {**ledger["in_flight"]["owner_process"], "host_id": "another-host"}
        path.write_text(json.dumps(ledger), encoding="utf-8")
        outcome = store.wakes.attempts.begin(signal, claim_id="here", now=2.0)
        assert outcome.abandoned is False and outcome.state == WakePoisonState()

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
        settled = store.wakes.attempts.quarantine(signal.wake_signal_id, outcome.decision, now=200.0)
        assert settled is not None and settled.status == WAKE_STATUS_FAILED_PERMANENTLY
        assert store.wakes.pending_one(signal.wake_signal_id) is None
        assert signal.wake_signal_id not in {item.wake_signal_id for item in store.wakes.pending(limit=0)}
        assert not store.storage.wake_attempt_path(signal.wake_signal_id).exists()
        record = _json(store.storage.wake_quarantine_path(signal.wake_signal_id))
        assert _stable_signal(WakeSignal.from_dict(record)) == _stable_signal(signal)
        assert record["quarantine"]["decision"]["reason_code"] == BUG.reason_code
        assert record["quarantine"]["last_error"]["type"] == "RuntimeError"
        assert store.wakes.attempts.quarantine(signal.wake_signal_id, outcome.decision, now=201.0) is None

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
    store.wakes.attempts.begin(mine, claim_id="c", now=400.0)
    store.wakes.attempts.record(mine, BUG, now=401.0)
    store.wakes.attempts.begin(other, claim_id="c", now=400.0)
    store.wakes.attempts.record(other, BUG, now=401.0)
    paths, errors = _conversation_related_paths(store.storage.root, store.threads.load("thread-a"))
    names = {path.relative_to(store.storage.root.resolve()).as_posix() for path in paths}
    assert errors == []
    assert f"wake_queue/attempts/{mine.wake_signal_id}.json" in names
    assert f"wake_queue/quarantine/replayed/{mine.wake_signal_id}/1.json" in names
    assert f"wake_queue/attempts/{other.wake_signal_id}.json" not in names
