"""目标续跑结论和提示沿现有持久状态恢复。"""
from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

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
