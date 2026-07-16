from __future__ import annotations

from types import SimpleNamespace

from agent_py_agent.agent.agent_core.tool_loop.final_exit_contract import (
    FinalExitRequest,
    FinalExitState,
    final_exit_closeout_decision,
)
from agent_py_agent.agent.agent_core.tool_loop.foreground_cooperative_yield import (
    finalize_foreground_cooperative_yield,
    is_foreground_cooperative_yield_response,
    maybe_queue_foreground_cooperative_yield,
)
from agent_py_agent.agent.agent_core.tool_loop.natural_user_reply import (
    finish_natural_user_reply,
    pending_natural_user_reply,
)
from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.agent.conversation.runtime import (
    BackgroundRunRequest,
    BackgroundToolPolicyRequest,
    _background_delivery_decision,
    _progress_policy_run_reason,
    background_prompt,
    background_tool_policy_decision,
)
from agent_py_agent.agent.settings import AgentConfig


def _runtime(tmp_path, *, source: str = "gateway", lane: str = "task"):
    store = ConversationStore(tmp_path / "conversations")
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "feishu",
            "channel_conversation_id": "chat-1",
            "channel_user_id": "user-1",
            "now": 10.0,
        }
    )
    store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-1",
            "goal": "完成一个可运行项目",
            "now": 11.0,
        }
    )
    agent = SimpleNamespace(
        conversation_store=store,
        config=AgentConfig(
            foreground_task_tool_round_quantum=2,
            foreground_task_resume_delay_seconds=5,
            foreground_task_handoff_fallback_seconds=300,
        ),
    )
    params = SimpleNamespace(
        source=source,
        request_id="request-1",
        task_attributes={
            "conversation_thread_id": thread.thread_id,
            "conversation_task_id": "task-1",
            "conversation_lane": lane,
        },
        live_archive_state={},
    )
    return agent, params


def test_foreground_task_yields_at_safe_quantum_and_resumes_same_task(tmp_path) -> None:
    agent, params = _runtime(tmp_path)

    assert maybe_queue_foreground_cooperative_yield(agent, params, tool_rounds=1) is False
    assert maybe_queue_foreground_cooperative_yield(agent, params, tool_rounds=2) is True
    _assert_interim_phase(params)

    policies = agent.conversation_store.list_progress_policies(enabled_only=True)
    assert len(policies) == 1
    fallback = policies[0]
    assert fallback.task_id == "task-1"
    assert fallback.metadata["kind"] == "foreground_task_handoff_fallback"

    interim = finish_natural_user_reply(
        params,
        ModelResponse(text="我已经开始处理，任务会继续推进；你现在可以接着和我说话。", backend="echo"),
        accepted=True,
    )
    assert is_foreground_cooperative_yield_response(params, interim) is True
    decision = final_exit_closeout_decision(
        FinalExitRequest(agent, params, interim, FinalExitState())
    )
    assert decision.should_continue is False
    assert decision.response is interim

    ctx = SimpleNamespace(
        source=params.source,
        request_id=params.request_id,
        task_attributes=params.task_attributes,
        final_response=interim,
    )
    activation = finalize_foreground_cooperative_yield(agent, ctx)
    assert activation["activated"] is True
    assert agent.conversation_store.get_progress_policy(fallback.policy_id).enabled is False

    enabled = agent.conversation_store.list_progress_policies(enabled_only=True)
    assert len(enabled) == 1
    continuation = enabled[0]
    assert continuation.task_id == fallback.task_id
    assert continuation.thread_id == fallback.thread_id
    assert continuation.metadata["kind"] == "foreground_task_continuation"
    assert _progress_policy_run_reason(continuation) == "foreground_task_continue"

    _assert_idempotent_continuation(agent, ctx, continuation)


def _assert_interim_phase(params) -> None:
    phase = pending_natural_user_reply(params)
    assert phase is not None
    assert phase["kind"] == "foreground_cooperative_yield"
    assert phase["facts"]["task_continues_in_background"] is True
    assert "completed_foreground_tool_rounds" not in phase["facts"]


def _assert_idempotent_continuation(agent, ctx, continuation) -> None:
    repeated = finalize_foreground_cooperative_yield(agent, ctx)
    assert repeated["activated"] is True
    assert repeated["policy_id"] == continuation.policy_id
    assert len(agent.conversation_store.list_progress_policies(enabled_only=True)) == 1


def test_foreground_yield_is_structurally_scoped_to_interactive_task_lane(tmp_path) -> None:
    background_agent, background = _runtime(tmp_path / "background", source="background_main_agent")
    chat_agent, chat = _runtime(tmp_path / "chat", lane="chat")
    disabled_agent, disabled = _runtime(tmp_path / "disabled")
    disabled_agent.config.foreground_task_tool_round_quantum = 0

    assert maybe_queue_foreground_cooperative_yield(background_agent, background, tool_rounds=20) is False
    assert maybe_queue_foreground_cooperative_yield(chat_agent, chat, tool_rounds=20) is False
    assert maybe_queue_foreground_cooperative_yield(disabled_agent, disabled, tool_rounds=20) is False


def test_foreground_continuation_has_full_task_tools_and_stays_internal_until_complete() -> None:
    request = BackgroundToolPolicyRequest(reason="foreground_task_continue")
    decision = background_tool_policy_decision(AgentConfig(), request=request)

    assert decision.profile == "foreground_task_continue"
    assert "create_subagents" in decision.allowed_tools
    assert "write_file" in decision.allowed_tools
    assert "submit_for_acceptance" in decision.allowed_tools
    assert "同一个持久任务" in background_prompt("foreground_task_continue")

    run_request = BackgroundRunRequest(
        thread_id="thread-1",
        task_id="task-1",
        reason="foreground_task_continue",
    )
    assert _background_delivery_decision(object(), run_request, content="还在继续推进。") == (
        False,
        "foreground_task_continuation_internal",
    )
    complete = (
        '[MAIN_AGENT_DELIVERY_COMPLETE]\n{"ok":true,"artifacts":[]}\n'
        "[/MAIN_AGENT_DELIVERY_COMPLETE]"
    )
    assert _background_delivery_decision(object(), run_request, content=complete) == (
        True,
        "foreground_task_completion",
    )
