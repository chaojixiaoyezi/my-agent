from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.agent.agent_core.finalization_compact_auto import (
    _compact_auto_continue_depth_exhausted,
)
from agent_py_agent.agent.agent_core.orchestration.create_policy import _create_attributes
from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.conversation import (
    BackgroundMainAgentRuntime,
    BackgroundMainAgentScheduler,
    ConversationStore,
    FakeDeliveryService,
)
from agent_py_agent.agent.conversation.runtime import (
    BackgroundToolPolicyRequest,
    background_prompt,
    background_tool_policy_decision,
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig


def test_scheduled_wake_toolset_includes_work_tools() -> None:
    decision = background_tool_policy_decision(
        None,
        request=BackgroundToolPolicyRequest(reason="scheduled_progress_report"),
    )

    assert decision.profile == "scheduled_progress"
    for tool in ("read_file", "list_files", "write_file", "run_command", "task_progress"):
        assert tool in decision.allowed_tools
    assert "create_subagents" not in decision.allowed_tools


def test_default_wake_toolset_includes_work_tools_and_create() -> None:
    decision = background_tool_policy_decision(None, request=BackgroundToolPolicyRequest(reason=""))

    assert "read_file" in decision.allowed_tools
    assert "create_subagents" in decision.allowed_tools


def test_goal_wake_has_goal_lifecycle_and_work_tools() -> None:
    decision = background_tool_policy_decision(
        None,
        request=BackgroundToolPolicyRequest(reason="thread_goal_continue"),
    )

    assert decision.profile == "thread_goal"
    for tool in ("get_goal", "update_goal", "read_file", "write_file", "run_command"):
        assert tool in decision.allowed_tools

    goal = SimpleNamespace(objective="完成测试", tokens_used=10, token_budget=100)
    prompt = background_prompt("thread_goal_continue", goal=goal)
    assert "Continue working toward the active thread goal" in prompt
    assert 'update_goal with status "complete"' in prompt
    assert "Completion audit" in prompt


def test_all_wake_profiles_can_resolve_and_cancel_stuck_children() -> None:
    for reason in ("scheduled_progress_report", "subagent_runner_finished", "", "urgent_wake_signal"):
        decision = background_tool_policy_decision(None, request=BackgroundToolPolicyRequest(reason=reason))
        assert "resolve_capability_requests" in decision.allowed_tools
        assert "cancel_subagents" in decision.allowed_tools


def test_background_prompt_scheduled_wake_is_self_continuation() -> None:
    prompt = background_prompt("scheduled_progress_report")

    assert "续跑轮" in prompt
    assert "wait_reason" in prompt
    assert "不会有人回答" in prompt


def test_due_policy_wake_carries_wait_reason_into_prompt(tmp_path: Path) -> None:
    captured: list[str] = []

    class _Backend:
        name = "capturing"

        def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
            captured.append(prompt)
            return ModelResponse(text="本轮检查完成。", backend=self.name)

    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    agent.backend = _Backend()
    store = ConversationStore(tmp_path / "conversations")
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=FakeDeliveryService())
    scheduler = BackgroundMainAgentScheduler({"runtime": runtime, "store": store})
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "now": 10.0,
        }
    )
    store.bind_task({"thread_id": thread.thread_id, "task_id": "task-watch", "goal": "盯日志", "now": 11.0})
    store.set_progress_policy(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-watch",
            "interval_seconds": 60,
            "route_channel": "internal",
            "route_target": "thread-1",
            "metadata": {"kind": "subagent_progress_watch", "tool": "wait", "reason": "盯日志增量有目标行才上报"},
            "now": 12.0,
        }
    )

    reports = scheduler.tick(now=73.0)

    assert len(reports) == 1
    assert len(captured) == 1
    assert "盯日志增量有目标行才上报" in captured[0]
    assert "续跑轮" in captured[0]


def _depth_ctx(depth: int, attrs: dict | None):
    return SimpleNamespace(compact_auto_continue_depth=depth, task_attributes=attrs)


def test_compact_depth_cap_holds_for_regular_runs() -> None:
    agent = SimpleNamespace(config=SimpleNamespace(memory_compact_auto_continue_max_depth=50))

    assert _compact_auto_continue_depth_exhausted(agent, _depth_ctx(50, None)) is True
    assert _compact_auto_continue_depth_exhausted(agent, _depth_ctx(49, None)) is False


def test_compact_depth_cap_exempts_declared_long_running() -> None:
    agent = SimpleNamespace(config=SimpleNamespace(memory_compact_auto_continue_max_depth=50))

    assert _compact_auto_continue_depth_exhausted(agent, _depth_ctx(50, {"long_running": True})) is False
    assert _compact_auto_continue_depth_exhausted(agent, _depth_ctx(5000, {"long_running": True})) is False
    assert _compact_auto_continue_depth_exhausted(agent, _depth_ctx(50, {"long_running": "yes"})) is True


def test_create_subagents_long_running_lands_in_task_attributes() -> None:
    assert _create_attributes({"long_running": True, "goal": "持续盯守日志"}, None).get("long_running") is True
    assert "long_running" not in _create_attributes({"goal": "普通任务"}, None)
