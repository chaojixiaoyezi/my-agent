"""后台接收本人输入后的回复义务沿实际 ACK 和 Compact 携带。"""
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.runtime import guidance
from agent_py_agent.agent.conversation.active_turn_input import merge_active_turn_user_inputs
from agent_py_agent.agent.conversation.background_execution import (
    collect_background_execution_result,
)
from agent_py_agent.agent.conversation.runtime import (
    BackgroundRunRequest,
    _background_delivery_decision,
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.tests.test_runtime_guidance import _tool_loop_params


@pytest.mark.parametrize("origin,consume,accepted", [("", True, True), ("session_message", True, False), ("", False, False)])
def test_only_consumed_direct_user_input_establishes_reply(tmp_path, origin, consume, accepted):
    agent = SimpleAgent(AgentConfig(model_backend="echo"), tmp_path)
    agent.conversation_store.guidance.append({
        "target_type": "agent_run", "target_id": "main-run", "message": "解释当前进展",
        "now": 10.0, "metadata": {"origin_kind": origin},
    })
    params = _tool_loop_params(run_id="main-run")
    assert guidance.inject_pending_guidance(agent, params, now=11.0)
    assert not any(p.get("user_reply_accepted") for p in params.active_turn_user_inputs)
    if consume:
        assert guidance.mark_injected_turn_input_submitted(agent, params, provider_call_id="reply-test") == 1
        assert guidance.acknowledge_injected_turn_input(agent, params, now=12.0) == 1
    result = collect_background_execution_result(agent, SimpleNamespace(
        response="当前进展", active_turn_user_inputs=params.active_turn_user_inputs), {})
    assert result.user_reply_accepted is accepted


def test_compact_merge_preserves_acceptance_without_allowing_content_change():
    packet = {"schema_version": "active-turn-user-input.v1", "input_ids": ["guidance-1"], "text": "当前进展"}
    accepted = dict(packet, user_reply_accepted=True)
    for first, second in [(packet, accepted), (accepted, packet)]:
        merged = merge_active_turn_user_inputs([first], [second])
        assert merged == [accepted]
        with pytest.raises(ValueError, match="冲突"):
            merge_active_turn_user_inputs(merged, [dict(packet, text="不同内容")])


@pytest.mark.parametrize("accepted", [False, True])
@pytest.mark.parametrize("task_status", ["active", "cancelled"])
def test_lifecycle_reply_obligation_respects_cancellation(tmp_path, accepted, task_status):
    agent = SimpleAgent(AgentConfig(model_backend="echo"), tmp_path)
    store = agent.conversation_store
    thread = store.threads.create()
    store.tasks.bind({"thread_id": thread.thread_id, "task_id": "task-root", "status": task_status})
    store.goals.create({"thread_id": thread.thread_id, "task_id": "task-root", "objective": "继续任务"})
    request = BackgroundRunRequest(thread_id=thread.thread_id, task_id="task-root",
        reason="subagent_runner_finished", wake_signal={"root_task_id": "task-root"})
    expected = ((False, "task_cancelled") if task_status == "cancelled" else
                (True, "active_turn_user_reply") if accepted else
                (False, "thread_goal_lifecycle_internal"))
    assert _background_delivery_decision(agent, request, store=store, user_reply_accepted=accepted) == expected


@pytest.mark.parametrize("accepted", [False, True])
def test_direct_reply_bypasses_external_idle_silence(tmp_path, accepted):
    from agent_py_agent.agent.conversation import runtime
    from agent_py_agent.agent.conversation.goal_progress_fuse import (
        WAKE_SNAPSHOT_KEY,
        goal_progress_snapshot,
    )

    agent = SimpleAgent(AgentConfig(model_backend="echo"), tmp_path)
    store = agent.conversation_store
    thread = store.threads.create()
    store.tasks.bind({"thread_id": thread.thread_id, "task_id": "root", "status": "active"})
    goal = store.goals.create({"thread_id": thread.thread_id, "task_id": "root", "objective": "持续检查"})
    request = BackgroundRunRequest(thread_id=thread.thread_id, task_id="root", reason="thread_goal_continue",
        wake_signal={"metadata": {WAKE_SNAPSHOT_KEY: goal_progress_snapshot(goal, "active")}})
    result = SimpleNamespace(user_reply_accepted=accepted, tool_call_count=0, material_progress_count=0)
    assert runtime._background_goal_delivery_mode(goal, request, result, agent=agent, store=store,
        deliver=True, delivery_reason="thread_goal_progress", route_ownership=runtime.ROUTE_EXTERNAL) == (
            (True, "thread_goal_progress", False) if accepted else (False, "thread_goal_idle", True))


def test_run_once_publishes_accepted_reply_once(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation.runtime import BackgroundMainAgentRuntime

    agent = SimpleAgent(AgentConfig(model_backend="echo", enable_tools=False), tmp_path)
    store = agent.conversation_store
    thread = store.threads.get_or_create({"canonical_user_id": "owner", "channel": "internal",
        "channel_conversation_id": "direct-reply", "channel_user_id": "owner"})
    store.tasks.bind({"thread_id": thread.thread_id, "task_id": "root", "status": "active"})
    store.goals.create({"thread_id": thread.thread_id, "task_id": "root", "objective": "继续检查"})
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store)
    packet = {"schema_version": "active-turn-user-input.v1", "input_ids": ["accepted"],
              "text": "当前进展", "user_reply_accepted": True}
    monkeypatch.setattr(runtime, "_run_agent", lambda *a, **kw: collect_background_execution_result(
        agent, SimpleNamespace(response="已核对当前进展。", active_turn_user_inputs=[packet]), {}))
    signal = store.wakes.raise_signal({"thread_id": thread.thread_id, "root_task_id": "root",
        "reason": "subagent_runner_finished"})
    params = {"thread_id": thread.thread_id, "task_id": "root", "reason": "subagent_runner_finished",
              "wake_signal": signal.to_dict()}
    report = runtime.run_once(params)
    assert report.delivery_reason == "active_turn_user_reply"
    assert report.delivery_status == "sent"
    runtime.run_once(params)
    rows = store.messages.recent(thread.thread_id, limit=20)
    assert [row.content for row in rows if row.role == "assistant" and row.content] == ["已核对当前进展。"]
