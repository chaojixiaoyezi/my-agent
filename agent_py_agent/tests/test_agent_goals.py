from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.runner.context import (
    restore_current_subagent_context,
    set_current_subagent_context,
)
from agent_py_agent.agent.agent_core.runtime.goal_accounting import account_goal_model_response
from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.backends.base import ModelResponse
from agent_py_agent.agent.conversation.agent_control import AgentControlError, read_agent_view
from agent_py_agent.agent.conversation.goal_control import execute_agent_goal_control
from agent_py_agent.agent.conversation.goal_delegation import active_delegated_goal_after_turn
from agent_py_agent.agent.conversation.goal_prompting import current_goal_scope_prompt
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.cli.chat_parts.tui_goal_editor import _request_goal
from agent_py_agent.tests.test_conversation_goal_tools import _goal_agent
from agent_py_agent.tests.test_gateway_agent_control_service import _bound_agent_tree
from agent_py_agent.tests.test_runtime_guidance import _tool_loop_params
from agent_py_agent.tests.test_subagent_runtime_compact import _OverflowThenCompleteChildBackend


def test_goal_editor_displays_structured_http_conflict_without_losing_code():
    response = {"ok": False, "error_code": "GOAL_REVISION_CONFLICT", "error": "目标已变化；草稿已保留，请比较最新内容。"}
    client = SimpleNamespace(gateway_client_only=True, post_gateway_json=lambda *args, **kwargs: (409, response))
    result = _request_goal(client, "session", {"operation": "save"})
    assert result["message"] == response["error"]
    assert result["error_code"] == "GOAL_REVISION_CONFLICT"
    assert "message" not in response


@pytest.mark.parametrize("status", ["active", "paused", "blocked", "usage_limited", "budget_limited"])
def test_one_unfinished_goal_per_agent_even_with_a_new_name_or_task(tmp_path, status):
    agent, thread, first = _goal_agent(tmp_path)
    store = agent.conversation_store
    store.goals.update({"thread_id": thread.thread_id, "goal_id": first.goal_id, "status": status})
    with pytest.raises(ValueError, match="unfinished goal"):
        store.goals.create({"thread_id": thread.thread_id, "task_id": "other-task", "name": "另一个", "objective": "另一项工作"})
    store.goals.update({"thread_id": thread.thread_id, "goal_id": first.goal_id, "status": "complete"})
    second = store.goals.create({"thread_id": thread.thread_id, "objective": "下一项工作"})
    assert len(store.goals.list(thread.thread_id)) == 2
    assert second.goal_id != first.goal_id


def test_user_goal_draft_cas_preserves_usage_and_paused_status(tmp_path):
    agent, scope, child = _bound_agent_tree(tmp_path)
    store = agent.conversation_store
    goal = store.goals.create({"thread_id": child.agent_thread_id, "task_id": child.id, "objective": "原始目标"})
    store.goals.account_usage({"thread_id": goal.thread_id, "goal_id": goal.goal_id, "token_delta": 100, "time_delta_seconds": 2})
    request = {"run_id": child.id, "goal_id": goal.goal_id, "operation": "save", "objective": "用户纠正目标", "expected_revision": goal.revision}
    saved = execute_agent_goal_control(agent, scope=scope, payload=request)
    assert saved["goal"]["tokensUsed"] == 100
    assert saved["goal"]["revision"] == goal.revision + 1
    assert len(store.guidance.pending("agent_run", child.id)) == 1
    with pytest.raises(AgentControlError, match="草稿已保留"):
        execute_agent_goal_control(agent, scope=scope, payload={**request, "objective": "过期草稿"})
    assert len(store.guidance.pending("agent_run", child.id)) == 1
    paused = store.goals.update({"thread_id": goal.thread_id, "goal_id": goal.goal_id, "status": "paused"})
    saved = execute_agent_goal_control(agent, scope=scope, payload={**request, "expected_revision": paused.revision})
    assert saved["goal"]["status"] == "paused"
    assert not store.wakes.pending()
    assert store.goals.load(goal.thread_id).task_id == child.id
    assert read_agent_view(agent, scope=scope, run_id=child.id)["goals"][0]["revision"] == saved["goal"]["revision"]


def test_goal_editor_rejects_other_session_and_other_goal(tmp_path):
    agent, scope, child = _bound_agent_tree(tmp_path)
    store = agent.conversation_store
    goal = store.goals.create({"thread_id": child.agent_thread_id, "task_id": child.id, "objective": "私有目标"})
    with pytest.raises(AgentControlError):
        execute_agent_goal_control(agent, scope=SimpleNamespace(channel="chat", user_id="stranger", conversation_id="other"),
                                   payload={"run_id": child.id, "goal_id": goal.goal_id})
    with pytest.raises(AgentControlError):
        execute_agent_goal_control(agent, scope=scope, payload={"goal_id": goal.goal_id})


def test_child_goal_tools_and_accounting_do_not_use_parent_goal(tmp_path):
    agent, scope, child = _bound_agent_tree(tmp_path)
    store = agent.conversation_store
    root, _ = store.threads.resolve_report(channel=scope.channel, channel_user_id=scope.user_id, channel_conversation_id=scope.conversation_id)
    parent_goal = store.goals.create({"thread_id": root.thread_id, "task_id": child.root_id, "objective": "父级目标"})
    attrs = {"conversation_thread_id": root.thread_id, "conversation_task_id": child.root_id,
             "thread_goal_id": parent_goal.goal_id, "agent_thread_id": child.agent_thread_id}
    agent._current_run_params = RunParams(task_attributes=attrs, context_scope="task_local")
    previous = set_current_subagent_context(agent, run_id=child.id, task_attributes=attrs)
    try:
        made = agent.tools.tools["create_goal"].execute({"objective": "直属目标"})
        assert made.ok, made.output
        goal = store.goals.load(child.agent_thread_id, task_id=child.id)
        response = ModelResponse(text="", backend="fake", usage={"prompt_tokens": 23, "completion_tokens": 7})
        account_goal_model_response(agent, agent._current_run_params, response)
        assert store.goals.load(root.thread_id).tokens_used == 0
        assert store.goals.load(child.agent_thread_id).tokens_used == 30
        assert agent.tools.tools["update_goal"].execute({"status": "complete"}).ok
        assert store.goals.load(root.thread_id).status == "active"
        assert store.goals.load(child.agent_thread_id).goal_id == goal.goal_id
    finally:
        restore_current_subagent_context(agent, previous)


def test_parent_can_revise_direct_child_goal_without_changing_its_own(tmp_path):
    agent, scope, child = _bound_agent_tree(tmp_path)
    store = agent.conversation_store
    root, _ = store.threads.resolve_report(channel=scope.channel, channel_user_id=scope.user_id, channel_conversation_id=scope.conversation_id)
    parent = store.goals.create({"thread_id": root.thread_id, "task_id": child.root_id, "objective": "父级目标"})
    goal = store.goals.create({"thread_id": child.agent_thread_id, "task_id": child.id, "objective": "原子目标"})
    agent._current_run_params = RunParams(task_attributes={"conversation_thread_id": root.thread_id, "conversation_task_id": child.root_id})
    result = agent.tools.tools["update_goal"].execute({"target_run_id": child.id, "objective": "改为新的子目标", "expected_revision": goal.revision})
    assert result.ok, result.output
    assert store.goals.load(root.thread_id).objective == parent.objective
    assert store.goals.load(child.agent_thread_id).objective == "改为新的子目标"
    assert store.guidance.pending("agent_run", child.id)


def test_model_cannot_complete_an_objective_edited_since_its_request(tmp_path):
    agent, thread, goal = _goal_agent(tmp_path)
    current_goal_scope_prompt(agent, agent._current_run_params)
    agent.conversation_store.goals.update({"thread_id": thread.thread_id, "goal_id": goal.goal_id,
                                           "objective": "刚补充的完整新要求"})
    stale = agent.tools.tools["update_goal"].execute({"status": "complete"})
    assert not stale.ok and stale.error_code == "GOAL_STATE_CONFLICT"
    assert agent.conversation_store.goals.load(thread.thread_id).status == "active"
    current_goal_scope_prompt(agent, agent._current_run_params)
    assert agent.tools.tools["update_goal"].execute({"status": "complete"}).ok


def test_stop_child_pauses_only_its_own_goal(tmp_path):
    from agent_py_agent.agent.conversation.agent_control import stop_agent

    agent, scope, child = _bound_agent_tree(tmp_path)
    goal = agent.conversation_store.goals.create({"thread_id": child.agent_thread_id,
                                                "task_id": child.id, "objective": "子代理的长目标"})
    stopped = stop_agent(agent, scope=scope, run_id=child.id, operation_id="stop-child-goal")
    assert stopped["ok"] is True
    assert agent.conversation_store.goals.load(goal.thread_id).status == "paused"


def test_child_goal_does_not_grant_sibling_edit_authority(tmp_path):
    agent, _scope, child = _bound_agent_tree(tmp_path)
    sibling = agent.subagents.create_run(goal="兄弟任务", root_id=child.root_id, parent_id=child.parent_id)
    goal = agent.conversation_store.goals.create({"thread_id": sibling.agent_thread_id,
                                                "task_id": sibling.id, "objective": "兄弟目标"})
    attrs = {"agent_thread_id": child.agent_thread_id}
    agent._current_run_params = RunParams(task_attributes=attrs, context_scope="task_local")
    previous = set_current_subagent_context(agent, run_id=child.id, task_attributes=attrs)
    try:
        denied = agent.tools.tools["update_goal"].execute({"target_run_id": sibling.id,
                    "objective": "不该被改", "expected_revision": goal.revision})
        assert not denied.ok
        assert agent.conversation_store.goals.load(goal.thread_id).objective == "兄弟目标"
    finally:
        restore_current_subagent_context(agent, previous)


def test_saved_goal_guidance_reaches_its_agent_and_can_cross_provider_boundary(tmp_path):
    from agent_py_agent.agent.agent_core.runtime.guidance import (
        acknowledge_injected_turn_input,
        has_pending_request_guidance,
        inject_pending_guidance,
        mark_injected_turn_input_submitted,
    )

    agent, scope, child = _bound_agent_tree(tmp_path)
    store = agent.conversation_store
    root = store.threads.resolve(channel=scope.channel, channel_user_id=scope.user_id, channel_conversation_id=scope.conversation_id)
    goal = store.goals.create({"thread_id": root.thread_id, "task_id": child.root_id, "objective": "父级原目标"})
    execute_agent_goal_control(agent, scope=scope, payload={"operation": "save", "goal_id": goal.goal_id,
                               "objective": "仅属于父级的新要求", "expected_revision": goal.revision})
    child_params = _tool_loop_params(context_scope="task_local", run_id=child.id, task_id=child.root_id,
                    request_id="child-attempt", attempt_id="child-attempt", task_attributes={"agent_thread_id": child.agent_thread_id})
    assert has_pending_request_guidance(agent, child_params) is False
    assert inject_pending_guidance(agent, child_params) is False
    assert store.guidance.pending("task", child.root_id)
    main_params = _tool_loop_params(run_id=child.root_id, task_id=child.root_id, request_id="main-turn", attempt_id="main-attempt")
    assert inject_pending_guidance(agent, main_params)
    assert mark_injected_turn_input_submitted(agent, main_params, provider_call_id="main-call") == 1
    assert acknowledge_injected_turn_input(agent, main_params) == 1
    assert not store.guidance.pending("task", child.root_id)
    child_goal = store.goals.create({"thread_id": child.agent_thread_id, "task_id": child.id, "objective": "子级原目标"})
    execute_agent_goal_control(agent, scope=scope, payload={"operation": "save", "run_id": child.id,
                "goal_id": child_goal.goal_id, "objective": "仅属于子级的新要求", "expected_revision": child_goal.revision})
    assert has_pending_request_guidance(agent, main_params) is False
    assert has_pending_request_guidance(agent, child_params) is True
    assert inject_pending_guidance(agent, child_params)
    assert mark_injected_turn_input_submitted(agent, child_params, provider_call_id="child-call") == 1
    assert acknowledge_injected_turn_input(agent, child_params) == 1
    assert not store.guidance.pending("agent_run", child.id)


@pytest.mark.parametrize("overflow_after_first_goal_turn", [False, True])
def test_child_goal_continues_in_same_run_with_distinct_history_turns(tmp_path, overflow_after_first_goal_turn):
    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path)
    task = agent.subagents.create_run(goal="处理两段工作", role="worker", attributes={"persistent_goal": "完成两段工作并总结"})

    class Backend(_OverflowThenCompleteChildBackend):
        def __init__(self):
            super().__init__(overflow_once=False)
            self.calls = 0

        def generate(self, prompt, on_chunk=None, **kwargs):
            if "You maintain a conversation summary" in prompt:
                return super().generate(prompt, on_chunk, **kwargs)
            self.calls += 1
            assert self.calls <= 3 + int(overflow_after_first_goal_turn), "Goal must stop after explicit completion"
            if self.calls == 1:
                return ModelResponse(text="第一段已处理，后续继续。", backend=self.name)
            if self.calls == 2 and overflow_after_first_goal_turn:
                return ModelResponse(text="上下文压力", backend=self.name, runtime_status="context_overflow",
                                     runtime_reason="context_overflow", runtime_source="provider_error")
            if self.calls == 2 + int(overflow_after_first_goal_turn):
                return ModelResponse(text="", backend=self.name, tool_use_blocks=[{"id": "finish-goal", "name": "update_goal", "input": {"status": "complete"}}])
            return ModelResponse(text="两段工作都处理完了。", backend=self.name)

    backend = Backend()
    agent.backend = backend
    result = agent.run_subagent(task.id, dry_run=False, probe=False)
    assert result.ok, result.message
    assert backend.calls == 3 + int(overflow_after_first_goal_turn)
    assert bool(backend.summary_prompts) is overflow_after_first_goal_turn
    goal = agent.conversation_store.goals.load(task.agent_thread_id)
    assert goal.status == "complete"
    assert goal.task_id == task.id
    rows = agent.conversation_store.messages.recent(task.agent_thread_id, limit=20)
    finals = [r for r in rows if r.role == "assistant" and r.metadata.get("assistant_part_id") == "final"]
    assert len(finals) == 2
    assert len({r.metadata["conversation_request_id"] for r in finals}) == 2
    assert len({r.metadata["agent_attempt_id"] for r in finals}) == 1
    assert agent.subagents.load(task.id).status == "DONE"


@pytest.mark.parametrize("reason", ["blocked", "error", "max-tokens", "interrupted", "aborted"])
def test_goal_never_retries_non_normal_child_end(tmp_path, reason):
    agent, _scope, child = _bound_agent_tree(tmp_path)
    agent.conversation_store.goals.create({"thread_id": child.agent_thread_id, "task_id": child.id, "objective": "持续目标"})
    assert active_delegated_goal_after_turn(agent, child, SimpleNamespace(turn_end_reason=reason)) is None


def test_paused_child_goal_does_not_relabel_completed_turn_as_interrupted(tmp_path):
    agent, _scope, child = _bound_agent_tree(tmp_path)
    goal = agent.conversation_store.goals.create({
        "thread_id": child.agent_thread_id, "task_id": child.id, "objective": "持续目标",
    })
    agent.conversation_store.goals.update({
        "thread_id": child.agent_thread_id, "goal_id": goal.goal_id, "status": "paused",
    })
    result = SimpleNamespace(turn_end_reason="completed", runtime_status="ok", runtime_reason="")
    assert active_delegated_goal_after_turn(agent, child, result) is None
    assert vars(result) == {"turn_end_reason": "completed", "runtime_status": "ok", "runtime_reason": ""}


@pytest.mark.parametrize("boundary", ["goal", "goal_then_compact"])
def test_child_continuation_keeps_exact_denials_and_isolates_other_children(tmp_path, monkeypatch, boundary):
    from agent_py_agent.agent.conversation.background_transcript import BackgroundTranscriptSink

    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path)
    prompts = []

    # LLM: 仅替换用户审批输入；真实子代理生命周期、工具权限和 Goal/Compact 续接照常执行。
    # 函数用途: 拒绝每次实际到达审批消费者的请求，以计数证明同参拒绝没有因续轮遗忘。
    def deny(_sink, payload, *, cancellation_token=None):
        prompts.append(payload)
        return {"permission_id": payload["permission_id"], "decision": "denied"}

    monkeypatch.setattr(BackgroundTranscriptSink, "request_permission", deny)

    # LLM: 只控制模型回复和压缩摘要，不代替权限裁决或修改运行参数。
    # 类用途: 稳定复现拒绝后跨 Goal 或 Compact 再请求同一工具，再尝试不同参数。
    class Backend(_OverflowThenCompleteChildBackend):
        # LLM: 每个孩子有自己的回复游标，共享审批观察列表只用于断言隔离。
        # 函数用途: 初始化这一次子代理的确定性模型序列。
        def __init__(self):
            super().__init__(overflow_once=False)
            self.calls = 0

        # LLM: 模型可以重复请求，但是否再次询问用户必须由宿主保留的精确拒绝事实决定。
        # 函数用途: 按固定序列产生工具调用、自动续轮边界和显式目标结束。
        def generate(self, prompt, on_chunk=None, **kwargs):
            if "You maintain a conversation summary" in prompt:
                return super().generate(prompt, on_chunk, **kwargs)
            self.calls += 1
            compact = boundary == "goal_then_compact"
            assert self.calls <= 6 + int(compact)
            if compact and self.calls == 3:
                return ModelResponse(text="上下文压力", backend=self.name, runtime_status="context_overflow",
                                     runtime_reason="context_overflow", runtime_source="provider_error")
            stage = self.calls - int(compact and self.calls > 3)
            if stage == 2:
                return ModelResponse(text="当前操作被拒绝。", backend=self.name)
            if stage in {1, 3, 4}:
                command = "printf changed-fixture" if stage == 4 else "printf denied-fixture"
                return ModelResponse(text="", backend=self.name, tool_use_blocks=[{
                    "id": f"request-{self.calls}", "name": "run_command",
                    "input": {"command": command, "run_in_background": True},
                }])
            if stage == 5:
                return ModelResponse(text="", backend=self.name, tool_use_blocks=[{
                    "id": "finish-goal", "name": "update_goal", "input": {"status": "complete"},
                }])
            return ModelResponse(text="已记录拒绝，未执行命令。", backend=self.name)

    for child_number in range(2):
        task = agent.subagents.create_run(goal="核对拒绝边界", role="worker",
                                         attributes={"persistent_goal": "检查两个工具参数的拒绝结果"})
        backend = Backend()
        agent.backend = backend
        result = agent.run_subagent(task.id, dry_run=False, probe=False)
        assert result.ok, result.message
        assert backend.calls == 6 + int(boundary == "goal_then_compact")
        assert len(prompts) == (child_number + 1) * 2
        current = prompts[-2:]
        assert {p["binding"]["run_id"] for p in current} == {task.id}
        assert current[0]["binding"]["args_hash"] != current[1]["binding"]["args_hash"]
        assert bool(backend.summary_prompts) is (boundary == "goal_then_compact")
        index = Path(task.agent_run_workspace_dir) / "blobs/tool_outputs/index.jsonl"
        commands = [row for line in index.read_text().splitlines()
                    if (row := json.loads(line))["tool"] == "run_command"]
        assert len(commands) == 3
        assert all(row["error_code"] == "APPROVAL_REJECTED" for row in commands)
        assert all(row["tool_execution"]["handler_executed"] is False for row in commands)
