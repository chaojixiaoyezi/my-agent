"""机械摘要必须先与持久检查点同口径，再进入真实恢复请求。"""
from __future__ import annotations

from collections import Counter

import pytest

from agent_py_agent.agent.agent_core.model.context_pressure import (
    projected_model_context_components,
)
from agent_py_agent.agent.agent_core.runtime.context_compactor import runtime_compact_policy
from agent_py_agent.agent.agent_core.tool_request_projection import ToolLoopRequestProjection
from agent_py_agent.agent.backends.base import ModelResponse, ProviderToolCapability, _utc_now_iso
from agent_py_agent.agent.conversation import compact
from agent_py_agent.agent.conversation.compact_checkpoint import iter_committed_compact_checkpoints
from agent_py_agent.agent.conversation.compact_projection import ConversationCompactProjection
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig


@pytest.mark.parametrize("summary_mode", ["tool_call", "empty"])
def test_child_goal_recovers_with_canonical_mechanical_summary(tmp_path, summary_mode):
    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path)
    task = agent.subagents.create_run(
        goal="处理两段工作", role="worker", attributes={"persistent_goal": "完成两段工作并总结"},
    )
    # 摘要调用会浅拷贝后端；共享计数和请求列表不能依赖后端自身的整数属性。
    calls = Counter()
    summary_requests = []
    resumed_messages = []

    class Backend:
        name = "mechanical-summary-recovery"
        context_window_tokens = 128_000

        def probe_tool_capability(self):
            return ProviderToolCapability(
                provider=self.name, endpoint="local://compact-regression", model="", stream=False,
                native_supported=True, evidence="test_backend_declares_native_tools", observed_at=_utc_now_iso(),
            )

        def generate(self, prompt, on_chunk=None, **kwargs):
            if "Direct factual archival only: maintain a concise summary" in prompt:
                calls["summary"] += 1
                summary_requests.append(kwargs)
                return ModelResponse(text="", backend=self.name, tool_use_blocks=(
                    [{"id": "s1", "name": "update_goal", "input": {"status": "complete"}}]
                    if summary_mode == "tool_call" else []
                ))
            calls["turn"] += 1
            assert calls["turn"] <= 4, "显式完成目标后不得再续跑"
            if calls["turn"] == 1:
                return ModelResponse(text="第一段已处理，后续继续。", backend=self.name)
            if calls["turn"] == 2:
                return ModelResponse(
                    text="上下文压力", backend=self.name, runtime_status="context_overflow",
                    runtime_reason="context_overflow", runtime_source="provider_error",
                )
            if calls["turn"] == 3:
                # 摘要中的工具调用不能执行；恢复后的业务轮才有权完成目标。
                assert agent.conversation_store.goals.load(task.agent_thread_id).status == "active"
                resumed_messages.extend(kwargs["messages"])
                return ModelResponse(text="", backend=self.name, tool_use_blocks=[
                    {"id": "finish-goal", "name": "update_goal", "input": {"status": "complete"}},
                ])
            return ModelResponse(text="两段工作都处理完了。", backend=self.name)

    agent.backend = Backend()
    result = agent.run_subagent(task.id, dry_run=False, probe=False)

    assert result.ok is True, result.message
    assert result.status == "DONE"
    assert calls == {"turn": 4, "summary": 2}
    assert summary_requests[0]["tools"] == summary_requests[1]["tools"]
    assert summary_requests[0]["tool_choice"] == summary_requests[1]["tool_choice"]
    committed = agent.conversation_store.threads.require(task.agent_thread_id)
    assert committed.compact_generation == 1
    assert committed.compact_checkpoint_id
    checkpoints = list(iter_committed_compact_checkpoints(agent, committed))
    assert len(checkpoints) == 1
    # select 已经过真实候选/提交等值校验；再读真实账本核对实际续跑使用的正文。
    assert committed.summary == checkpoints[0]["summary"] == checkpoints[0]["summary"].strip()
    resumed_texts = [
        block["text"]
        for message in resumed_messages
        for block in ([{"text": message["content"]}] if isinstance(message["content"], str) else message["content"])
        if isinstance(block, dict) and isinstance(block.get("text"), str)
    ]
    assert any(committed.summary in text for text in resumed_texts)
    assert "第一段已处理，后续继续。" in committed.summary
    assert agent.conversation_store.goals.load(task.agent_thread_id).status == "complete"
    assert agent.subagents.load(task.id).status == "DONE"


@pytest.mark.parametrize("summary", ["摘要正文\n\n", " \n  摘要第一行\n摘要第二行。\n\n", "无需裁剪的摘要"])
def test_measure_candidate_projects_checkpoint_canonical_text(tmp_path, summary):
    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path)
    thread = agent.conversation_store.threads.get_or_create({"channel": "chat", "channel_conversation_id": "measure"})
    views = []
    projections = []

    def project(view):
        views.append(view)
        request = ToolLoopRequestProjection(
            status="ready", provider_prompt="完整请求", system_instruction="固定系统指令", tools=[],
            messages=[{"role": "user", "content": view.summary}],
        )
        tokens, _ = projected_model_context_components(request)
        projection = ConversationCompactProjection(tokens, request)
        projections.append(projection)
        return projection

    request = compact._CompactRunRequest(
        agent=agent, store=agent.conversation_store, thread=thread, current_prompt="", pending=(),
        policy=runtime_compact_policy(agent, thread=thread), projected_tokens=100, forced=True,
        attempted_at=0.0, operation_id="measure-canonical-summary", request_projector=project,
    )
    measured = compact._measure_candidate(request, compact._CandidateTail((), {}), summary)

    assert len(views) == 1
    assert views[0].summary == measured.summary == summary.strip()
    assert views[0].is_candidate is True
    assert views[0].compact_generation == thread.compact_generation + 1
    assert measured.projection is projections[0]
    assert measured.projection.material.messages == [{"role": "user", "content": summary.strip()}]
    assert measured.tokens == projections[0].projected_tokens
