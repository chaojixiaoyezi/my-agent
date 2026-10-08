"""服务端压缩（compact_remote，2026-10-08，07）：Responses 后端发触发项/解析压缩项、远端助手的预算与回退、检查点与视图的
兼容性判断、主请求前缀发压缩项、live 摘要执行器走远端。全部用假后端，不联网。"""
from __future__ import annotations

import json
from copy import deepcopy
from types import SimpleNamespace

from agent_py_agent.agent.backends.base import ModelResponse
from agent_py_agent.agent.backends.factory import get_backend
from agent_py_agent.agent.backends.responses_wire import (
    compaction_item,
    message_items,
    response_fields,
)
from agent_py_agent.agent.backends.tool_ir import CompactionSummary
from agent_py_agent.agent.conversation import ConversationStore, auxiliary_model_call
from agent_py_agent.agent.conversation.auxiliary_model_call import (
    AuxiliaryModelCallRequest,
    generate_auxiliary_model_response,
)
from agent_py_agent.agent.conversation.compact import (
    ConversationCompactOptions,
    load_conversation_compact_source,
    prepare_conversation_context,
)
from agent_py_agent.agent.conversation.compact_calibration import CompactRequestCalibration
from agent_py_agent.agent.conversation.compact_provider_surface import (
    ConversationCompactProviderSurface,
    conversation_compact_provider_messages,
    conversation_compact_provider_source,
)
from agent_py_agent.agent.conversation.compact_remote import (
    PROVIDER_COMPACTION_SCHEMA,
    RemoteCompactionRequest,
    provider_compaction_compatible,
    provider_compaction_content,
    provider_compaction_marker,
    remote_compaction_enabled,
    request_remote_compaction,
)
from agent_py_agent.agent.conversation.compact_scope import THREAD_COMPACT_SCOPE
from agent_py_agent.agent.conversation.compact_summary_view import resolve_compact_summary_view
from agent_py_agent.agent.memory_archive.compact_semantic_summary import (
    LiveToolHistorySummaryRequest,
    summarize_live_tool_history,
)
from agent_py_agent.agent.prompting_parts.cache_layout import CacheStructuredPrompt
from agent_py_agent.agent.settings.config import AgentConfig

CIPHER = "ZW5jcnlwdGVkLWNvbXBhY3Rpb24="
ITEM = {"type": "compaction", "encrypted_content": CIPHER}
BLOCK = {"type": "responses_compaction", "model": "fake", "item": ITEM}


def test_wire_layer_parses_and_replays_compaction_items():
    assert compaction_item({"type": "compaction", "encrypted_content": CIPHER, "id": "x"}) == ITEM
    assert compaction_item({"type": "compaction", "encrypted_content": 7}) == {}
    fields = response_fields({"status": "completed", "output": [{"type": "compaction", "encrypted_content": CIPHER}]}, "m")
    assert fields["assistant_content_blocks"] == [{"type": "responses_compaction", "model": "m", "item": ITEM}]
    assert message_items({"role": "user", "content": [{"type": "responses_compaction", "item": ITEM}]}, "m") == [ITEM]


# 函数用途: 造一个只截获 JSON 载荷的订阅 Responses 后端（auth_ref 模拟 chatgpt 登录，不读凭据）。
def _subscription_backend():
    config = AgentConfig(model_backend="openai_responses", api_key="fake", stream_enabled=False, model_name="gpt-6.1-sol",
                         api_base="https://chatgpt.com/backend-api/codex")
    backend = get_backend("openai_responses", config)
    backend.auth_ref = {"mode": "chatgpt", "path": "x", "provider_id": "p", "generation": "g", "binding": "b"}
    payloads = []

    def request_json(_path, payload, _headers, **_options):
        payloads.append(deepcopy(payload))
        return {"status": "completed", "output": [{"type": "compaction", "encrypted_content": CIPHER}]}

    backend.request_json = request_json
    return backend, config, payloads


def test_responses_backend_sends_the_trigger_last_and_declares_the_capability():
    backend, config, payloads = _subscription_backend()
    assert backend.supports_remote_compaction() is True
    assert backend.provider_compaction_scope() == {"protocol": "openai_responses", "endpoint": backend.api_base}
    agent = SimpleNamespace(backend=backend, config=config, conversation_store=SimpleNamespace(threads=SimpleNamespace(load=lambda _id: None)))
    response = generate_auxiliary_model_response(AuxiliaryModelCallRequest(
        agent=agent, prompt=CacheStructuredPrompt("稳定前缀", ""), messages=[{"role": "user", "content": [{"type": "text", "text": "历史"}]}],
        tools=[{"name": "read_file", "description": "read", "input_schema": {"type": "object"}}], purpose="conversation_compact_remote",
        compaction_trigger=True))
    payload = payloads[-1]
    assert payload["input"][-1] == {"type": "compaction_trigger"} and payload["input"][-2]["role"] == "user"
    assert payload["tools"][0]["name"] == "read_file" and "instructions" in payload
    assert response.assistant_content_blocks == [{"type": "responses_compaction", "model": "gpt-6.1-sol", "item": ITEM}]
    plain = get_backend("openai_responses", AgentConfig(model_backend="openai_responses", api_key="k", stream_enabled=False,
                                                        model_name="gpt-6.1-sol", api_base="https://api.openai.com/v1"))
    assert plain.supports_remote_compaction() is False, "API Key 端点未核实，不声明"


# 函数用途: 造一个声明支持服务端压缩的假后端和 agent，generate 由 monkeypatch 控制。
def _remote_agent(monkeypatch, response, *, window=20_000, enabled=True):
    # 模拟 GPT-6 订阅后端：能降档（小输出预留才生效，见 call_runtime.compact_summary_output_reserve_tokens）且声明服务端压缩
    backend = SimpleNamespace(name="openai_responses", model_name="gpt-6.1-sol", max_tokens=2_000, api_base="https://chatgpt.com/backend-api/codex",
                              supports_remote_compaction=lambda: True, supports_reasoning_update_items=lambda: True,
                              provider_compaction_scope=lambda: {"protocol": "openai_responses", "endpoint": "https://chatgpt.com/backend-api/codex"})
    calls = []

    def generate(request):
        calls.append(request)
        return response() if callable(response) else response

    monkeypatch.setattr(auxiliary_model_call, "generate_auxiliary_model_response", generate)
    agent = SimpleNamespace(backend=backend, config=SimpleNamespace(model_context_window_tokens=window, memory_compact_remote_enabled=enabled,
                                                                    memory_compact_summary_max_output_tokens=1_000,
                                                                    memory_compact_reasoning_level="low"))
    return agent, calls


def test_remote_request_returns_a_record_and_falls_back_on_failure(monkeypatch):
    good = ModelResponse(text="", backend="fake", assistant_content_blocks=[BLOCK])
    agent, calls = _remote_agent(monkeypatch, good)
    assert remote_compaction_enabled(agent) is True
    record = request_remote_compaction(RemoteCompactionRequest(agent=agent, prompt="p", messages=[{"role": "user", "content": "历史"}]))
    assert record["schema"] == PROVIDER_COMPACTION_SCHEMA and record["protocol"] == "openai_responses"
    assert record["item"] == ITEM and record["model"] == "gpt-6.1-sol" and len(record["sha256"]) == 64
    assert calls[-1].compaction_trigger is True and calls[-1].purpose == "conversation_compact_remote"
    assert provider_compaction_content(record) == CIPHER and "provider-compaction" in provider_compaction_marker(record)
    # 没返回压缩项 / 后端抛错 / 开关关 → None，交给客户端压缩
    agent, _ = _remote_agent(monkeypatch, ModelResponse(text="只有文字", backend="fake"))
    assert request_remote_compaction(RemoteCompactionRequest(agent=agent, prompt="p", messages=[])) is None

    def boom():
        raise RuntimeError("provider rejected")

    agent, _ = _remote_agent(monkeypatch, boom)
    assert request_remote_compaction(RemoteCompactionRequest(agent=agent, prompt="p", messages=[])) is None
    agent, _ = _remote_agent(monkeypatch, good, enabled=False)
    assert remote_compaction_enabled(agent) is False


def test_remote_request_shrinks_old_tool_outputs_or_gives_up(monkeypatch):
    good = ModelResponse(text="", backend="fake", assistant_content_blocks=[BLOCK])
    agent, calls = _remote_agent(monkeypatch, good, window=6_000)
    results = [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"c{i}", "content": "x" * 6_000}]} for i in range(6)]
    assert request_remote_compaction(RemoteCompactionRequest(agent=agent, prompt="p", messages=results)) is not None
    sent = calls[-1].messages
    assert sent[0]["content"][0]["content"].startswith("[compact-omitted-tool-output") and sent[-1]["content"][0]["content"].startswith("x")
    assert results[0]["content"][0]["content"].startswith("x"), "只改交给远端请求的副本"
    agent, calls = _remote_agent(monkeypatch, good, window=1_200)
    assert request_remote_compaction(RemoteCompactionRequest(agent=agent, prompt="p", messages=results)) is None and not calls


# 函数用途: 远端压缩的预算检查和单次缓存面同一校准口径：主请求观测到估算是实际的两倍，装得下就逐字发、不瘦身。
def test_remote_budget_uses_the_main_request_calibration(monkeypatch):
    good = ModelResponse(text="", backend="fake", assistant_content_blocks=[BLOCK])
    agent, calls = _remote_agent(monkeypatch, good, window=6_000)
    results = [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"c{i}", "content": "x" * 4_000}]} for i in range(6)]
    assert request_remote_compaction(RemoteCompactionRequest(agent=agent, prompt="p", messages=results)) is not None
    assert calls[-1].messages[0]["content"][0]["content"].startswith("[compact-omitted-tool-output"), "前提：原始上界下会瘦身"
    calibration = CompactRequestCalibration(raw_estimated_tokens=100_000, provider_input_tokens=50_000)
    request = RemoteCompactionRequest(agent=agent, prompt="p", messages=results, calibration=calibration)
    assert request_remote_compaction(request) is not None
    assert all(row["content"][0]["content"].startswith("x") for row in calls[-1].messages), "按校准口径装得下，不换占位"


def test_compatibility_only_matches_protocol_and_endpoint():
    record = {"protocol": "openai_responses", "endpoint": "https://chatgpt.com/backend-api/codex", "item": ITEM}
    same = SimpleNamespace(provider_compaction_scope=lambda: {"protocol": "openai_responses", "endpoint": "https://chatgpt.com/backend-api/codex"})
    other = SimpleNamespace(provider_compaction_scope=lambda: {"protocol": "openai_responses", "endpoint": "https://api.openai.com/v1"})
    assert provider_compaction_compatible(same, record) and not provider_compaction_compatible(other, record)
    assert not provider_compaction_compatible(SimpleNamespace(), record) and not provider_compaction_compatible(same, "junk")


def test_prefix_messages_replay_the_item_instead_of_the_marker():
    messages = list(conversation_compact_provider_source("[provider-compaction …] 占位", 2, (), previous_provider_compaction=CIPHER))
    assert messages[0]["content"] == [{"type": "responses_compaction", "item": ITEM}]
    assert message_items(messages[0], "m") == [ITEM]
    assert conversation_compact_provider_messages("普通摘要", 2, ())[0]["content"][0]["type"] == "text"
    assert CompactionSummary("x", provider_compaction=CIPHER).provider_compaction == CIPHER


# 函数用途: 写一份有真实消息文件的线程，返回 agent（可换后端）与线程。
def _thread(tmp_path, backend, count=8):
    store = ConversationStore(tmp_path / "conversation")
    thread = store.threads.get_or_create({"canonical_user_id": "owner", "now": 1})
    path = store.storage.message_path(thread.thread_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        for index in range(count):
            row = {"message_id": f"m-{index}", "thread_id": thread.thread_id, "role": "user" if index % 2 == 0 else "assistant",
                   "content": f"原文{index}:" + "x" * 400, "metadata": {}}
            handle.write((json.dumps(row, ensure_ascii=False) + "\n").encode())
    agent = SimpleNamespace(conversation_store=store, home_paths=SimpleNamespace(owner_compact_dir=tmp_path / "compact"), backend=backend,
                            prompts=SimpleNamespace(build=lambda prompt, *_a, **_k: prompt),
                            config=SimpleNamespace(model_context_window_tokens=20_000, memory_compact_remote_enabled=True,
                                                   memory_compact_summary_max_output_tokens=1_000))
    return agent, thread


def test_transcript_compaction_stores_the_item_and_incompatible_backends_see_through_it(tmp_path, monkeypatch):
    backend = SimpleNamespace(name="openai_responses", model_name="gpt-6.1-sol", max_tokens=2_000, api_base="https://chatgpt.com/backend-api/codex",
                              supports_remote_compaction=lambda: True,
                              provider_compaction_scope=lambda: {"protocol": "openai_responses", "endpoint": "https://chatgpt.com/backend-api/codex"})
    agent, thread = _thread(tmp_path, backend)
    calls = []

    def generate(request):
        calls.append(request)
        return ModelResponse(text="", backend="fake", assistant_content_blocks=[BLOCK])

    monkeypatch.setattr(auxiliary_model_call, "generate_auxiliary_model_response", generate)
    source = load_conversation_compact_source(agent, agent.conversation_store, thread, scope=THREAD_COMPACT_SCOPE)
    result = prepare_conversation_context(agent, agent.conversation_store, thread, options=ConversationCompactOptions(
        source=source, force=True, exclude_request_id="current-request",
        provider_surface=ConversationCompactProviderSurface("稳定前缀", None, "摘要系统")))
    assert result.compacted and calls and calls[-1].compaction_trigger is True
    assert result.thread.summary.startswith("[provider-compaction openai_responses")
    rows = [json.loads(line) for line in (tmp_path / "compact" / "conversations" / f"{thread.thread_id}.jsonl").read_text().splitlines()]
    assert rows[-1]["provider_compaction"]["item"] == ITEM and rows[-1]["summary_sha256"]
    view = resolve_compact_summary_view(agent, result.thread, THREAD_COMPACT_SCOPE)
    assert provider_compaction_content(view.provider_compaction) == CIPHER and view.source_message_ids
    # 换到读不了压缩项的后端：检查点透明，历史重新算未覆盖，下一次压缩从原文重新做
    agent.backend = SimpleNamespace(name="anthropic_compatible", model_name="MiniMax-M2.7", max_tokens=2_000)
    blind = resolve_compact_summary_view(agent, result.thread, THREAD_COMPACT_SCOPE)
    assert blind.checkpoint_id == "" and not blind.source_message_ids
    again = load_conversation_compact_source(agent, agent.conversation_store, result.thread, scope=THREAD_COMPACT_SCOPE)
    assert len(again.messages) >= len(view.source_message_ids)


# LLM: 10-08 生产：服务端压缩后的第二次压缩曾被当成非文本内容拒绝（COMPACT_REQUEST_NON_TEXT，request_content 分类）。
#   这里守 transcript 路径的链式压缩：前缀逐字发上一代压缩项、再次触发、存下新一代的项。
# 函数用途: 服务端压缩之后再压一次，前缀带压缩项照样能压并链式存项。
def test_second_transcript_compaction_chains_on_the_stored_item(tmp_path, monkeypatch):
    backend = SimpleNamespace(name="openai_responses", model_name="gpt-6.1-sol", max_tokens=2_000, api_base="https://chatgpt.com/backend-api/codex",
                              supports_remote_compaction=lambda: True,
                              provider_compaction_scope=lambda: {"protocol": "openai_responses", "endpoint": "https://chatgpt.com/backend-api/codex"})
    agent, thread = _thread(tmp_path, backend)
    calls = []
    monkeypatch.setattr(auxiliary_model_call, "generate_auxiliary_model_response",
                        lambda request: calls.append(request) or ModelResponse(text="", backend="fake", assistant_content_blocks=[BLOCK]))
    surface = ConversationCompactProviderSurface("稳定前缀", None, "摘要系统")
    source = load_conversation_compact_source(agent, agent.conversation_store, thread, scope=THREAD_COMPACT_SCOPE)
    first = prepare_conversation_context(agent, agent.conversation_store, thread, options=ConversationCompactOptions(
        source=source, force=True, exclude_request_id="request-1", provider_surface=surface))
    assert first.compacted
    path = agent.conversation_store.storage.message_path(first.thread.thread_id)
    with path.open("ab") as handle:
        for index in range(8, 16):
            row = {"message_id": f"m-{index}", "thread_id": first.thread.thread_id, "role": "user" if index % 2 == 0 else "assistant",
                   "content": f"续文{index}:" + "y" * 400, "metadata": {}}
            handle.write((json.dumps(row, ensure_ascii=False) + "\n").encode())
    source = load_conversation_compact_source(agent, agent.conversation_store, first.thread, scope=THREAD_COMPACT_SCOPE)
    second = prepare_conversation_context(agent, agent.conversation_store, first.thread, options=ConversationCompactOptions(
        source=source, force=True, exclude_request_id="request-2", provider_surface=surface))
    assert second.compacted and second.thread.compact_generation == first.thread.compact_generation + 1
    assert calls[-1].compaction_trigger is True
    assert calls[-1].messages[0]["content"] == [{"type": "responses_compaction", "item": ITEM}], "前缀逐字发上一代压缩项"
    view = resolve_compact_summary_view(agent, second.thread, THREAD_COMPACT_SCOPE)
    assert provider_compaction_content(view.provider_compaction) == CIPHER and view.generation == second.thread.compact_generation


def test_live_tool_summary_takes_the_remote_path_when_available(monkeypatch):
    agent, calls = _remote_agent(monkeypatch, ModelResponse(text="", backend="fake", assistant_content_blocks=[BLOCK]))
    outcome = []
    text = summarize_live_tool_history(LiveToolHistorySummaryRequest(
        history=[CompactionSummary("旧交接")], backend=agent.backend, agent=agent, provider_prompt=CacheStructuredPrompt("稳定前缀", "动态尾巴"),
        provider_history_messages=({"role": "user", "content": [{"type": "text", "text": "历史"}]},), tools=(), system_instruction="系统",
        provider_outcome=outcome))
    assert text.startswith("[provider-compaction") and outcome and outcome[-1]["item"] == ITEM
    sent = calls[-1]
    assert sent.compaction_trigger is True and sent.system_instruction == "系统" and sent.messages[0]["content"][0]["text"] == "历史"
    assert str(sent.prompt) == "稳定前缀\n\n动态尾巴" or "动态尾巴" in str(sent.prompt)
    # 开关关：回到原文字摘要路径（这里假 generate 返回空正文 → 机械兜底），不出现占位文本
    agent, _ = _remote_agent(monkeypatch, ModelResponse(text="", backend="fake"), enabled=False)
    text = summarize_live_tool_history(LiveToolHistorySummaryRequest(
        history=[CompactionSummary("旧交接")], backend=agent.backend, agent=agent, provider_prompt=CacheStructuredPrompt("稳定前缀", ""),
        provider_history_messages=(), tools=(), system_instruction="系统", provider_outcome=outcome))
    assert "provider-compaction" not in text


def test_memory_settings_parse_the_remote_switch():
    from agent_py_agent.agent.settings.memory import normalize_memory_settings

    assert AgentConfig().memory_compact_remote_enabled is True
    settings, warnings = normalize_memory_settings({"memory_compact_remote_enabled": False})
    assert settings.memory_compact_remote_enabled is False and warnings == []
    assert normalize_memory_settings({})[0].memory_compact_remote_enabled is True


def test_archive_keeps_the_marker_but_never_the_item():
    from agent_py_agent.agent.agent_core.runtime.loop_support import _completed_turn_native_messages
    from agent_py_agent.agent.conversation.native_history import (
        canonical_native_messages_envelope,
        canonical_native_messages_from_metadata,
    )

    params = SimpleNamespace(tool_ir_history=[CompactionSummary("[provider-compaction …] 占位", source="applied_compact", provider_compaction=CIPHER)],
                             turn_trigger=None, tool_protocol_snapshot=SimpleNamespace(source_protocol="native"))
    import agent_py_agent.agent.agent_core.runtime.loop_support as loop_support
    original = loop_support._completed_turn_native_messages
    # 原生协议判定借 monkeypatch 以外的方式：直接给 native_tool_use_active 可识别的快照不稳定，这里改用模块函数替身
    from agent_py_agent.agent.agent_core import native_tool_protocol
    saved = native_tool_protocol.native_tool_use_active
    native_tool_protocol.native_tool_use_active = lambda _p: True
    try:
        archived = original(params, ModelResponse(text="完成", backend="fake"))
    finally:
        native_tool_protocol.native_tool_use_active = saved
    assert archived[0]["content"] == [{"type": "text", "text": "[provider-compaction …] 占位"}], "落盘只留占位文本"
    assert all(b.get("type") != "responses_compaction" for m in archived for b in (m["content"] if isinstance(m["content"], list) else []))
    # 旧版本已经落盘的块：回放时剔除，整条只剩该块时丢弃
    envelope = canonical_native_messages_envelope([
        {"role": "user", "content": [{"type": "responses_compaction", "item": ITEM}]},
        {"role": "user", "content": [{"type": "responses_compaction", "item": ITEM}, {"type": "text", "text": "还有字"}]},
        {"role": "assistant", "content": "回答"},
    ])
    replayed = canonical_native_messages_from_metadata({"canonical_native_messages": envelope})
    assert [m["content"] for m in replayed] == [[{"type": "text", "text": "还有字"}], "回答"]
