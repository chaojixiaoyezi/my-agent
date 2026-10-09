# LLM: 仅传输和文件工具用合成替身，真实回合/检查点/存储必须执行，以捕获跨回合而非单次投影失配。
# 模块用途: 验证密文与文字中途压缩的两回合前缀延伸、后续压缩同源及工具配对。
"""两次真实工具循环、临时 Store/checkpoint；仅替换模型传输和文件工具 handler。"""
from copy import deepcopy

import pytest

from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.backends.base import ProviderToolCapability
from agent_py_agent.agent.conversation.authority import CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR
from agent_py_agent.agent.conversation.compact_scope import THREAD_COMPACT_SCOPE
from agent_py_agent.agent.conversation.compact_summary_view import (
    AppliedCompactContext,
    resolve_compact_summary_view,
)
from agent_py_agent.agent.conversation.models import ConversationHistorySeed
from agent_py_agent.agent.conversation.native_history import (
    canonical_native_messages_envelope,
    provider_history_messages_from_rows,
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.tooling.models import ToolHandlerOutcome
from agent_py_agent.tests.test_compact_remote_provider import BLOCK, _subscription_backend


# LLM: 只输出结构类型，不把模拟正文当分类依据。
# 函数用途: 返回首个分叉的条目类型。
def _kind(item):
    return item.get("type", "message") + ":" + item.get("role", "")


# LLM: 每例独立临时Store，不连接真实后端；旧历史须经过真实native重放。
# 函数用途: 准备合成后端、线程与上一轮历史。
def _prepare_run(tmp_path, remote):
    backend, config, _ = _subscription_backend()
    config.my_agent_home = str(tmp_path / "home")
    config.model_context_window_tokens = 128_000
    config.max_tokens = 64
    config.memory_compact_remote_enabled = remote
    config.tool_output_externalize_min_chars = 10_000_000
    config.tool_output_externalize_on_low_headroom = False
    config.prompt_files = []
    config.config_sources = {"model_context_window_tokens": {"source": "test"}}
    agent = SimpleAgent(config, tmp_path)
    agent.backend = backend
    backend.probe_tool_capability = lambda: ProviderToolCapability(
        provider=backend.name, endpoint="local://fake", model="fake", stream=False,
        native_supported=True, evidence="fake_transport", observed_at="2026-10-08T00:00:00Z",
    )
    store = agent.conversation_store
    thread = store.threads.get_or_create({"channel": "test", "channel_conversation_id": "midturn"})
    old_messages = []
    for role, content in [("user", "保留的旧要求"), ("assistant", "保留的旧答复")]:
        old_messages.append(store.messages.append({"thread_id": thread.thread_id, "role": role, "content": content,
                                                  "metadata": {"conversation_request_id": "old"}}))
    prior = provider_history_messages_from_rows(old_messages)
    return agent, thread, prior


# LLM: 辅助调用用结构化调用边界区分；业务模型持续产出大工具结果，直到真实CAS推进generation。
# 函数用途: 替换模型传输和工具handler，记录所有业务与压缩请求。
def _install_fake_io(agent, thread, monkeypatch, remote):
    store = agent.conversation_store
    backend = agent.backend
    business, summaries, executed = [], [], []

    # LLM: 大结果触发原生压缩，不直接调用压缩实现。
    # 函数用途: 返回可重复的合成大文件。
    def fake_read(_params):
        executed.append(1)
        return ToolHandlerOutcome(tool="read_file", ok=True, output="x" * 140_000)

    monkeypatch.setattr(agent.tools.tools["read_file"], "execute", fake_read)
    summary = "[compact-live-handoff.v1]\ncurrent_progress: 已读取\nuser_constraints: 保留要求\ncompleted: 文件\nfailures: 无\nunresolved: 继续\nnext_step: 核对"
    from agent_py_agent.agent.conversation import compact_request_budget
    generate_auxiliary = compact_request_budget.generate_auxiliary_model_response
    auxiliary = False
    # LLM: 包裹真实辅助调用，仅记录用途边界，不替换摘要预算流程。
    # 函数用途: 标记辅助模型运输的动态范围。
    def generate_summary(request):
        nonlocal auxiliary
        auxiliary = True
        try:
            return generate_auxiliary(request)
        finally:
            auxiliary = False
    monkeypatch.setattr(compact_request_budget, "generate_auxiliary_model_response", generate_summary)

    # LLM: 供应商响应保持真实协议形状；路由只读触发项与辅助调用状态。
    # 函数用途: 捕获JSON请求并返回模拟工具调用、摘要或完成消息。
    def send(_path, payload, _headers, **_options):
        compact = any(item.get("type") == "compaction_trigger" for item in payload.get("input", []))
        if compact or auxiliary:
            summaries.append(deepcopy(payload))
            return {"status": "completed", "output": [BLOCK["item"]] if remote else [
                {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": summary}]}]}
        business.append(deepcopy(payload))
        if store.threads.require(thread.thread_id).compact_generation == 0 and len(business) <= 8:
            return {"status": "completed", "output": [{"type": "function_call", "call_id": f"read-{len(business)}",
                "name": "read_file", "arguments": '{"path":"fake.txt"}'}]}
        return {"status": "completed", "output": [{"type": "message", "role": "assistant",
                "content": [{"type": "output_text", "text": "完成"}]}]}

    backend.request_json = send
    return business, summaries, executed


# LLM: 两轮都走公开agent.run，宿主存储动作只在本例临时Store模拟，不能短路恢复与提交。
# 函数用途: 比较本轮中途压缩的末请求与下一轮首请求的完整前缀。
@pytest.mark.parametrize("remote", [True, False], ids=["responses_compaction", "text_summary"])
def test_two_real_turns_extend_post_compact_provider_prefix(tmp_path, monkeypatch, remote):
    agent, thread, prior = _prepare_run(tmp_path, remote)
    store = agent.conversation_store
    business, summaries, executed = _install_fake_io(agent, thread, monkeypatch, remote)
    # LLM: 存储宿主需把canonical结果写入真实行，下一轮由公开重放函数读取。
    # 函数用途: 执行一轮并持久合成输入与结果。
    def run_turn(number, seed, context):
        request = f"request-{number}"
        text = f"本轮要求-{number}"
        store.messages.append({"thread_id": thread.thread_id, "role": "user", "content": text,
                               "metadata": {"conversation_request_id": request}})
        result = agent.run(text, params=RunParams(save=False, allowed_tools=["read_file"], context_scope="conversation",
            request_id=request, run_id=request, task_attributes={CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR: True,
                "conversation_thread_id": thread.thread_id}, conversation_history_seed=seed, compact_context=context))
        store.messages.append({"thread_id": thread.thread_id, "role": "assistant", "content": result.response,
            "metadata": {"conversation_request_id": request,
                         "canonical_native_messages": canonical_native_messages_envelope(result.canonical_native_messages)}})
        return result

    first = run_turn(1, ConversationHistorySeed(canonical_messages=prior),
                     AppliedCompactContext(thread.thread_id, THREAD_COMPACT_SCOPE, resolve_compact_summary_view(agent, thread, THREAD_COMPACT_SCOPE)))
    current = store.threads.require(thread.thread_id)
    assert first.response == "完成" and current.compact_generation == 1 and executed and summaries, (
        first.response, current.compact_generation, len(executed), len(summaries), len(business),
        [len(str(row["input"])) for row in business],
        [str(item.get("output", ""))[:300] for item in business[-1]["input"] if item.get("type") == "function_call_output"])
    last = business[-1]
    view = resolve_compact_summary_view(agent, current, THREAD_COMPACT_SCOPE)
    rows = store.messages.recent(thread.thread_id, limit=0)
    seed = ConversationHistorySeed(compact_summary=view.summary, compact_generation=view.generation,
                                   canonical_messages=provider_history_messages_from_rows(rows))
    count = len(business)
    second = run_turn(2, seed, AppliedCompactContext(thread.thread_id, THREAD_COMPACT_SCOPE, view))
    assert second.response == "完成" and len(business) == count + 1
    before, after = last["input"], business[count]["input"]
    mismatch = next((i for i, (a, b) in enumerate(zip(before, after)) if a != b), min(len(before), len(after)))
    print("first_divergence", mismatch, _kind(before[mismatch]) if mismatch < len(before) else "end",
          _kind(after[mismatch]) if mismatch < len(after) else "end")
    # 本例不合法丢弃任何已发送条目，连同状态/保留工具尾部全部保持到第一轮请求末尾。
    assert after[:len(before)] == before


# LLM: 第二次压缩也必须复用主请求的顺序；同代applied摘要不应挪动独立handoff、推理或工具对。
# 函数用途: 核验辅助请求同源投影且不改输入。
@pytest.mark.parametrize("remote", [True, False])
def test_next_compact_uses_same_layout_without_moving_handoff_or_pairs(remote):
    from types import SimpleNamespace

    from agent_py_agent.agent.agent_core.tool_ir_history import (
        applied_compact_summary_item,
        project_native_provider_messages,
    )
    from agent_py_agent.agent.backends.base import ModelResponse
    from agent_py_agent.agent.backends.tool_ir import AssistantTurn, CompactionSummary, UserTurn
    from agent_py_agent.agent.memory_archive.compact_semantic_summary import (
        LiveToolHistorySummaryRequest,
        summarize_live_tool_history,
    )
    from agent_py_agent.agent.prompting_parts.cache_layout import CacheStructuredPrompt
    from agent_py_agent.tests._tool_runtime_harness import (
        canonical_history_call,
        canonical_history_result,
    )
    from agent_py_agent.tests.test_compact_remote_provider import CIPHER
    from agent_py_agent.tests.test_native_tool_ir_compact_and_orphan_sweep import (
        _valid_live_handoff,
    )
    call = canonical_history_call("read_file", {"path": "fake"}, call_id="retained")
    reasoning = {"type": "responses_reasoning", "model": "fake", "item": {"type": "reasoning", "encrypted_content": "thought", "summary": []}}
    history = [UserTurn("current"), applied_compact_summary_item("summary", 1, CIPHER if remote else ""),
               CompactionSummary("handoff", source="carried_tool_handoff"),
               AssistantTurn("reading", tool_calls=[call], content_blocks=[reasoning]), canonical_history_result(call, "result")]
    prior = [{"role": "user", "content": "prior"}]
    original = deepcopy(history)
    expected = project_native_provider_messages(history, prior_messages=prior)
    sent = []
    # LLM: 捕获真实摘要流程的出站参数，只替换模型返回。
    # 函数用途: 留存辅助请求并返回合法交接摘要。
    def generate(_prompt, **kwargs):
        sent.append(kwargs["messages"])
        return ModelResponse(text=_valid_live_handoff(), backend="fake")
    summarize_live_tool_history(LiveToolHistorySummaryRequest(
        history=history, backend=SimpleNamespace(generate=generate),
        provider_prompt=CacheStructuredPrompt("stable", ""), provider_history_messages=tuple(prior),
    ))
    assert sent[0][:len(expected)] == expected
    assert history == original and prior == [{"role": "user", "content": "prior"}]
    assert expected[1] == prior[0]
    assert expected[2]["content"][0]["text"] == "current"
    assert expected[3]["content"][0]["text"] == "handoff"
    assert expected[4]["content"][0] == reasoning
    assert expected[4]["content"][-1]["id"] == expected[5]["content"][0]["tool_use_id"] == "retained"
