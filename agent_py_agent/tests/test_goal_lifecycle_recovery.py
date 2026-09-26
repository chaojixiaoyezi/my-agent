from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.control_commands import _goal_command
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts.goal_control_service import (
    GoalControlRequest,
    execute_goal_control_operation,
)
from agent_py_agent.agent.gateway_parts.io import write_json_file_atomic
from agent_py_agent.agent.settings import AgentConfig


# LLM: 只生成临时 owner 下的旧格式冲突样本，不通过产品创建入口绕过当前合同。
# 函数用途: 模拟升级前两条 Goal 共用已完成任务，保留可比对的用量与原始记录。
def _legacy_goals(tmp_path):
    agent = SimpleAgent(AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=False), tmp_path)
    store = agent.conversation_store
    thread = store.threads.get_or_create({"canonical_user_id": "user-test", "channel": "internal", "channel_conversation_id": "goal-recovery"})
    first = store.goals.create({"thread_id": thread.thread_id, "task_id": "task-legacy", "objective": "整理资料", "token_budget": 10000})
    first = replace(first, tokens_used=37, time_used_seconds=12)
    second = replace(first, goal_id="goal-legacy-second", objective="整理索引", tokens_used=19)
    path = store.storage.goals_dir / f"{thread.thread_id}.json"
    payload = json.loads(path.read_text())
    payload["goals"] = [first.to_dict(), second.to_dict()]
    write_json_file_atomic(path, payload)
    store.tasks.bind({"thread_id": thread.thread_id, "task_id": first.task_id, "goal": first.objective, "status": "completed", "work_kind": "goal"})
    return agent, thread, first, second


# LLM: 测试控制入口使用精确目标编号，不修改运行模型或向真实用户发送消息。
# 函数用途: 调用临时会话的显式 /goal 控制，由真实存储记录任务状态和唤醒。
def _control(agent, thread, text):
    return execute_goal_control_operation(GoalControlRequest(
        owner_agent=agent, store=agent.conversation_store, thread=thread, command=_goal_command(text),
        scope=SimpleNamespace(channel="internal", conversation_id="goal-recovery"),
        resume_registry=lambda task_id: None,
    ))


def test_shared_task_is_conflict_not_absent_and_exact_id_remains_readable(tmp_path):
    agent, thread, first, _second = _legacy_goals(tmp_path)
    with pytest.raises(ValueError, match="multiple unfinished"):
        agent.conversation_store.goals.load(thread.thread_id, task_id=first.task_id)
    assert agent.conversation_store.goals.load(thread.thread_id, goal_id=first.goal_id).tokens_used == 37
    view = _control(agent, thread, "")
    assert first.goal_id in view.message and "resume" in view.message
    assert agent.conversation_store.wakes.pending() == []


def test_exact_resume_migrates_only_selected_goal_and_preserves_source(tmp_path):
    agent, thread, first, second = _legacy_goals(tmp_path)
    resumed = _control(agent, thread, f"{first.goal_id} resume")
    assert resumed.ok, resumed.message
    store = agent.conversation_store
    current = store.goals.load(thread.thread_id, goal_id=first.goal_id)
    assert current.task_id != first.task_id and current.status == "active"
    assert current.tokens_used == first.tokens_used and current.token_budget == first.token_budget
    assert current.created_at == first.created_at
    assert current.metadata["task_binding_migration"]["source_goal"] == first.to_dict()
    assert store.goals.load(thread.thread_id, goal_id=second.goal_id).to_dict() == second.to_dict()
    assert store.tasks.load(first.task_id).status == "completed"
    assert store.tasks.load(current.task_id).status == "active"
    repeated = _control(agent, thread, f"{first.goal_id} resume")
    assert repeated.ok and repeated.request_id == current.task_id
    assert len(store.wakes.pending()) == 1
    assert store.goals.load(thread.thread_id, goal_id=first.goal_id).metadata == current.metadata


def test_resume_does_not_migrate_shared_running_task(tmp_path):
    agent, thread, first, second = _legacy_goals(tmp_path)
    agent.conversation_store.tasks.update_status({"task_id": first.task_id, "status": "active"})
    result = _control(agent, thread, f"{first.goal_id} resume")
    assert not result.ok and "尚未确认完成" in result.message
    assert agent.conversation_store.goals.load(thread.thread_id, goal_id=first.goal_id).task_id == second.task_id
    assert agent.conversation_store.wakes.pending() == []


@pytest.mark.parametrize("expired", [False, True])
@pytest.mark.parametrize("detached", [False, True])
def test_goal_migration_never_overrides_an_unreleased_claim(tmp_path, expired, detached):
    from agent_py_agent.agent.conversation.run_claim import detached_task_claim_scope_id

    agent, thread, first, _second = _legacy_goals(tmp_path)
    store = agent.conversation_store
    scope = detached_task_claim_scope_id(thread.thread_id, first.task_id) if detached else ""
    request = {"thread_id": thread.thread_id, "task_id": first.task_id,
               "claim_scope_id": scope, "reason": "goal-recovery-test", "lease_seconds": 60,
               "recover_same_task_only": True}
    if expired:
        request["now"] = 10.0
    claim = store.claims.acquire(request)
    assert claim is not None
    original = store.goals.list(thread.thread_id)
    result = _control(agent, thread, f"{first.goal_id} resume")
    assert not result.ok and "会话执行尚未释放" in result.message
    assert store.goals.list(thread.thread_id) == original
    assert store.claims.load(thread.thread_id, claim_scope_id=scope)["claim_id"] == claim["claim_id"]
    assert store.wakes.pending() == []


def test_active_goal_with_completed_task_can_be_explicitly_resumed(tmp_path):
    agent, thread, first, second = _legacy_goals(tmp_path)
    agent.conversation_store.goals.delete(thread.thread_id, expected_goal_id=second.goal_id)
    result = _control(agent, thread, "resume")
    assert result.ok and result.request_id == first.task_id
    assert agent.conversation_store.tasks.load(first.task_id).status == "active"
    assert len(agent.conversation_store.wakes.pending()) == 1


@pytest.mark.parametrize("action", ["pause", "resume", "clear"])
def test_goal_command_accepts_exact_identity(action):
    parsed = _goal_command(f"goal-known-id {action}")
    assert parsed.valid and parsed.name == "goal-known-id" and parsed.operation == action


def test_completed_goal_history_does_not_hide_new_active_goal(tmp_path):
    agent, thread, first, second = _legacy_goals(tmp_path)
    agent.conversation_store.goals.update({"thread_id": thread.thread_id, "goal_id": first.goal_id, "status": "complete"})
    assert agent.conversation_store.goals.load(thread.thread_id, task_id=first.task_id).goal_id == second.goal_id


@pytest.mark.parametrize("task_status", ["interrupted", "completed"])
def test_completed_goal_history_does_not_block_sequential_goal_resume(tmp_path, task_status):
    agent = SimpleAgent(AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=False), tmp_path)
    store = agent.conversation_store
    thread = store.threads.get_or_create({"canonical_user_id": "user-test", "channel": "internal",
                                          "channel_conversation_id": "goal-recovery"})
    first = store.goals.create({"thread_id": thread.thread_id, "task_id": "same-project", "objective": "完成初版"})
    finished = store.goals.update({"thread_id": thread.thread_id, "goal_id": first.goal_id, "status": "complete"})
    second = store.goals.create({"thread_id": thread.thread_id, "task_id": first.task_id, "objective": "增加备份恢复"})
    store.tasks.bind({"thread_id": thread.thread_id, "task_id": first.task_id, "goal": second.objective,
                     "status": task_status, "work_kind": "goal"})
    store.goals.update({"thread_id": thread.thread_id, "goal_id": second.goal_id, "status": "paused"})
    result = _control(agent, thread, "resume")
    assert result.ok and result.request_id == first.task_id
    current = store.goals.load(thread.thread_id, goal_id=second.goal_id)
    assert current.status == "active" and current.task_id == second.task_id
    assert "task_binding_migration" not in current.metadata
    assert store.goals.load(thread.thread_id, goal_id=first.goal_id) == finished
    assert len(store.wakes.pending()) == 1


def test_child_wake_keeps_exact_goal_scope_and_shared_original_history(tmp_path):
    from agent_py_agent.agent.conversation.runtime import (
        BackgroundRunRequest,
        _goal_runtime_context,
        _run_params,
        background_prompt,
    )

    agent, thread, first, second = _legacy_goals(tmp_path)
    store = agent.conversation_store
    _control(agent, thread, f"{first.goal_id} resume")
    first = store.goals.load(thread.thread_id, goal_id=first.goal_id)
    original = "同时整理资料和索引，两个目标各自推进"
    store.messages.append({"thread_id": thread.thread_id, "role": "user", "content": original,
                          "metadata": {"conversation_request_id": "request-both"}})
    request = BackgroundRunRequest(
        thread_id=thread.thread_id, task_id=first.task_id, reason="subagent_runner_finished",
        wake_signal={"metadata": {"conversation_request_id": "request-both"}},
    )
    context = _goal_runtime_context(agent, store, request)
    assert context.task_objective == first.objective
    assert [item.goal_id for item in context.other_goals] == [second.goal_id]
    params = _run_params(thread.thread_id, request, agent, goal_context=context, thread=thread)
    assert params.task_attributes["thread_goal_id"] == first.goal_id
    assert params.root_user_prompt == first.objective
    prompt = background_prompt(request.reason, goal=context.goal, other_goals=context.other_goals)
    assert first.goal_id in prompt and second.goal_id in prompt
    assert '"current_goal"' in prompt and '"other_goals"' in prompt
    assert "[goal-continuation]" in prompt and "每个代理同时只有一个未结束目标" in prompt
    assert store.messages.recent(thread.thread_id)[0].content == original


@pytest.mark.parametrize("status", ["active", "paused", "blocked", "usage_limited"])
def test_followup_selects_only_active_goal_without_changing_it(tmp_path, status):
    from agent_py_agent.agent.gateway_parts.request_context import (
        GatewayAskRunContext,
        GatewayConversationContext,
        _select_gateway_workspace_link,
    )
    from agent_py_agent.agent.gateway_parts.request_execution import (
        _gateway_run_params,
        _GatewayRunParamsRequest,
    )
    from agent_py_agent.agent.gateway_parts.request_prompt import _conversation_prompt_section
    from agent_py_agent.tests.test_conversation_goal_tools import _goal_agent

    agent, thread, goal = _goal_agent(tmp_path)
    store = agent.conversation_store
    current = store.goals.update({"thread_id": thread.thread_id, "goal_id": goal.goal_id, "status": status})
    goal_data = current.to_dict()
    links = store.tasks.list(thread.thread_id)
    selected, _ = _select_gateway_workspace_link(
        selectable=links, thread=thread, thread_goal=goal_data, request={}, request_id="followup",
    )
    assert (selected is not None) == (status == "active")
    conversation = GatewayConversationContext(thread_id=thread.thread_id, thread_goal=goal_data)
    request = {"id": "followup", "prompt": "补充要求，报告里保留原始来源"}
    context = GatewayAskRunContext(agent, request, tmp_path / "request.json", tmp_path / "response.json", "followup", None)
    params = _gateway_run_params(_GatewayRunParamsRequest(request, context, conversation, request["prompt"]))
    assert params.task_id == (goal.task_id if status == "active" else "")
    assert store.goals.load(thread.thread_id) == current
    prompt = _conversation_prompt_section(conversation)
    assert "纠偏不必写入 Goal 才生效" in prompt
    assert "当前普通用户消息不会自动成为目标引导" not in prompt


def test_followup_runtime_attempt_reuses_goal_and_settles_by_attempt(tmp_path):
    from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
    from agent_py_agent.agent.agent_core.runtime_mixin import (
        _bind_main_agent_authority,
        _settle_main_agent_run_status,
    )
    from agent_py_agent.tests.test_conversation_goal_tools import _goal_agent

    agent, thread, goal = _goal_agent(tmp_path)
    first = _bind_main_agent_authority(agent, RunParams(
        run_id="first-turn", task_id=goal.task_id, task_attributes={"conversation_thread_id": thread.thread_id},
    ))
    _settle_main_agent_run_status(agent, run_id="first-turn", attempt_id=first.attempt_id, runtime_status="cancelled")
    second = _bind_main_agent_authority(agent, replace(first, run_id="followup-turn"))
    repo = agent.subagents.runtime_db
    row = repo.main_agent_run_for_task(goal.task_id)
    assert row["current_attempt_id"] == second.attempt_id != first.attempt_id
    assert repo.agent_run_for_run_id("followup-turn") is None
    _settle_main_agent_run_status(agent, run_id="followup-turn", attempt_id=second.attempt_id, runtime_status="ok")
    assert repo.main_agent_run_for_task(goal.task_id)["status"] == "done"
    assert not repo.has_active_exec_lock(row["agent_run_id"])


def test_compact_keeps_correction_without_editing_goal(tmp_path):
    from agent_py_agent.agent.conversation.compact_landmarks import (
        LandmarkOptions,
        summary_with_conversation_landmarks,
    )
    from agent_py_agent.tests.test_conversation_goal_tools import _goal_agent

    agent, thread, goal = _goal_agent(tmp_path)
    correction = "接下来的报告都附原始来源，称重单位改为千克。"
    store = agent.conversation_store
    store.messages.append({"thread_id": thread.thread_id, "role": "user", "content": goal.objective})
    store.messages.append({"thread_id": thread.thread_id, "role": "user", "content": correction})
    options = LandmarkOptions(max_tokens=6_000)
    first = summary_with_conversation_landmarks("正在整理资料", "", store.messages.recent(thread.thread_id), options=options).text
    second = summary_with_conversation_landmarks("继续整理下一批", first, [], options=options).text
    assert correction in first and correction in second
    assert store.goals.load(thread.thread_id).objective == goal.objective
