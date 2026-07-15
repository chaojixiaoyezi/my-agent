from __future__ import annotations

from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig


def _goal_agent(tmp_path):
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=False),
        tmp_path,
    )
    thread = agent.conversation_store.get_or_create_thread(
        {
            "canonical_user_id": "u-1",
            "channel": "feishu",
            "channel_conversation_id": "c-1",
            "channel_user_id": "u-1",
        }
    )
    goal = agent.conversation_store.create_goal(
        {"thread_id": thread.thread_id, "objective": "持续完成资料整理"}
    )
    agent.conversation_store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": goal.task_id,
            "goal": goal.objective,
            "status": "active",
        }
    )
    agent._current_run_params = RunParams(
        task_id=goal.task_id,
        task_attributes={
            "conversation_thread_id": thread.thread_id,
            "conversation_task_id": goal.task_id,
        },
    )
    return agent, thread, goal


def test_goal_tools_require_exact_current_goal_context(tmp_path) -> None:
    agent, _thread, goal = _goal_agent(tmp_path)
    get_result = agent.tools.tools["get_goal"].execute({})

    assert get_result.ok is True
    assert goal.objective in get_result.output

    agent._current_run_params = RunParams(
        task_attributes={
            "conversation_thread_id": goal.thread_id,
            "conversation_task_id": "another-task",
        }
    )
    rejected = agent.tools.tools["update_goal"].execute({"status": "complete"})
    assert rejected.ok is False
    assert rejected.reported_error_code == "GOAL_NOT_FOUND"


def test_update_goal_complete_closes_task_link(tmp_path) -> None:
    agent, thread, goal = _goal_agent(tmp_path)

    result = agent.tools.tools["update_goal"].execute({"status": "complete"})

    assert result.ok is True
    assert agent.conversation_store.load_goal(thread.thread_id).status == "complete"
    links = {item.task_id: item for item in agent.conversation_store.task_links(thread.thread_id)}
    assert links[goal.task_id].status == "completed"


def test_update_goal_blocked_preserves_resumable_task(tmp_path) -> None:
    agent, thread, goal = _goal_agent(tmp_path)

    result = agent.tools.tools["update_goal"].execute({"status": "blocked"})

    assert result.ok is True
    assert agent.conversation_store.load_goal(thread.thread_id).status == "blocked"
    links = {item.task_id: item for item in agent.conversation_store.task_links(thread.thread_id)}
    assert links[goal.task_id].status == "interrupted"
