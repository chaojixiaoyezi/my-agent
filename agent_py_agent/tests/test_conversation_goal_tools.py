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


def test_goal_controls_are_visible_without_search_by_default(tmp_path) -> None:
    agent, _thread, _goal = _goal_agent(tmp_path)
    visible = {spec.name for spec in agent.tools.model_visible_specs()}
    assert {"get_goal", "create_goal", "update_goal"} <= visible
    description = agent.tools.tools["create_goal"].model_spec.description
    assert "at most one unfinished goal" in description
    assert "does not create a second executor" in description


@pytest.mark.parametrize("status", ["active", "complete", "paused", "blocked", "budget_limited", "usage_limited"])
def test_bound_goal_id_does_not_replace_current_lifecycle_state(tmp_path, status):
    from agent_py_agent.agent.agent_core._tool_loop_service import (
        _active_goal_continuation_available,
    )

    agent, thread, goal = _goal_agent(tmp_path)
    params = agent._current_run_params
    params.task_attributes["thread_goal_id"] = goal.goal_id
    agent.conversation_store.update_goal({"thread_id": thread.thread_id, "goal_id": goal.goal_id, "status": status})
    assert _active_goal_continuation_available(agent, params) is (status == "active")


def test_goal_continuation_promise_rejects_stale_and_foreign_bindings(tmp_path):
    from agent_py_agent.agent.agent_core._tool_loop_service import (
        _active_goal_continuation_available,
    )

    agent, thread, goal = _goal_agent(tmp_path)
    params = agent._current_run_params
    params.task_attributes["thread_goal_id"] = goal.goal_id
    params.task_id = "different-run-task"
    assert not _active_goal_continuation_available(agent, params)
    params.task_attributes["conversation_task_id"] = params.task_id
    assert not _active_goal_continuation_available(agent, params)
    params.task_id = goal.task_id
    params.task_attributes["conversation_task_id"] = goal.task_id
    agent.conversation_store.delete_goal(thread.thread_id, expected_goal_id=goal.goal_id)
    assert not _active_goal_continuation_available(agent, params)


@pytest.mark.parametrize("scope", ["foreground", "detached"])
def test_named_goal_keeps_existing_task_execution_scope(tmp_path, scope) -> None:
    agent, thread, previous = _goal_agent(tmp_path)
    store = agent.conversation_store
    store.delete_goal(thread.thread_id, expected_goal_id=previous.goal_id)
    store.bind_task({
        "thread_id": thread.thread_id, "task_id": previous.task_id,
        "goal": "当前工作", "status": "active", "cancellation_scope": scope,
    })

    result = agent.tools.tools["create_goal"].execute({"name": "交付评审", "objective": "安排交付评审"})
    receipt = json.loads(result.output)
    assert result.ok
    assert receipt["execution"]["mode"] == "current_turn"
    assert store.load_task_link(previous.task_id).cancellation_scope == scope

    sibling = agent.tools.tools["create_goal"].execute({"name": "另一项评审", "objective": "独立验证"})
    assert not sibling.ok and sibling.error_code == "GOAL_STATE_CONFLICT"
    assert len(store.load_goals(thread.thread_id)) == 1
    assert not store.pending_wake_signals()
    assert store.load_task_link(previous.task_id).cancellation_scope == scope


def test_goal_tool_receipts_distinguish_current_and_history(tmp_path) -> None:
    agent, thread, first = _goal_agent(tmp_path)
    agent._current_run_params.task_attributes["thread_goal_id"] = first.goal_id
    refused = agent.tools.tools["create_goal"].execute({"name": "索引", "objective": "制作索引"})
    assert not refused.ok and refused.error_code == "GOAL_STATE_CONFLICT"
    assert len(agent.conversation_store.load_goals(thread.thread_id)) == 1
    status = json.loads(agent.tools.tools["get_goal"].execute({}).output)
    assert status["goal"]["goalId"] == first.goal_id
    finished = agent.tools.tools["update_goal"].execute({"status": "complete"})
    finished_scope = json.loads(finished.output)["execution"]
    assert finished_scope["current_goal"]["status"] == "complete"
    assert finished_scope["other_goals"] == []
    assert finished_scope["next_action"] == "report_current_goal"
    assert not agent.conversation_store.pending_wake_signals()


def test_goal_scope_updates_during_foreground_without_mutating_history(tmp_path) -> None:
    from agent_py_agent.agent.conversation.goal_prompting import current_goal_scope_prompt

    agent, thread, first = _goal_agent(tmp_path)
    params = SimpleNamespace(task_attributes=agent._current_run_params.task_attributes, context_scope="conversation")
    original_history = agent.conversation_store.recent_messages(thread.thread_id)
    before = current_goal_scope_prompt(agent, params)
    assert first.goal_id in before
    changed = agent.tools.tools["update_goal"].execute({"objective": "制作新的索引", "expected_revision": first.revision})
    assert changed.ok
    after = current_goal_scope_prompt(agent, params)
    assert "制作新的索引" in after and before != after
    assert after == current_goal_scope_prompt(agent, params)
    assert agent.conversation_store.recent_messages(thread.thread_id) == original_history
    params.context_scope = "task_local"
    assert current_goal_scope_prompt(agent, params) == ""


def test_new_goals_reach_actual_native_requests_in_the_creating_turn(tmp_path) -> None:
    from agent_py_agent.agent.backends.base import ProviderToolCapability, _utc_now_iso

    agent = SimpleAgent(AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=False), tmp_path)
    (tmp_path / "notes.txt").write_text("交付说明", encoding="utf-8")
    thread = agent.conversation_store.get_or_create_thread(
        {"canonical_user_id": "goal-native-test", "channel": "internal", "channel_conversation_id": "native-goals"}
    )

    # LLM: 走完整前台 run 与真实请求装配，替身只代替供应商；不得预绑 task 或共享测试用属性掩盖晋升缺口。
    # 类用途: 按轮创建两个 Goal 并完成第一个，记录各次真正交给模型的状态，而非仅断言提示 helper。
    class GoalRequestBackend:
        name = "goal_native_request_test"

        def __init__(self):
            self.requests = []

        def probe_tool_capability(self):
            return ProviderToolCapability(provider=self.name, endpoint="local://goal-test", model="", stream=False,
                                          native_supported=True, evidence="test_native_tools", observed_at=_utc_now_iso())

        def generate(self, prompt, on_chunk=None, **kwargs):
            self.requests.append(str(prompt) + json.dumps(kwargs.get("messages"), ensure_ascii=False))
            calls = (
                ("create_goal", {"name": "资料", "objective": "整理资料"}),
                ("create_goal", {"name": "索引", "objective": "制作索引"}),
                ("update_goal", {"status": "complete"}),
                ("read_file", {"path": "notes.txt"}),
            )
            index = len(self.requests) - 1
            if index < len(calls):
                name, payload = calls[index]
                return ModelResponse(text="", backend=self.name,
                                     tool_use_blocks=[{"id": f"goal-call-{index}", "name": name, "input": payload}])
            return ModelResponse(text="资料目标已完成，索引目标仍在独立处理。", backend=self.name)

    backend = GoalRequestBackend()
    agent.backend = backend
    agent.run("建立资料和索引两个 Goal", save=False, task_id="request-new-goals", source="gateway",
              task_attributes={"conversation_thread_id": thread.thread_id})
    assert len(backend.requests) == 5
    assert "[current-goal-scope]" not in backend.requests[0]
    assert "[current-goal-scope]" in backend.requests[1]
    goals = agent.conversation_store.load_goals(thread.thread_id)
    assert len(goals) == 1
    assert "GOAL_STATE_CONFLICT" in backend.requests[2]
    for goal in goals:
        assert goal.goal_id in backend.requests[2]
    assert "[current-goal-scope]" in backend.requests[3]
    assert "[current-goal-scope]" in backend.requests[4]
    assert "report_current_goal" in backend.requests[4]


def test_named_goal_does_not_create_a_second_executor(tmp_path) -> None:
    agent, thread, existing = _goal_agent(tmp_path)
    result = agent.tools.tools["create_goal"].execute({"name": "整理索引", "objective": "整理文件索引"})
    assert not result.ok and result.error_code == "GOAL_STATE_CONFLICT"
    goals = agent.conversation_store.load_goals(thread.thread_id)
    assert [goal.goal_id for goal in goals] == [existing.goal_id]
    assert agent._current_run_params.task_attributes["conversation_task_id"] == existing.task_id
    assert not agent.conversation_store.pending_wake_signals()


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
    assert invalid_create.reported_error_code == "GOAL_STATE_CONFLICT"
    assert invalid_create.error_code == "GOAL_STATE_CONFLICT"


def test_get_goal_without_existing_goal_matches_sample_a_response(tmp_path) -> None:
    agent, thread, goal = _goal_agent(tmp_path)
    agent.conversation_store.delete_goal(thread.thread_id, expected_goal_id=goal.goal_id)

    result = agent.tools.tools["get_goal"].execute({})

    assert result.ok is True
    assert '"goal": null' in result.output


@pytest.mark.parametrize("missing", [True, False])
def test_update_goal_precondition_failure_is_not_unknown(tmp_path, missing):
    from agent_py_agent.tests._tool_runtime_harness import execute_canonical_test_call

    agent, thread, goal = _goal_agent(tmp_path)
    if missing:
        agent.conversation_store.delete_goal(thread.thread_id, expected_goal_id=goal.goal_id)
    else:
        agent._current_run_params.task_attributes["conversation_task_id"] = "another-task"
    result = execute_canonical_test_call(
        tmp_path, tools={"update_goal": agent.tools.tools["update_goal"]},
        tool_name="update_goal", arguments={"status": "complete"},
        operation_store=agent.local_store,
    ).result

    assert result.error_code == ("GOAL_NOT_FOUND" if missing else "GOAL_STATE_CONFLICT")
    assert result.effect_outcome == "not_started"
    assert result.failure_stage == "validation"
    assert result.operation.status == "failed"
    if not missing:
        assert agent.conversation_store.load_goal(thread.thread_id).status == "active"


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


def test_goal_objective_limit_and_public_schema_match_sample_a(tmp_path) -> None:
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
        "goalId",
        "threadId",
        "objective",
        "status",
        "tokensUsed",
        "timeUsedSeconds",
        "createdAt",
        "updatedAt",
        "revision",
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


def test_different_agent_goal_clocks_and_persistence_are_independent(tmp_path) -> None:
    agent, thread, first = _goal_agent(tmp_path)
    store = agent.conversation_store
    child_thread = store.get_or_create_thread({"canonical_user_id": "child", "channel": "internal", "channel_conversation_id": "child-session"})
    second = store.create_goal({"thread_id": child_thread.thread_id, "objective": "完成第二件事"})
    store.clear_goal_accounting(thread.thread_id)
    store.clear_goal_accounting(child_thread.thread_id)
    store.begin_goal_accounting(first, reset=True, monotonic_now=10.0)
    store.begin_goal_accounting(second, reset=True, monotonic_now=20.0)
    assert store.take_goal_elapsed_seconds(first, monotonic_now=13.2) == 3
    assert store.take_goal_elapsed_seconds(second, monotonic_now=22.8) == 2
    restarted = ConversationStore(store.root)
    assert restarted.load_goal(thread.thread_id).goal_id == first.goal_id
    assert restarted.load_goal(child_thread.thread_id).goal_id == second.goal_id


def test_new_store_starts_fresh_clock_without_charging_service_downtime(
    tmp_path,
    monkeypatch,
) -> None:
    import weakref

    from agent_py_agent.agent.conversation import goal_clock

    agent, thread, _goal = _goal_agent(tmp_path)
    # 模拟服务重启后的空进程注册表；同一进程内新建 Store 并不等于服务重启。
    monkeypatch.setattr(goal_clock, "_CLOCK_GROUPS", weakref.WeakValueDictionary())
    monkeypatch.setattr(
        "agent_py_agent.agent.conversation.store.time.monotonic",
        lambda: 5_000.0,
    )
    restarted = ConversationStore(agent.conversation_store.root)

    loaded = restarted.load_goal(thread.thread_id)

    assert loaded is not None and loaded.time_used_seconds == 0
    assert restarted.take_goal_elapsed_seconds(loaded, monotonic_now=5_000.0) == 0
    assert restarted.take_goal_elapsed_seconds(loaded, monotonic_now=5_002.9) == 2


def test_foreground_background_and_control_share_one_goal_clock(tmp_path) -> None:
    agent, thread, goal = _goal_agent(tmp_path)
    foreground = agent.conversation_store
    background = ConversationStore(foreground.root)
    control = ConversationStore(foreground.root)
    foreground.begin_goal_accounting(goal, reset=True, monotonic_now=10.0)
    assert foreground.take_goal_elapsed_seconds(goal, monotonic_now=20.5) == 10
    assert background.take_goal_elapsed_seconds(goal, monotonic_now=20.5) == 0
    assert control.take_goal_elapsed_seconds(goal, monotonic_now=21.0) == 1
    assert foreground.take_goal_elapsed_seconds(goal, monotonic_now=21.0) == 0
    other_owner = ConversationStore(tmp_path / "different-owner")
    other_owner.begin_goal_accounting(goal, reset=True, monotonic_now=10.0)
    assert other_owner.take_goal_elapsed_seconds(goal, monotonic_now=20.5) == 10
    control.clear_goal_accounting(thread.thread_id, goal_id=goal.goal_id)
    assert background.take_goal_elapsed_seconds(goal, monotonic_now=22.0) == 0


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


def test_update_goal_complete_preserves_task_until_turn_finalization(tmp_path) -> None:
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
    assert links[goal.task_id].status == "active"


@pytest.mark.parametrize("child_status", ["PLANNING", "RUNNING", "UNKNOWN"])
def test_completed_goal_does_not_close_an_open_child_tree(tmp_path, monkeypatch, child_status) -> None:
    from agent_py_agent.agent.conversation.authority import CONVERSATION_TASK_TURN_ACTIVE_ATTR
    from agent_py_agent.agent.conversation.task_promotion import complete_current_conversation_task

    agent, thread, goal = _goal_agent(tmp_path)
    attrs = agent._current_run_params.task_attributes
    attrs[CONVERSATION_TASK_TURN_ACTIVE_ATTR] = True
    child = SimpleNamespace(id="child-still-owned", status=child_status)
    monkeypatch.setattr(agent, "subagent_run_ids_for_request", lambda task_id: [child.id])
    monkeypatch.setattr(agent.subagents, "list_runs", lambda: [child])
    assert agent.tools.tools["update_goal"].execute({"status": "complete"}).ok
    assert not complete_current_conversation_task(agent, attrs, source="gateway", current_task_id=goal.task_id)
    assert agent.conversation_store.load_task_link(goal.task_id).status == "active"
    assert child.status == child_status
    assert agent.conversation_store.load_goal(thread.thread_id, goal_id=goal.goal_id).status == "complete"


def test_update_goal_complete_also_closes_terminal_runtime_tree(tmp_path) -> None:
    """Goal 完成不提前关任务；真实 finalization 仍能关闭没有未决子树的执行任务。"""
    from agent_py_agent.agent.conversation.authority import CONVERSATION_TASK_TURN_ACTIVE_ATTR
    from agent_py_agent.agent.conversation.task_promotion import complete_current_conversation_task
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
            CONVERSATION_TASK_TURN_ACTIVE_ATTR: True,
        },
    )
    agent._current_run_params = params

    result = agent.tools.tools["update_goal"].execute({"status": "complete"})
    assert result.ok is True
    assert "conversation_task_completed" not in params.task_attributes
    assert agent.conversation_store.load_task_link(goal.task_id).status == "active"
    assert complete_current_conversation_task(
        agent, params.task_attributes, source="gateway", current_task_id=goal.task_id,
    )

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


# LLM: Goal 预算口径必须跨协议等价：两种协议的"总输入"含义不同（Anthropic 三字段互斥、
# OpenAI prompt_tokens 已含嵌套 cached），只有减去"缓存命中"后才可比，缓存写入不能免费。
# 函数用途: 用文档示例锁定去缓存命中的 Goal 扣减口径，防止回退成"只减嵌套 cached"。
def test_goal_token_usage_is_protocol_equivalent_and_keeps_cache_writes() -> None:
    from types import SimpleNamespace

    from agent_py_agent.agent.agent_core.model.usage import goal_token_usage

    anthropic = SimpleNamespace(
        usage={
            "input_tokens": 100,
            "cache_creation_input_tokens": 200,
            "cache_read_input_tokens": 700,
            "output_tokens": 50,
        }
    )
    openai = SimpleNamespace(
        usage={
            "prompt_tokens": 1000,
            "prompt_tokens_details": {"cached_tokens": 700},
            "completion_tokens": 50,
        }
    )
    assert goal_token_usage(anthropic) == goal_token_usage(openai) == 350

    all_hit = SimpleNamespace(
        usage={"input_tokens": 0, "cache_read_input_tokens": 700, "output_tokens": 50}
    )
    assert goal_token_usage(all_hit) == 50  # 命中不算预算，但输出照扣

    write_only = SimpleNamespace(
        usage={"input_tokens": 0, "cache_creation_input_tokens": 200, "output_tokens": 50}
    )
    assert goal_token_usage(write_only) == 250  # 缓存写入不是免费


# LLM: 真实事故回归：同一 task 上先后建了两条 active 目标（名字不同即绕过旧守卫），
# 之后 get_goal/update_goal 恒 GOAL_STATE_CONFLICT，_matching_goal_status 也把该 task 的
# goal 当成"不存在"。这里锁住"同 task 未完成目标不得再建"，并且不做隐式 supersede。
# 函数用途: 验证未显式命名的第二条目标不会悄悄开启额外工作。
def test_create_goal_rejects_second_unnamed_goal_on_same_task(tmp_path) -> None:
    agent, thread, existing = _goal_agent(tmp_path)
    first = agent.conversation_store.load_goal(thread.thread_id, task_id=existing.task_id)
    assert first is not None and first.status == "active"

    rejected = agent.tools.tools["create_goal"].execute(
        {"objective": "另一份资料整理"}
    )

    assert rejected.ok is False
    goals = agent.conversation_store.load_goals(thread.thread_id)
    same_task = [goal for goal in goals if goal.task_id == existing.task_id]
    assert len(same_task) == 1, "同 task 不得出现第二条未完成目标"
    assert same_task[0].goal_id == existing.goal_id
