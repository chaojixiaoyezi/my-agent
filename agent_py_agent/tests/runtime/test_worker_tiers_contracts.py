from __future__ import annotations

from agent_py_agent.agent.runtime.worker_tiers import (
    WorkerTier,
    build_worker_context,
    choose_worker_tier,
)


def test_large_complex_task_uses_task_agent_context():
    tier = choose_worker_tier(complexity="large", requires_user_memory=True, requires_feedback=True)

    assert tier == WorkerTier.TASK_AGENT
    context = build_worker_context(
        tier,
        task_id="task-1",
        goal="ship complete feature",
        user_id="user-1",
        session_id="sess-1",
    )
    assert context["worker_tier"] == "task_agent"
    assert context["context_bundle_level"] == "full_task"
    assert context["feedback_channel"] == "session:sess-1"
    assert context["can_request_user_input"] is True


def test_small_clear_task_uses_weak_subagent_context():
    tier = choose_worker_tier(complexity="short", requires_user_memory=False, requires_feedback=False)

    assert tier == WorkerTier.WEAK_SUBAGENT
    context = build_worker_context(
        tier,
        task_id="task-1",
        goal="read one file",
        user_id="user-1",
        session_id="sess-1",
    )
    assert context["worker_tier"] == "weak_subagent"
    assert context["context_bundle_level"] == "narrow_task"
    assert context["feedback_channel"] is None
    assert context["can_request_user_input"] is False


def test_tool_only_task_uses_tool_worker():
    tier = choose_worker_tier(complexity="tool", requires_user_memory=False, requires_feedback=False)

    assert tier == WorkerTier.TOOL_WORKER
