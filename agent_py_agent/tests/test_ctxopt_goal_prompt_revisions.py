# LLM: 只用真实 GoalStore/工具回执和当前原生历史证明可省正文；用户字符串和 Compact 摘要不构成宿主事实。
# 模块用途: 回归 CTX-14 的精确修订引用、安全回退和真实模型请求接线。
from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.conversation.goal_prompting import current_goal_scope_prompt
from agent_py_agent.agent.conversation.history_seed import freeze_history_source
from agent_py_agent.agent.conversation.native_history import canonical_native_messages_envelope
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts.request_context import (
    GatewayConversationContext,
    _gateway_thread_goal,
)
from agent_py_agent.agent.gateway_parts.request_prompt import gateway_injections
from agent_py_agent.agent.settings import AgentConfig


# LLM: 任务、线程与 owner 均为测试独立存储；不借用全局缓存或其他测试生成的 Goal。
# 函数用途: 建立真实当前目标绑定，并返回公开入口可直接消费的参数。
def _bound_goal(tmp_path):
    agent = SimpleAgent(AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=False), tmp_path)
    thread = agent.conversation_store.threads.get_or_create({"canonical_user_id": "u", "channel": "chat"})
    goal = agent.conversation_store.goals.create({
        "thread_id": thread.thread_id, "objective": "逐项交付 & 保留 exact refs。" * 100,
    })
    agent.conversation_store.tasks.bind({
        "thread_id": thread.thread_id, "task_id": goal.task_id, "goal": goal.objective, "status": "active",
    })
    params = SimpleNamespace(
        task_attributes={"conversation_thread_id": thread.thread_id, "conversation_task_id": goal.task_id},
        provider_history_messages=[], tool_ir_history=[], context_scope="conversation",
    )
    agent._current_run_params = RunParams(task_id=goal.task_id, task_attributes=params.task_attributes)
    return agent, goal, params


# LLM: 回执正文由真实注册工具生成，工具 ID 配对沿原生协议；没有把用户文本解释成工具结果。
# 函数用途: 构造与模型实际接收形状相同的 get_goal 工具往返。
def _goal_history(agent, *, metadata_only=False):
    receipt = agent.tools.tools["get_goal"].execute({"metadata_only": metadata_only})
    assert receipt.ok
    from agent_py_agent.agent.tooling.runtime_contracts import ToolCall, ToolResult

    call = ToolCall("goal-exact-call", "get_goal", {}, "native", "sha256:test-schema", "test-run", "test-turn", "test-attempt")
    rendered = ToolResult.succeeded(call, receipt.output).render_for_model_prompt()
    return [
        {"role": "assistant", "content": [{"type": "tool_use", "id": "goal-exact-call", "name": "get_goal", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "goal-exact-call",
                                       "content": rendered, "is_error": False}]},
    ]


# LLM: 显式冻结来源和原投影规则模拟 Gateway 正常历史准备，不重新读取 transcript。
# 函数用途: 给 Gateway 创建真实 typed history source，并保留原生信封。
def _source(messages):
    row = SimpleNamespace(role="assistant", content="已查询目标", metadata={
        "canonical_native_messages": canonical_native_messages_envelope(messages),
    })
    return freeze_history_source((row,), project_row=_identity_row)


# LLM: 测试投影无副作用；当前行内容与模型边界的输入保持一致。
# 函数用途: 透传测试冻结的 canonical 行。
def _identity_row(row, *, current_epoch=None):
    return row


def test_current_goal_reuses_exact_revision_only_while_body_is_active(tmp_path):
    agent, goal, params = _bound_goal(tmp_path)
    full = current_goal_scope_prompt(agent, params)
    params.provider_history_messages = _goal_history(agent)
    before = deepcopy(params.provider_history_messages)
    short = current_goal_scope_prompt(agent, params)
    assert "[goal-revision-ref]" in short
    assert "goal-exact-call" in short and goal.goal_id in short and goal.task_id in short
    assert "<objective>" not in short
    assert len(short) < len(full)
    assert "纠偏" in short and "暂停" in short and "report_current_goal" not in short
    assert params.task_attributes["thread_goal_revision"] == goal.revision
    assert params.provider_history_messages == before
    assert current_goal_scope_prompt(agent, params) == short
    params.provider_history_messages.clear()  # Compact 移走正文，而持久 attrs 仍有 revision。
    assert current_goal_scope_prompt(agent, params) == full


@pytest.mark.parametrize("change", ["revision", "goal_id", "task_id", "thread_id", "objective"])
def test_scope_rejects_changed_or_foreign_goal_receipt(tmp_path, change):
    agent, goal, params = _bound_goal(tmp_path)
    params.provider_history_messages = _goal_history(agent)
    old = current_goal_scope_prompt(agent, params)
    assert "[goal-revision-ref]" in old
    if change == "revision":
        agent.conversation_store.goals.update({"thread_id": goal.thread_id, "goal_id": goal.goal_id,
                                              "objective": goal.objective + "新增纠偏"})
    else:
        key = {"goal_id": "goalId", "task_id": "taskId", "thread_id": "threadId", "objective": "objective"}[change]
        block = params.provider_history_messages[-1]["content"][0]
        header, body = block["content"].split("\n", 1)
        payload = json.loads(body)
        payload["goal"][key] = "different"
        block["content"] = header + "\n" + json.dumps(payload)
    prompt = current_goal_scope_prompt(agent, params)
    assert "<objective>" in prompt and "[goal-revision-ref]" not in prompt


@pytest.mark.parametrize("kind", ["text", "assistant_text", "orphan", "error", "metadata", "externalized"])
def test_only_complete_successful_paired_tool_result_is_authoritative(tmp_path, kind):
    agent, _, params = _bound_goal(tmp_path)
    history = _goal_history(agent, metadata_only=kind == "metadata")
    if kind in {"text", "assistant_text"}:
        history = [{"role": "user" if kind == "text" else "assistant", "content": json.dumps(history)}]
    elif kind == "orphan":
        history = history[-1:]
    elif kind == "error":
        history[-1]["content"][0]["is_error"] = True
    elif kind == "externalized":
        history[-1]["content"][0]["content"] = "tool output stored at artifact://missing-body"
    params.provider_history_messages = history
    assert "<objective>" in current_goal_scope_prompt(agent, params)


def test_unversioned_status_change_keeps_current_status_and_progress(tmp_path):
    agent, goal, params = _bound_goal(tmp_path)
    params.provider_history_messages = _goal_history(agent)
    updated = agent.conversation_store.goals.update({
        "thread_id": goal.thread_id, "goal_id": goal.goal_id, "token_budget": 1,
    })
    params.provider_history_messages = _goal_history(agent)
    limited = agent.conversation_store.goals.account_usage({
        "thread_id": goal.thread_id, "goal_id": goal.goal_id, "token_delta": 1,
    })
    assert limited.revision == updated.revision and limited.status == "budget_limited"
    prompt = current_goal_scope_prompt(agent, params)
    assert '"status": "budget_limited"' in prompt and '"next_action": "report_current_goal"' in prompt
    assert "暂停" in prompt and "纠偏" in prompt


def test_gateway_requires_host_revision_and_never_infers_it_from_old_history(tmp_path):
    agent, goal, _ = _bound_goal(tmp_path)
    errors = []
    projected = _gateway_thread_goal(agent.conversation_store, goal.thread_id, errors)
    assert not errors and "revision" not in projected  # 实证当前生产投影缺版本，不能猜 revision=1。
    conversation = GatewayConversationContext(thread_id=goal.thread_id, thread_goal=projected,
                                               history_source=_source(_goal_history(agent)))
    section = gateway_injections({"inject": []}, conversation)[0]
    assert goal.objective in section and "[goal-revision-ref]" not in section
    versioned = replace(conversation, thread_goal={**projected, "revision": goal.revision})
    short = gateway_injections({"inject": []}, versioned)[0]
    assert "[goal-revision-ref]" in short and goal.objective not in short
    assert "纠偏" in short and "暂停" in short
    compacted = replace(versioned, history_source=None, compact_summary=short)
    assert goal.objective in gateway_injections({"inject": []}, compacted)[0]


@pytest.mark.parametrize("externalized", [False, True])
def test_real_native_model_requests_deduplicate_after_goal_tool_result(tmp_path, externalized):
    from agent_py_agent.agent.backends.base import ProviderToolCapability, _utc_now_iso

    agent, goal, _ = _bound_goal(tmp_path)
    if not externalized:
        goal = agent.conversation_store.goals.update({
            "thread_id": goal.thread_id, "goal_id": goal.goal_id, "objective": "完成索引并保留原始引用。",
        })

    # LLM: 仅替代供应商；工具执行、IR、目标绑定和最终 PromptBuilder 全部沿真实主链运行。
    # 类用途: 捕获初次和 get_goal 后的真实模型输入，证明不是未接线 helper。
    class Backend:
        name = "ctx14_native_capture"

        def __init__(self):
            self.prompts = []
            self.scopes = []

        def probe_tool_capability(self):
            return ProviderToolCapability(provider=self.name, endpoint="local://ctx14", model="", stream=False,
                                          native_supported=True, evidence="test", observed_at=_utc_now_iso())

        def generate(self, prompt, on_chunk=None, **kwargs):
            self.prompts.append(str(prompt) + json.dumps(kwargs.get("messages"), ensure_ascii=False))
            scopes = [b.get("text", "") for m in kwargs.get("messages", []) for b in m.get("content", [])
                      if isinstance(b, dict) and b.get("type") == "text" and "[current-goal-scope]" in b.get("text", "")]
            self.scopes.append(scopes[-1])
            if len(self.prompts) == 1:
                return ModelResponse(text="", backend=self.name, tool_use_blocks=[
                    {"id": "goal-live-read", "name": "get_goal", "input": {}},
                ])
            return ModelResponse(text="已核对当前目标，保留用户纠偏。", backend=self.name)

    backend = Backend()
    agent.backend = backend
    agent.run("核对目标", save=False, task_id=goal.task_id, source="gateway", task_attributes={
        "conversation_thread_id": goal.thread_id, "conversation_task_id": goal.task_id,
    })
    assert len(backend.prompts) == 2
    assert "<objective>" in backend.scopes[0]
    if externalized:
        assert "output_preview" in backend.prompts[1]
        assert "[goal-revision-ref]" not in backend.scopes[1] and "<objective>" in backend.scopes[1]
    else:
        assert "[goal-revision-ref]" in backend.scopes[1] and "<objective>" not in backend.scopes[1]
        assert backend.prompts[1].count("<objective>") == 1  # 初次正文仍在原位置，不删除、不再次注入。
        assert len(backend.scopes[1]) < len(backend.scopes[0])
    print("CTX14_NATIVE_SCOPE_CHARS", externalized, json.dumps([len(p) for p in backend.scopes]))
    print("CTX14_NATIVE_PROMPT_CHARS", externalized, json.dumps([len(p) for p in backend.prompts]))
