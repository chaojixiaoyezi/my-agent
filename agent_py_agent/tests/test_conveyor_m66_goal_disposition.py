"""目标续跑结论和提示沿现有持久状态恢复。"""
from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.conversation import (
    BackgroundMainAgentRuntime,
    BackgroundMainAgentScheduler,
    FakeDeliveryService,
    background_goal,
    goal_progress_fuse,
    goal_runtime,
    technical_continuation,
)
from agent_py_agent.agent.conversation.background_goal import (
    GoalContinuationDependencies,
    continue_goal_after_report,
)
from agent_py_agent.agent.conversation.background_routing import BackgroundRouteDependencies
from agent_py_agent.agent.conversation.goal_progress_fuse import (
    FUSE_METADATA_KEY,
    WAKE_SNAPSHOT_KEY,
    goal_progress_snapshot,
    push_goal_withheld_notice,
)
from agent_py_agent.agent.conversation.host_notices import pending_host_notices
from agent_py_agent.agent.conversation.models import BackgroundMainAgentReport, WakeSignal
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.conversation.store_layout import ConversationStorage
from agent_py_agent.agent.conversation.store_wakes import WakeStore
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts.io import update_json_file_atomic
from agent_py_agent.agent.settings import AgentConfig


def _goal_world(tmp_path):
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.create(canonical_user_id="person-a", owner_id="owner-a")
    goal = store.goals.create({"thread_id": thread.thread_id, "objective": "推进目标"})
    return store, goal


def _signal(goal, wake_id="wake-original"):
    return WakeSignal(wake_signal_id=wake_id, thread_id=goal.thread_id, root_task_id=goal.task_id,
                      reason="thread_goal_continue", metadata={
                          "goal_id": goal.goal_id, WAKE_SNAPSHOT_KEY: goal_progress_snapshot(goal, "active"),
                      })


def _report(goal, **fields):
    report = BackgroundMainAgentReport(
        thread_id=goal.thread_id, task_id=goal.task_id, reason="thread_goal_continue", response="",
        route_channel="", route_target="", created_at=1., turn_end_reason="blocked", runtime_status="blocked",
    )
    return replace(report, **fields)


def _push(store, goal, signal, report, sent):
    channels = SimpleNamespace(supports_proactive=lambda _channel: True,
                               deliver=lambda context, reply: sent.append((context, reply))
                               or SimpleNamespace(delivery_status="sent"))
    route = BackgroundRouteDependencies(store.threads.load, lambda: (), lambda: ("feishu", "target-a"))
    return push_goal_withheld_notice(store, goal, signal, report, channels=channels, route_deps=route)


def test_same_wake_and_revision_push_one_hint_after_restart(tmp_path):
    store, goal = _goal_world(tmp_path)
    signal, report, sent = _signal(goal), _report(goal), []
    assert _push(store, goal, signal, report, sent)
    assert not pending_host_notices(store, goal.thread_id)

    reopened = ConversationStore(tmp_path / "conversations")
    assert _push(reopened, goal, signal, report, sent)
    assert len(sent) == 1


def test_new_revision_uses_distinct_hint_identity_for_the_same_wake(tmp_path):
    store, goal = _goal_world(tmp_path)
    signal, report, sent = _signal(goal), _report(goal), []
    assert _push(store, goal, signal, report, sent)
    edited = store.goals.update({"thread_id": goal.thread_id, "goal_id": goal.goal_id, "objective": "调整目标"})
    assert edited.revision > goal.revision
    assert _push(store, edited, signal, report, sent)
    first, second = [context.idempotency_key for context, _reply in sent]
    assert first != second


def _dependencies(store, raised, notices):
    return GoalContinuationDependencies(
        goals=store.goals, tasks=store.tasks, goal_clock=store.goal_clock,
        task_status=lambda _thread, _task: "active", subagent_phase=lambda _task: ("idle", ""),
        raise_wake=lambda goal, **_metadata: raised.append(goal.goal_id), task_registry=lambda: None,
        queue_withheld_notice=lambda goal, signal, report: notices.append((goal, signal, report)),
    )


def test_reopened_goal_replays_frozen_withheld_disposition(tmp_path):
    store, goal = _goal_world(tmp_path)
    signal = _signal(goal)
    raised, notices = [], []
    original = _report(goal, runtime_reason="PROVIDER_TIMEOUT", tool_success_count=2)
    continue_goal_after_report(_dependencies(store, raised, notices), signal, report=original, now=2)

    # 重投仍持原唤醒编号。改变输入报告以验证重投从原账取结论和提示事实。
    reopened = ConversationStore(tmp_path / "conversations")
    retry = _report(goal, turn_end_reason="completed", runtime_status="completed", goal_continuation_allowed=True)
    continue_goal_after_report(_dependencies(reopened, raised, notices), signal, report=retry, now=3)

    assert not raised and len(notices) == 2
    assert [row[2].turn_end_reason for row in notices] == ["blocked", "blocked"]
    assert [row[2].runtime_reason for row in notices] == ["PROVIDER_TIMEOUT", "PROVIDER_TIMEOUT"]
    assert [row[2].tool_success_count for row in notices] == [2, 2]
    assert not any(row[2].goal_continuation_allowed for row in notices)
    current = reopened.goals.load(goal.thread_id, goal_id=goal.goal_id)
    disposition = current.metadata[FUSE_METADATA_KEY]["last_disposition"]
    assert disposition["wake_signal_id"] == signal.wake_signal_id
    assert disposition["action"] == "withheld"
    assert disposition["progress_evidence"]["tool_success_count"] == 2


def test_accounting_keeps_one_small_disposition_and_the_existing_wake_window(tmp_path):
    store, goal = _goal_world(tmp_path)
    raised, notices = [], []
    report = _report(goal, goal_continuation_allowed=True, tool_success_count=1, response="正文" * 5000,
                     turn_end_reason="completed", runtime_reason="RESULT_" * 500)
    dependencies = _dependencies(store, raised, notices)
    for index in range(20):
        continue_goal_after_report(dependencies, _signal(goal, f"wake-{index}"), report=report, now=2 + index)

    current = store.goals.load(goal.thread_id, goal_id=goal.goal_id)
    state = current.metadata[FUSE_METADATA_KEY]
    assert state["processed_wake_ids"] == [f"wake-{index}" for index in range(4, 20)]
    assert state["idle_slices"] == 0 and len(raised) == 20 and not notices
    latest = state["last_disposition"]
    assert latest["wake_signal_id"] == "wake-19"
    assert latest["progress_evidence"]["basis"] == "tool_success"
    assert "正文" not in repr(latest) and len(repr(latest)) < 1000


def test_redelivery_retries_original_wake_publication_without_counting_again(tmp_path):
    store, goal = _goal_world(tmp_path)
    signal = _signal(goal)
    raised, notices = [], []

    def publication_error(_goal, **_metadata):
        raise OSError("simulated wake publication error")

    before = replace(_dependencies(store, raised, notices), raise_wake=publication_error)
    continue_goal_after_report(before, signal, report=_report(goal, goal_continuation_allowed=True), now=2)

    reopened = ConversationStore(tmp_path / "conversations")
    continue_goal_after_report(_dependencies(reopened, raised, notices), signal, report=_report(goal), now=3)
    current = reopened.goals.load(goal.thread_id, goal_id=goal.goal_id)
    state = current.metadata[FUSE_METADATA_KEY]
    assert raised == [goal.goal_id] and not notices
    assert state["idle_slices"] == 1 and state["processed_wake_ids"] == [signal.wake_signal_id]
    assert state["last_disposition"]["action"] == "continue"


def test_late_redelivery_does_not_publish_for_an_edited_revision(tmp_path):
    store, goal = _goal_world(tmp_path)
    signal = _signal(goal)
    raised, notices = [], []
    dependencies = _dependencies(store, raised, notices)
    continue_goal_after_report(dependencies, signal, report=_report(goal), now=2)
    edited = store.goals.update({"thread_id": goal.thread_id, "goal_id": goal.goal_id, "objective": "调整目标"})

    continue_goal_after_report(dependencies, signal, report=_report(goal), now=3)
    assert len(notices) == 1 and not raised
    continue_goal_after_report(dependencies, _signal(edited, "wake-edited"), report=_report(edited), now=4)
    assert [notice[0].revision for notice in notices] == [goal.revision, edited.revision]


class _Crash(BaseException):
    """Stops the scheduler like a process exit: ordinary error handling does not catch it."""


class _CountingBackend:
    name = "counting"

    def __init__(self, calls, reply):
        self.calls, self.reply = calls, reply

    def probe_tool_capability(self):
        return None

    def generate(self, prompt, on_chunk=None, **kwargs):
        self.calls.append(prompt)
        return ModelResponse(text=self.reply, backend=self.name)


def _open_scheduler(tmp_path, calls, reply="slice finished", **config):
    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl", **config), tmp_path)
    agent.backend = _CountingBackend(calls, reply)
    store = agent.conversation_store
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=FakeDeliveryService())
    return agent, store, BackgroundMainAgentScheduler({"runtime": runtime, "store": store})


def _scheduled_goal(tmp_path, calls, reply="slice finished", **config):
    agent, store, scheduler = _open_scheduler(tmp_path, calls, reply, **config)
    owner_id = agent.home_paths.owner_id
    thread = store.threads.create(canonical_user_id=owner_id, owner_id=owner_id)
    goal = store.goals.create({"thread_id": thread.thread_id, "objective": "advance the same work", "now": 11.0})
    store.tasks.bind({"thread_id": thread.thread_id, "task_id": goal.task_id, "goal": goal.objective,
                      "status": "active", "now": 12.0})
    first = goal_runtime.raise_goal_continuation_wake(store, goal, now=13.0)
    return agent, store, scheduler, goal, first


def _fuse_state(store, goal):
    return store.goals.load(goal.thread_id, goal_id=goal.goal_id).metadata[FUSE_METADATA_KEY]


def _pending_ids(store, goal):
    return [signal.wake_signal_id for signal in store.wakes.pending(limit=0)
            if signal.dedupe_key == f"thread-goal:{goal.goal_id}"]


def _interrupt_publication(monkeypatch, scheduler, now):
    def interrupted(*_args, **_kwargs):
        raise _Crash

    with monkeypatch.context() as patch:
        patch.setattr(goal_runtime, "raise_goal_continuation_wake", interrupted)
        with pytest.raises(_Crash):
            scheduler.tick(now=now)


@pytest.mark.parametrize(("reply", "recovery"), [
    ("slice finished", "cached_owner_delivery_retry"), ("", "goal_disposition_recovery"),
], ids=["frozen-reply", "no-reply"])
def test_restart_between_accounting_and_publication_recovers_one_successor(tmp_path, monkeypatch, reply, recovery):
    calls = []
    _agent, store, scheduler, goal, first = _scheduled_goal(tmp_path, calls, reply)
    _interrupt_publication(monkeypatch, scheduler, 14.0)

    state = _fuse_state(store, goal)
    assert state["processed_wake_ids"] == [first.wake_signal_id] and state["idle_slices"] == 1
    assert state["last_disposition"]["action"] == "continue"
    assert _pending_ids(store, goal) == [first.wake_signal_id] and len(calls) == 1

    _agent, reopened, restarted = _open_scheduler(tmp_path, calls, reply)
    reports = restarted.tick(now=4000.0)

    # A frozen reply is redelivered through its cached delivery; an empty slice is settled from the
    # accounted decision. Either way the model is not called again and the slice is not counted again.
    assert [report.delivery_reason for report in reports] == [recovery]
    pending = _pending_ids(reopened, goal)
    assert len(pending) == 1 and pending[0] != first.wake_signal_id
    assert reopened.wakes.pending_one(first.wake_signal_id) is None
    state = _fuse_state(reopened, goal)
    assert state["processed_wake_ids"] == [first.wake_signal_id] and state["idle_slices"] == 1
    assert len(calls) == 1
    replies = [row.content for row in reopened.messages.recent(goal.thread_id) if row.content]
    assert replies == ([reply] if reply else [])


def test_restart_after_publication_before_acknowledgement_keeps_the_same_successor(tmp_path, monkeypatch):
    calls = []
    _agent, store, scheduler, goal, first = _scheduled_goal(tmp_path, calls, "")
    mark_handled = WakeStore.mark_handled

    def interrupted(self, wake_signal_id, **options):
        if wake_signal_id == first.wake_signal_id:
            raise _Crash
        return mark_handled(self, wake_signal_id, **options)

    with monkeypatch.context() as patch:
        patch.setattr(WakeStore, "mark_handled", interrupted)
        with pytest.raises(_Crash):
            scheduler.tick(now=14.0)
    pending = _pending_ids(store, goal)
    assert first.wake_signal_id in pending and len(pending) == 2
    successor = next(wake_id for wake_id in pending if wake_id != first.wake_signal_id)

    _agent, reopened, restarted = _open_scheduler(tmp_path, calls, "")
    reports = restarted.tick(now=4000.0)

    assert [report.delivery_reason for report in reports] == ["goal_disposition_recovery"]
    assert _pending_ids(reopened, goal) == [successor]
    state = _fuse_state(reopened, goal)
    assert state["processed_wake_ids"] == [first.wake_signal_id] and state["idle_slices"] == 1
    assert len(calls) == 1


def test_goal_edit_after_interruption_replaces_the_accounted_continuation(tmp_path, monkeypatch):
    calls = []
    _agent, store, scheduler, goal, first = _scheduled_goal(tmp_path, calls, "")
    _interrupt_publication(monkeypatch, scheduler, 14.0)
    edited = store.goals.update({"thread_id": goal.thread_id, "goal_id": goal.goal_id, "objective": "revised work"})
    # The Goal edit control publishes the continuation for the saved revision.
    edit_wake = goal_runtime.raise_goal_continuation_wake(store, edited, now=15.0)
    assert edit_wake.wake_signal_id != first.wake_signal_id
    assert sorted(_pending_ids(store, goal)) == sorted([first.wake_signal_id, edit_wake.wake_signal_id])

    _agent, reopened, restarted = _open_scheduler(tmp_path, calls, "")
    reports = restarted.tick(now=4000.0)

    assert [report.delivery_reason for report in reports] == ["goal_disposition_recovery"]
    assert _pending_ids(reopened, goal) == [edit_wake.wake_signal_id]
    snapshot = reopened.wakes.pending_one(edit_wake.wake_signal_id).metadata[WAKE_SNAPSHOT_KEY]
    assert snapshot["goal_revision"] == edited.revision > goal.revision
    assert _fuse_state(reopened, goal)["processed_wake_ids"] == [first.wake_signal_id]
    assert len(calls) == 1


def test_terminal_goal_after_interruption_acknowledges_without_a_successor(tmp_path, monkeypatch):
    calls = []
    _agent, store, scheduler, goal, first = _scheduled_goal(tmp_path, calls, "")
    _interrupt_publication(monkeypatch, scheduler, 14.0)
    assert store.goals.update({"thread_id": goal.thread_id, "goal_id": goal.goal_id, "status": "complete"})

    _agent, reopened, restarted = _open_scheduler(tmp_path, calls, "")
    restarted.tick(now=4000.0)

    assert _pending_ids(reopened, goal) == [] and reopened.wakes.pending_one(first.wake_signal_id) is None
    state = _fuse_state(reopened, goal)
    assert state["processed_wake_ids"] == [first.wake_signal_id] and state["idle_slices"] == 1
    assert len(calls) == 1


def test_cutoff_that_owns_continuation_after_interruption_keeps_its_own_wake(tmp_path, monkeypatch):
    calls = []
    agent, store, scheduler, goal, first = _scheduled_goal(
        tmp_path, calls, "", technical_cutoff_continuation_enabled=True)
    _interrupt_publication(monkeypatch, scheduler, 14.0)
    # A cutoff closeout on the same task takes over continuation: receipt and wake commit together.
    message = store.messages.append({"thread_id": goal.thread_id, "role": "user", "content": "keep going"})
    receipt = technical_continuation.CutoffReceipt(
        agent.home_paths.owner_id, goal.thread_id, goal.task_id, message.message_id, "request-1", "run-1",
        "attempt-1", 1, "TOOL_ROUND_LIMIT_REACHED", "max_tool_rounds", "pending", goal.goal_id)
    technical_continuation._write(store, goal.task_id, lambda _link: receipt,
                                  publish=lambda value: technical_continuation._publish_receipt(store, value))
    cutoff = [signal.wake_signal_id for signal in store.wakes.pending(limit=0) if signal.dedupe_key == receipt.key]
    assert len(cutoff) == 1

    _agent, reopened, restarted = _open_scheduler(
        tmp_path, calls, "", technical_cutoff_continuation_enabled=True)
    restarted.tick(now=4000.0)

    assert _pending_ids(reopened, goal) == [] and reopened.wakes.pending_one(first.wake_signal_id) is None
    assert [signal.wake_signal_id for signal in reopened.wakes.pending(limit=0)] == cutoff
    state = _fuse_state(reopened, goal)
    assert state["processed_wake_ids"] == [first.wake_signal_id] and state["idle_slices"] == 1
    assert len(calls) == 1


def test_restart_queues_one_withheld_notice_for_the_accounted_slice(tmp_path, monkeypatch):
    calls = []
    _agent, store, scheduler, goal, first = _scheduled_goal(tmp_path, calls, "")

    def interrupted(*_args, **_kwargs):
        raise _Crash

    with monkeypatch.context() as patch:
        # Steer this slice's decision; recovery must apply the frozen decision instead of deciding again.
        patch.setattr(background_goal, "_disposition_action",
                      lambda *_args: ("withheld", "report_withholds_continuation"))
        patch.setattr(goal_progress_fuse, "push_goal_withheld_notice", interrupted)
        with pytest.raises(_Crash):
            scheduler.tick(now=14.0)
    assert _fuse_state(store, goal)["last_disposition"]["action"] == "withheld"
    assert not pending_host_notices(store, goal.thread_id)

    for now in (4000.0, 8000.0):
        _agent, reopened, restarted = _open_scheduler(tmp_path, calls, "")
        restarted.tick(now=now)

    assert len(pending_host_notices(reopened, goal.thread_id)) == 1
    assert _pending_ids(reopened, goal) == [] and reopened.wakes.pending_one(first.wake_signal_id) is None
    assert _fuse_state(reopened, goal)["processed_wake_ids"] == [first.wake_signal_id]
    assert len(calls) == 1


def test_failed_publication_keeps_the_accounted_wake_for_a_later_tick(tmp_path, monkeypatch):
    calls = []
    _agent, store, scheduler, goal, first = _scheduled_goal(tmp_path, calls, "")

    def unavailable(*_args, **_kwargs):
        raise OSError("simulated wake store outage")

    with monkeypatch.context() as patch:
        patch.setattr(goal_runtime, "raise_goal_continuation_wake", unavailable)
        assert scheduler.tick(now=14.0) == []
    assert _pending_ids(store, goal) == [first.wake_signal_id]
    assert scheduler.tick(now=20.0) == []

    reports = scheduler.tick(now=4000.0)

    assert [report.delivery_reason for report in reports] == ["goal_disposition_recovery"]
    pending = _pending_ids(store, goal)
    assert len(pending) == 1 and pending[0] != first.wake_signal_id
    state = _fuse_state(store, goal)
    assert state["processed_wake_ids"] == [first.wake_signal_id] and state["idle_slices"] == 1
    assert len(calls) == 1


def test_recovery_reads_only_the_goal_named_by_the_pending_wake(tmp_path, monkeypatch):
    calls = []
    agent, store, scheduler, goal, _first = _scheduled_goal(tmp_path, calls, "")
    owner_id = agent.home_paths.owner_id
    other = store.threads.create(canonical_user_id=owner_id, owner_id=owner_id)
    store.goals.create({"thread_id": other.thread_id, "objective": "unrelated work"})
    _interrupt_publication(monkeypatch, scheduler, 14.0)
    _agent, _reopened, restarted = _open_scheduler(tmp_path, calls, "")
    goal_path = ConversationStorage.goal_path
    read = []

    def recorded(self, thread_id):
        read.append(thread_id)
        return goal_path(self, thread_id)

    monkeypatch.setattr(ConversationStorage, "goal_path", recorded)
    restarted.tick(now=4000.0)

    assert read and set(read) == {goal.thread_id}


def test_recovery_report_keeps_the_recorded_slice_delivery(tmp_path, monkeypatch):
    calls, recorded = [], []
    _agent, store, scheduler, goal, _first = _scheduled_goal(tmp_path, calls, "")

    def spy(report, **_options):
        recorded.append(report.delivery_reason)
        return False

    monkeypatch.setattr(scheduler.scheduler_service.repository, "record_waiting_delivery", spy)
    _interrupt_publication(monkeypatch, scheduler, 14.0)
    assert len(recorded) == 1 and recorded[0] != "goal_disposition_recovery"

    # A waiting scheduled run keeps the response of the slice that ran; the recovery report carries no body.
    _agent, _reopened, restarted = _open_scheduler(tmp_path, calls, "")
    monkeypatch.setattr(restarted.scheduler_service.repository, "record_waiting_delivery", spy)
    assert [report.delivery_reason for report in restarted.tick(now=4000.0)] == ["goal_disposition_recovery"]
    assert len(recorded) == 1


def test_empty_goal_slice_survives_accounting_outage_and_restart(tmp_path, monkeypatch):
    calls = []
    _agent, store, scheduler, goal, first = _scheduled_goal(tmp_path, calls, "")

    def unavailable(_request):
        raise OSError("simulated accounting outage")

    with monkeypatch.context() as patch:
        patch.setattr(store.goals, "record_continuation_fuse", unavailable)
        scheduler.tick(now=14.0)

    assert _pending_ids(store, goal) == [first.wake_signal_id]
    _agent, reopened, restarted = _open_scheduler(tmp_path, calls, "")
    assert len(restarted.tick(now=4000.0)) == 1
    assert _fuse_state(reopened, goal)["processed_wake_ids"] == [first.wake_signal_id]
    pending = _pending_ids(reopened, goal)
    assert len(pending) == 1 and pending[0] != first.wake_signal_id
    assert len(calls) == 1


@pytest.mark.parametrize("notice_failure", ["false", "error"])
def test_unconfirmed_withheld_notice_keeps_goal_wake_pending(tmp_path, monkeypatch, notice_failure):
    calls = []
    _agent, store, scheduler, goal, first = _scheduled_goal(tmp_path, calls, "")

    def unavailable(*_args, **_kwargs):
        if notice_failure == "error":
            raise OSError("simulated notice outage")
        return False

    with monkeypatch.context() as patch:
        patch.setattr(background_goal, "_disposition_action",
                      lambda *_args: ("withheld", "report_withholds_continuation"))
        patch.setattr(goal_progress_fuse, "push_goal_withheld_notice", unavailable)
        scheduler.tick(now=14.0)
    assert _pending_ids(store, goal) == [first.wake_signal_id]
    assert not pending_host_notices(store, goal.thread_id)

    _agent, reopened, restarted = _open_scheduler(tmp_path, calls, "")
    restarted.tick(now=4000.0)
    assert len(pending_host_notices(reopened, goal.thread_id)) == 1
    assert not _pending_ids(reopened, goal)
    assert len(calls) == 1


def test_failed_exact_goal_read_cannot_reexecute_accounted_empty_slice(tmp_path, monkeypatch):
    calls = []
    _agent, store, scheduler, goal, first = _scheduled_goal(tmp_path, calls, "")
    _interrupt_publication(monkeypatch, scheduler, 14.0)

    def remove_settlement_snapshot(data):
        delivery = data["metadata"].get("owner_delivery", {})
        if "goal_slice_settlement" in delivery:
            data["metadata"].pop("owner_delivery")
        return data

    # An already-accounted wake created by an older runtime has only the Goal disposition.
    update_json_file_atomic(store.wakes._find_path(first.wake_signal_id), remove_settlement_snapshot,
                            require_existing=True)
    _agent, reopened, restarted = _open_scheduler(tmp_path, calls, "")
    load = reopened.goals.load

    def unavailable(thread_id, *, goal_id=None, **filters):
        if goal_id == goal.goal_id and not filters:
            raise OSError("simulated disposition read interruption")
        return load(thread_id, goal_id=goal_id, **filters)

    with monkeypatch.context() as patch:
        patch.setattr(reopened.goals, "load", unavailable)
        restarted.tick(now=4000.0)
    assert _pending_ids(reopened, goal) == [first.wake_signal_id]
    assert len(calls) == 1
    assert len(restarted.tick(now=8000.0)) == 1
    assert _fuse_state(reopened, goal)["processed_wake_ids"] == [first.wake_signal_id]
    assert len(calls) == 1
