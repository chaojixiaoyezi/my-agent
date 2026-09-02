from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.runtime.goal_accounting import account_goal_model_response
from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.agent_core.runtime_mixin import _settle_main_agent_run
from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.conversation import ConversationStore
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
    assert rejected.reported_error_code == "GOAL_STATE_CONFLICT"
    assert rejected.error_code == "GOAL_STATE_CONFLICT"


def test_goal_tool_errors_keep_registered_control_codes(tmp_path) -> None:
    agent, thread, goal = _goal_agent(tmp_path)

    agent._current_run_params = RunParams(task_attributes={})
    missing_context = agent.tools.tools["get_goal"].execute({})
    assert missing_context.error_code == "GOAL_CONTEXT_REQUIRED"

    agent._current_run_params = RunParams(
        task_id=goal.task_id,
        task_attributes={
            "conversation_thread_id": thread.thread_id,
            "conversation_task_id": goal.task_id,
        },
    )
    agent.conversation_store.delete_goal(thread.thread_id, expected_goal_id=goal.goal_id)
    missing_goal = agent.tools.tools["update_goal"].execute({"status": "complete"})
    assert missing_goal.reported_error_code == "GOAL_NOT_FOUND"
    assert missing_goal.error_code == "GOAL_NOT_FOUND"
    assert missing_goal.recommended_action == "change_strategy"

    recreated = agent.conversation_store.create_goal(
        {"thread_id": thread.thread_id, "objective": "仍在进行的目标"}
    )
    invalid_create = agent.tools.tools["create_goal"].execute({"objective": "另一个目标"})
    assert recreated.status == "active"
    assert invalid_create.reported_error_code == "GOAL_INVALID_REQUEST"
    assert invalid_create.error_code == "GOAL_INVALID_REQUEST"


def test_get_goal_without_existing_goal_matches_codex_response(tmp_path) -> None:
    agent, thread, goal = _goal_agent(tmp_path)
    agent.conversation_store.delete_goal(thread.thread_id, expected_goal_id=goal.goal_id)

    result = agent.tools.tools["get_goal"].execute({})

    assert result.ok is True
    assert '"goal": null' in result.output


def test_legacy_cleared_goal_is_absent_but_unknown_status_fails_closed(tmp_path) -> None:
    agent, thread, goal = _goal_agent(tmp_path)
    path = agent.conversation_store._goal_path(thread.thread_id)
    payload = goal.to_dict()
    payload["status"] = "cleared"
    path.write_text(json.dumps(payload), encoding="utf-8")

    loaded, error = agent.conversation_store.load_goal_report(thread.thread_id)

    assert loaded is None
    assert error is None

    payload["status"] = "unknown-state"
    path.write_text(json.dumps(payload), encoding="utf-8")
    loaded, error = agent.conversation_store.load_goal_report(thread.thread_id)

    assert loaded is None
    assert error is not None
    assert "thread goal status is invalid: unknown-state" in str(error.get("message"))


def test_create_goal_requires_explicit_tool_and_rejects_unfinished_goal(tmp_path) -> None:
    agent, thread, existing = _goal_agent(tmp_path)

    rejected = agent.tools.tools["create_goal"].execute({"objective": "新的目标"})
    assert rejected.ok is False

    agent.conversation_store.update_goal(
        {
            "thread_id": thread.thread_id,
            "goal_id": existing.goal_id,
            "status": "complete",
        }
    )
    created = agent.tools.tools["create_goal"].execute(
        {"objective": "新的目标", "token_budget": 1000}
    )

    assert created.ok is True
    goal = agent.conversation_store.load_goal(thread.thread_id)
    assert goal is not None and goal.objective == "新的目标"
    assert goal.token_budget == 1000 and goal.tokens_used == 0


def test_stop_named_work_tool_uses_exact_thread_name_and_kind(tmp_path) -> None:
    agent, thread, existing = _goal_agent(tmp_path)
    agent.conversation_store.delete_goal(
        thread.thread_id,
        expected_goal_id=existing.goal_id,
    )
    goal = agent.conversation_store.create_goal(
        {
            "thread_id": thread.thread_id,
            "objective": "持续整理周报",
            "name": "周报整理",
        }
    )
    agent.conversation_store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": goal.task_id,
            "goal": goal.objective,
            "status": "active",
            "work_kind": "goal",
            "work_name": goal.name,
        }
    )
    agent._current_run_params = RunParams(
        task_attributes={
            "conversation_thread_id": thread.thread_id,
            "conversation_task_id": "ordinary-turn",
        }
    )

    result = agent.tools.tools["stop_named_work"].execute({"name": "周报整理"})

    assert result.ok is True
    assert agent.conversation_store.load_goals(thread.thread_id) == []
    link = next(
        item
        for item in agent.conversation_store.task_links(thread.thread_id)
        if item.task_id == goal.task_id
    )
    assert link.status == "cancelled"


def test_stop_named_work_without_kind_fails_closed_on_cross_kind_name_conflict(
    tmp_path,
) -> None:
    agent, thread, existing = _goal_agent(tmp_path)
    agent.conversation_store.delete_goal(
        thread.thread_id,
        expected_goal_id=existing.goal_id,
    )
    goal = agent.conversation_store.create_goal(
        {
            "thread_id": thread.thread_id,
            "objective": "持续整理资料",
            "name": "每日检查",
        }
    )
    for task_id, kind in (
        (goal.task_id, "goal"),
        ("audit-daily", "audit"),
    ):
        agent.conversation_store.bind_task(
            {
                "thread_id": thread.thread_id,
                "task_id": task_id,
                "goal": "持续检查",
                "status": "active",
                "work_kind": kind,
                "work_name": "每日检查",
                "cancellation_scope": "detached",
            }
        )
    agent._current_run_params = RunParams(
        task_attributes={
            "conversation_thread_id": thread.thread_id,
            "conversation_task_id": "ordinary-turn",
        }
    )

    result = agent.tools.tools["stop_named_work"].execute({"name": "每日检查"})

    assert result.ok is False
    assert result.error_code == "NAMED_WORK_CONFLICT"
    assert agent.conversation_store.load_goal(
        thread.thread_id,
        goal_id=goal.goal_id,
    ) is not None
    links = {
        item.task_id: item
        for item in agent.conversation_store.task_links(thread.thread_id)
    }
    assert links[goal.task_id].status == "active"
    assert links["audit-daily"].status == "active"


def test_goal_objective_limit_and_public_schema_match_codex(tmp_path) -> None:
    agent, thread, goal = _goal_agent(tmp_path)

    with pytest.raises(ValueError, match="4000"):
        agent.conversation_store.update_goal(
            {
                "thread_id": thread.thread_id,
                "goal_id": goal.goal_id,
                "objective": "x" * 4001,
            }
        )

    public = goal.public_dict()
    assert set(public) == {
        "threadId",
        "objective",
        "status",
        "tokensUsed",
        "timeUsedSeconds",
        "createdAt",
        "updatedAt",
    }
    assert "goal_id" not in public and "task_id" not in public


def test_goal_clock_preserves_fractional_seconds_between_charges(tmp_path) -> None:
    agent, _thread, goal = _goal_agent(tmp_path)
    store = agent.conversation_store
    store.clear_goal_accounting(goal.thread_id, goal_id=goal.goal_id)
    store.begin_goal_accounting(goal, reset=True, monotonic_now=10.0)

    assert store.take_goal_elapsed_seconds(goal, monotonic_now=12.7) == 2
    assert store.take_goal_elapsed_seconds(goal, monotonic_now=13.2) == 1
    assert store.take_goal_elapsed_seconds(goal, monotonic_now=13.8) == 0


def test_named_goal_clocks_and_persistence_are_independent(tmp_path) -> None:
    agent, thread, existing = _goal_agent(tmp_path)
    agent.conversation_store.delete_goal(
        thread.thread_id,
        expected_goal_id=existing.goal_id,
    )
    first = agent.conversation_store.create_goal(
        {
            "thread_id": thread.thread_id,
            "name": "目标一",
            "objective": "完成第一件事",
        }
    )
    second = agent.conversation_store.create_goal(
        {
            "thread_id": thread.thread_id,
            "name": "目标二",
            "objective": "完成第二件事",
        }
    )
    store = agent.conversation_store
    store.clear_goal_accounting(thread.thread_id)
    store.begin_goal_accounting(first, reset=True, monotonic_now=10.0)
    store.begin_goal_accounting(second, reset=True, monotonic_now=20.0)

    assert store.take_goal_elapsed_seconds(first, monotonic_now=13.2) == 3
    assert store.take_goal_elapsed_seconds(second, monotonic_now=22.8) == 2

    restarted = ConversationStore(store.root)
    loaded = restarted.load_goals(thread.thread_id)
    assert [(goal.name, goal.objective) for goal in loaded] == [
        ("目标一", "完成第一件事"),
        ("目标二", "完成第二件事"),
    ]


def test_new_store_starts_fresh_clock_without_charging_service_downtime(
    tmp_path,
    monkeypatch,
) -> None:
    agent, thread, _goal = _goal_agent(tmp_path)
    monkeypatch.setattr(
        "agent_py_agent.agent.conversation.store.time.monotonic",
        lambda: 5_000.0,
    )
    restarted = ConversationStore(agent.conversation_store.root)

    loaded = restarted.load_goal(thread.thread_id)

    assert loaded is not None and loaded.time_used_seconds == 0
    assert restarted.take_goal_elapsed_seconds(loaded, monotonic_now=5_000.0) == 0
    assert restarted.take_goal_elapsed_seconds(loaded, monotonic_now=5_002.9) == 2


def test_pause_accounts_live_time_and_resume_starts_a_new_baseline(
    tmp_path,
    monkeypatch,
) -> None:
    agent, thread, goal = _goal_agent(tmp_path)
    store = agent.conversation_store
    clock = {"now": 10.0}
    monkeypatch.setattr(
        "agent_py_agent.agent.conversation.store.time.monotonic",
        lambda: clock["now"],
    )
    store.clear_goal_accounting(thread.thread_id, goal_id=goal.goal_id)
    store.begin_goal_accounting(goal, reset=True)
    clock["now"] = 15.9

    paused = store.update_goal(
        {
            "thread_id": thread.thread_id,
            "goal_id": goal.goal_id,
            "status": "paused",
            "expected_status": "active",
        }
    )

    assert paused is not None and paused.time_used_seconds == 5
    clock["now"] = 20.0
    resumed = store.update_goal(
        {
            "thread_id": thread.thread_id,
            "goal_id": goal.goal_id,
            "status": "active",
            "expected_status": "paused",
        }
    )
    assert resumed is not None and resumed.time_used_seconds == 5
    assert store.take_goal_elapsed_seconds(resumed, monotonic_now=22.4) == 2


def test_goal_accounting_excludes_openai_cached_input_and_limits_budget(tmp_path) -> None:
    agent, thread, goal = _goal_agent(tmp_path)
    agent.conversation_store.update_goal(
        {
            "thread_id": thread.thread_id,
            "goal_id": goal.goal_id,
            "token_budget": 70,
        }
    )
    params = type(
        "LoopParams",
        (),
        {
            "task_attributes": agent._current_run_params.task_attributes,
            "runtime_injections": [],
            "live_archive_state": {},
        },
    )()
    response = ModelResponse(
        text="done",
        backend="test",
        usage={
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "prompt_tokens_details": {"cached_tokens": 50},
        },
    )

    updated = account_goal_model_response(agent, params, response)

    assert updated is not None and updated.tokens_used == 70
    assert updated.status == "budget_limited"
    assert len(params.runtime_injections) == 1


def test_update_goal_complete_closes_task_link(tmp_path) -> None:
    agent, thread, goal = _goal_agent(tmp_path)
    agent.conversation_store.update_goal(
        {
            "thread_id": thread.thread_id,
            "goal_id": goal.goal_id,
            "token_budget": 1000,
        }
    )

    result = agent.tools.tools["update_goal"].execute({"status": "complete"})

    assert result.ok is True
    payload = json.loads(result.output)
    assert payload["goal"]["status"] == "complete"
    assert payload["goal"]["tokenBudget"] == 1000
    assert payload["completionBudgetReport"]
    assert agent.conversation_store.load_goal(thread.thread_id).status == "complete"
    links = {item.task_id: item for item in agent.conversation_store.task_links(thread.thread_id)}
    assert links[goal.task_id].status == "completed"


def test_update_goal_complete_also_closes_terminal_runtime_tree(tmp_path) -> None:
    """Goal 工具先关持久链接时，最终收口不能依赖随后缺失的临时 completion 标记。"""
    agent, thread, goal = _goal_agent(tmp_path)
    repo = agent.subagents.runtime_db
    rec = repo.record_run_creation(
        owner_id="local/main",
        goal=goal.objective,
        conversation_task_id=goal.task_id,
        run_id="goal-run",
        role="main",
    )
    params = RunParams(
        run_id="goal-run",
        task_id=goal.task_id,
        attempt_id=rec["attempt_id"],
        task_attributes={
            "conversation_thread_id": thread.thread_id,
            "conversation_task_id": goal.task_id,
        },
    )
    agent._current_run_params = params

    result = agent.tools.tools["update_goal"].execute({"status": "complete"})
    assert result.ok is True
    assert "conversation_task_completed" not in params.task_attributes

    _settle_main_agent_run(
        agent,
        params,
        SimpleNamespace(
            runtime_status="ok",
            runtime_reason="",
            runtime_source="",
            tool_rounds=1,
        ),
    )

    task_run = repo.get_task_run(rec["task_run_id"])
    assert task_run is not None
    assert task_run["status"] == "done"
    assert task_run["closed_at"] > 0


def test_update_goal_blocked_preserves_resumable_task(tmp_path) -> None:
    agent, thread, goal = _goal_agent(tmp_path)

    result = agent.tools.tools["update_goal"].execute({"status": "blocked"})

    assert result.ok is True
    assert agent.conversation_store.load_goal(thread.thread_id).status == "blocked"
    links = {item.task_id: item for item in agent.conversation_store.task_links(thread.thread_id)}
    assert links[goal.task_id].status == "interrupted"
