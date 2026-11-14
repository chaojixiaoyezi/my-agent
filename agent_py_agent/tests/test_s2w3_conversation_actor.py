"""可重建的邮箱只排序命令，绝不拥有执行权或退出证明。"""
from dataclasses import replace

from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule

from agent_py_agent.agent.conversation import conversation_actor as actor


def msg(kind, request="r1", attempt="a1", epoch=1, **kw):
    return getattr(actor, kind)("owner", "conversation", request, attempt, epoch, **kw)


def test_stop_between_claim_and_admit_is_a_tombstone():
    seq = actor.ConversationSequencer()
    stop = seq.decide(msg("Stop"))
    assert stop.action == "run_leftover_cleanup"
    assert seq.decide(msg("Admit")).action == "rejected"


def test_newer_control_invalidates_decision_at_t_commit():
    seq = actor.ConversationSequencer()
    admit = seq.decide(msg("Admit"))
    assert seq.check_still_current("conversation", admit.expected_seq)
    seq.decide(msg("Steer"))
    assert not seq.check_still_current("conversation", admit.expected_seq)


def test_stop_new_late_finish_keeps_new_turn():
    seq = actor.ConversationSequencer()
    seq.decide(msg("Admit"))
    seq.decide(msg("Stop"))
    assert seq.decide(msg("Admit", "r2", "a2")).action == "defer"
    assert seq.decide(msg("Recover", "r2", "a2")).action == "require_exit_proof"
    assert seq.decide(msg("Recover", "r2", "a2", exit_proof=True,
                          previous_attempt=("r1", "a1", 1))).action == "admit"
    seq.decide(msg("TurnFinished"))
    assert seq.snapshot("conversation")["active"] == ("r2", "a2", 1)


def test_queued_controls_coalesce_and_precede_admit():
    seq = actor.ConversationSequencer()
    seq.enqueue(msg("Admit"))
    first = seq.enqueue(msg("Stop"))
    latest = seq.enqueue(msg("Stop"))
    assert latest.seq > first.seq
    assert seq.snapshot("conversation")["controls"] == 1
    assert isinstance(seq.pop("conversation"), actor.Stop)
    assert isinstance(seq.pop("conversation"), actor.Admit)


def test_mailbox_loss_rebuilds_from_durable_live_records():
    seq = actor.ConversationSequencer()
    seq.rebuild([msg("Recover", exit_proof=True), msg("Stop", "stopped")])
    assert seq.snapshot("conversation")["active"] == ("r1", "a1", 1)
    assert seq.decide(msg("Admit", "stopped")).action == "rejected"


def test_gateway_stop_stale_decision_does_not_write(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from types import SimpleNamespace

    from agent_py_agent.agent.gateway_parts import control_service as control
    from agent_py_agent.agent.gateway_parts import request_worker as worker
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root

    paths = gateway_paths_from_root(tmp_path / "gateway")
    agent = SimpleNamespace(config=SimpleNamespace(conversation_actor_workers=1))
    scheduler = worker.gateway_conversation_scheduler(paths, agent)
    scope = control.GatewayControlScope("owner", "gateway", "conversation")
    key = "\x1f".join((scope.user_id, scope.channel, scope.conversation_id))
    scheduler.sequencer.decide(actor.Admit("owner", key, "r1", "a1", 1))
    record = control._GatewayRequestRecord(paths.processing / "r1.json", {"id": "r1"})
    writes = []
    monkeypatch.setattr(control, "_mark_request_stopping_locked", lambda *_: writes.append(True) or (True, "", {}))

    @contextmanager
    def race(*args):
        scheduler.sequencer.decide(actor.Stop("owner", key, "r1"))
        yield

    monkeypatch.setattr(control, "gateway_turn_transition", race)
    try:
        accepted, _ = control._mark_request_stopping(record, scope)
        assert accepted is False
        assert writes == []
    finally:
        scheduler.close(wait=True)


def test_scheduler_handler_failure_does_not_strand_conversation():
    import pytest

    scheduler = actor.ActorScheduler(workers=1)

    def broken(command, decision):
        raise ValueError("handler failed")

    try:
        future = scheduler.post(msg("Stop"), handler=broken)
        with pytest.raises(ValueError):
            future.result(timeout=5)
        next_future = scheduler.post(msg("TurnFinished"))
        assert next_future is not None, "failed drainer must release its scheduled ownership"
        next_future.result(timeout=5)
    finally:
        scheduler.close(wait=True)


def test_scheduler_retry_preserves_handler(monkeypatch):
    import threading

    scheduler = actor.ActorScheduler(workers=1)
    original = scheduler.sequencer.take_decision
    pending = [True]
    delivered = []
    done = threading.Event()

    def contend(key):
        if pending:
            pending.pop()
            return None, actor.ActorDecision(0, "retry", -1)
        return original(key)

    def handler(*_):
        delivered.append(True)
        done.set()

    monkeypatch.setattr(scheduler.sequencer, "take_decision", contend)
    try:
        future = scheduler.post(msg("Stop"), handler=handler)
        future.result(timeout=5)
        assert done.is_set()
        assert delivered == [True]
    finally:
        scheduler.close(wait=True)


def test_recover_proof_cannot_replace_unrelated_active_attempt():
    seq = actor.ConversationSequencer()
    seq.decide(msg("Admit", "new", "new-attempt"))
    stale = msg("Recover", "recovery", "recover-attempt", exit_proof=True,
                previous_attempt=("old", "old-attempt", 1))
    assert seq.decide(stale).action == "require_exit_proof"
    assert seq.snapshot("conversation")["active"] == ("new", "new-attempt", 1)


def test_popped_control_remains_a_barrier_before_application():
    seq = actor.ConversationSequencer()
    seq.decide(msg("Admit", attempt="", epoch=0))
    seq.enqueue(msg("Stop", request=""))
    stopped = seq.pop("conversation")
    assert seq.decide(msg("Admit")).action != "admit"
    seq.decide(stopped)
    assert seq.decide(msg("Admit")).action == "rejected"


def test_old_control_does_not_borrow_new_control_version():
    seq = actor.ConversationSequencer()
    seq.decide(msg("Admit"))
    old = seq.enqueue(msg("Steer"))
    seq.pop("conversation")
    current = seq.decide(msg("Steer"))
    result = seq.decide(old)
    assert result.action == "stale"
    assert not seq.check_still_current("conversation", result.expected_seq)
    assert seq.check_still_current("conversation", current.expected_seq)


def test_actual_admit_then_stale_t_releases_exact_unstarted_reservation(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from types import SimpleNamespace

    from agent_py_agent.agent.gateway_parts import request_worker as worker
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
    from agent_py_agent.agent.gateway_parts.queue_service import (
        claim_request,
        ensure_gateway_folders,
    )
    from agent_py_agent.agent.gateway_parts.request_client import (
        GatewayAskParams,
        submit_gateway_ask,
    )

    paths = gateway_paths_from_root(tmp_path / "gateway")
    ensure_gateway_folders(paths)
    rid, pending, _ = submit_gateway_ask(paths, params=GatewayAskParams("work", chat_session_id="conversation"))
    claim = claim_request(paths, pending)
    agent = SimpleNamespace(config=SimpleNamespace(conversation_actor_workers=1))
    scheduler = worker.gateway_conversation_scheduler(paths, agent)
    payload = worker.read_json_file_report(claim.path).payload
    key = worker.request_conversation_key(payload)
    executed = []
    monkeypatch.setattr(worker, "_reject_worker_route", lambda *_: False)
    monkeypatch.setattr(worker, "_resolve_request_agent", lambda *_: agent)
    monkeypatch.setattr(worker, "_execute_claimed_gateway_request", lambda *_: executed.append(True))
    original = worker.gateway_turn_transition

    @contextmanager
    def race(*args):
        with original(*args):
            scheduler.sequencer.decide(actor.Stop("owner", key, rid))
            yield

    monkeypatch.setattr(worker, "gateway_turn_transition", race)
    try:
        assert worker._process_claimed_gateway_request_path(agent, paths, claim.path, "worker", claim=claim) is False
        scheduler.post(actor.TurnFinished("owner", key, "marker")).result(timeout=5)
        assert executed == []
        assert scheduler.sequencer.snapshot(key)["active"] is None
        assert claim.path.exists()
    finally:
        scheduler.close(wait=True)


def test_scheduler_does_not_mix_attempt_handlers():
    import threading

    scheduler = actor.ActorScheduler(workers=1)
    ready, release = threading.Event(), threading.Event()
    delivered = []

    def hold(*_):
        ready.set()
        assert release.wait(5)

    try:
        first = scheduler.post(msg("TurnFinished", "hold"), handler=hold)
        assert ready.wait(5)
        old = scheduler.post(msg("TurnFinished", attempt="old"), handler=lambda *_: delivered.append("old"))
        new = scheduler.post(msg("TurnFinished", attempt="new"), handler=lambda *_: delivered.append("new"))
        release.set()
        first.result(timeout=5)
        old.result(timeout=5)
        new.result(timeout=5)
        assert delivered == ["old", "new"]
    finally:
        release.set()
        scheduler.close(wait=True)


def test_strict_handler_budget_is_counted_even_when_future_is_observed(monkeypatch):
    from types import SimpleNamespace

    import pytest

    from agent_py_agent.agent.concurrency import lock_order as dep

    dep.reset_for_tests()
    dep.configure(strict=True)
    ticks = iter([0.0, 1.0])
    monkeypatch.setattr(actor, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    scheduler = actor.ActorScheduler(workers=1)
    try:
        with pytest.raises(dep.LockOrderViolation):
            scheduler.post(msg("Stop")).result(timeout=5)
        assert scheduler.slow_handlers == 1
        assert dep.lockdep_snapshot().get("handler_overruns", 0) == 1
    finally:
        scheduler.close(wait=True)
        dep.reset_for_tests()


def test_submit_failure_rolls_back_pending_result_and_reservation(monkeypatch):
    import pytest

    scheduler = actor.ActorScheduler(workers=1)

    def fail(*_):
        raise RuntimeError("executor submit failed")

    monkeypatch.setattr(scheduler._executor, "submit", fail)
    try:
        with pytest.raises(RuntimeError, match="executor submit failed"):
            scheduler.post(msg("Stop"))
        assert scheduler._scheduled == set()
        assert scheduler._pending_results == set()
        assert not scheduler.sequencer.has_pending("conversation")
    finally:
        scheduler.close(wait=False)


def test_replayed_control_cannot_roll_back_finished_version():
    seq = actor.ConversationSequencer()
    seq.decide(msg("Admit"))
    decision = seq.decide(msg("Steer"))
    seq.decide(msg("TurnFinished"))
    replayed = msg("Steer", seq=decision.seq)
    assert seq.decide(replayed).action == "stale"
    assert not seq.check_still_current("conversation", decision.expected_seq)


def test_queued_finish_after_control_publishes_fresh_version():
    seq = actor.ConversationSequencer()
    seq.decide(msg("Admit"))
    seq.enqueue(msg("TurnFinished"))
    stopped = seq.decide(msg("Stop"))
    finished = seq.decide(seq.pop("conversation"))
    assert finished.action == "finished"
    assert finished.expected_seq > stopped.expected_seq
    assert not seq.check_still_current("conversation", stopped.expected_seq)


def test_tail_submit_failure_keeps_accepted_message_and_future(monkeypatch):
    import threading

    scheduler = actor.ActorScheduler(workers=1)
    ready, release = threading.Event(), threading.Event()
    delivered = []

    def hold(*_):
        ready.set()
        assert release.wait(5)

    def fail(*_):
        raise RuntimeError("tail submit failed")

    try:
        first = scheduler.post(msg("TurnFinished", "hold"), handler=hold)
        assert ready.wait(5)
        monkeypatch.setattr(scheduler._executor, "submit", fail)
        pending = scheduler.post(msg("TurnFinished", "accepted"), handler=lambda *_: delivered.append("accepted"))
        release.set()
        first.result(timeout=5)
        assert pending.result(timeout=5).action == "ignored"
        assert delivered == ["accepted"]
        assert scheduler._pending_results == set()
    finally:
        release.set()
        scheduler.close(wait=False)


def test_submit_exception_cannot_leave_a_ghost_runner(monkeypatch):
    import threading

    import pytest

    scheduler = actor.ActorScheduler(workers=2)
    raw_submit = scheduler._executor.submit
    ghost_release, active_release = threading.Event(), threading.Event()
    active_ready, queued_started = threading.Event(), threading.Event()
    ghosts = []

    def submit_then_raise(function, *args):
        def delayed():
            assert ghost_release.wait(5)
            return function(*args)

        ghosts.append(raw_submit(delayed))
        raise RuntimeError("submit accepted then failed")

    def hold(*_):
        active_ready.set()
        assert active_release.wait(5)

    try:
        monkeypatch.setattr(scheduler._executor, "submit", submit_then_raise)
        with pytest.raises(RuntimeError, match="accepted then failed"):
            scheduler.post(msg("TurnFinished", "failed"))
        monkeypatch.setattr(scheduler._executor, "submit", raw_submit)
        active = scheduler.post(msg("TurnFinished", "active"), handler=hold)
        assert active_ready.wait(5)
        queued = scheduler.post(msg("TurnFinished", "queued"), handler=lambda *_: queued_started.set())
        ghost_release.set()
        ghosts[0].result(timeout=5)
        assert not queued_started.is_set()
        active_release.set()
        active.result(timeout=5)
        queued.result(timeout=5)
        assert queued_started.is_set()
    finally:
        ghost_release.set()
        active_release.set()
        scheduler.close(wait=False)


class SequencerMachine(RuleBasedStateMachine):
    def __init__(self):
        super().__init__()
        self.seq = actor.ConversationSequencer()
        self.finished = set()

    @rule(kind=st.sampled_from(["Admit", "Steer", "Stop", "TurnFinished", "Recover"]),
          request=st.sampled_from(["r1", "r2", "r3"]))
    def command(self, kind, request):
        command = msg(kind, request, request)
        if kind == "Recover":
            command = replace(command, exit_proof=False)
        before = self.seq.snapshot("conversation")["active"]
        result = self.seq.decide(command)
        if kind == "Recover" and before is not None:
            assert result.action == "require_exit_proof"
        if kind == "Stop":
            self.finished.add(request)
        if kind == "Admit" and request in self.finished:
            assert result.action == "rejected"

    @invariant()
    def one_active_turn_and_projection_state(self):
        state = self.seq.snapshot("conversation")
        assert state["state"] in {"idle", "running", "stopping"}
        assert state["active"] is None or len(state["active"]) == 3


TestSequencerStateful = SequencerMachine.TestCase
