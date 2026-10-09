# LLM: 仅传输和文件工具用合成替身，真实回合/检查点/存储必须执行，以捕获跨回合而非单次投影失配。
# 模块用途: 验证密文与文字中途压缩的两回合前缀延伸、后续压缩同源及工具配对。
"""两次真实工具循环、临时 Store/checkpoint；仅替换模型传输和文件工具 handler。"""
import json
from copy import deepcopy

import pytest

from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.backends.base import ProviderToolCapability
from agent_py_agent.agent.capability.runtime_config_reload import capability_config_path_for
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


# LLM: 仅 fake Responses 运输；真实 skill_search、安装、主运行身份和 canonical 行都执行，不手插方法账本。
# 函数用途: 在长运行之前成功读过一个包，冻结在用目录，避免把首次目录变化混作带回失配。
def _seed_method(agent, thread, prior, monkeypatch):
    from agent_py_agent.agent.plugin_activation import PluginActivationRequest
    from agent_py_agent.agent.plugin_content_activation import PluginContentActivation
    from agent_py_agent.agent.plugin_install_store import PluginInstallStore
    from agent_py_agent.agent.plugin_installation import PluginInstallRequest
    from agent_py_agent.agent.plugin_package import inspect_plugin_package
    from agent_py_agent.agent.user_space.owner_resolver import resolve_owner_home
    from agent_py_agent.tests.test_capability_package import content_bundle

    package = inspect_plugin_package(content_bundle(files={"CAPABILITY.md": b"MIDTURN_METHOD_ENTRY"},
        change=lambda row: row.update(plugin_id="midturn-method")))
    install = PluginInstallStore(resolve_owner_home(agent.home_paths.root))
    row = install.install(PluginInstallRequest(package, "install-method", 0)).installation
    activation = PluginContentActivation("enable-method", "midturn-method", row.package_sha256, row.revision, row.settings_revision)
    install.change_activation(PluginActivationRequest("enable-method", row.revision, activation))
    sent = []

    def seed_io(_path, payload, _headers, **_options):
        sent.append(deepcopy(payload))
        if len(sent) == 1:
            return {"status": "completed", "output": [{"type": "function_call", "call_id": "seed-method",
                "name": "skill_search", "arguments": '{"action":"get","package_id":"midturn-method"}'}]}
        return {"status": "completed", "output": [{"type": "message", "role": "assistant",
            "content": [{"type": "output_text", "text": "已读方法"}]}]}

    monkeypatch.setattr(agent.backend, "request_json", seed_io)
    store = agent.conversation_store
    store.messages.append({"thread_id": thread.thread_id, "role": "user", "content": "先读方法",
                           "metadata": {"conversation_request_id": "seed-method"}})
    result = agent.run("先读方法", params=RunParams(save=False, allowed_tools=["read_file", "skill_search"],
        context_scope="conversation", request_id="seed-method", run_id="seed-method",
        task_attributes={CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR: True, "conversation_thread_id": thread.thread_id},
        conversation_history_seed=ConversationHistorySeed(canonical_messages=prior)))
    assert result.response == "已读方法" and len(sent) == 2
    assert len(store.threads.require(thread.thread_id).conversation_methods) == 1
    store.messages.append({"thread_id": thread.thread_id, "role": "assistant", "content": result.response,
        "metadata": {"conversation_request_id": "seed-method",
                     "canonical_native_messages": canonical_native_messages_envelope(result.canonical_native_messages)}})
    return provider_history_messages_from_rows(store.messages.recent(thread.thread_id, limit=0))


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


# LLM: 沿真实方法读取准备主会话目录，开关只改合成工作区，保持原生产权限边界。
# 函数用途: 给文字/密文用例设置未读、开启、关闭三种对照。
def _prepare_midturn_method(run, monkeypatch, method_mode):
    agent, thread, prior = run
    if method_mode != "none":
        prior = _seed_method(agent, thread, prior, monkeypatch)
        if method_mode == "off":
            path = capability_config_path_for(agent)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("conversation_method_carry_enabled: false\n", encoding="utf-8")
    return prior


# LLM: 存储宿主需把 canonical 结果写入真实行，下一轮由公开重放函数读取，不能手造出站前缀。
# 函数用途: 为两次真实运行提供合成输入与结果的持久化函数。
def _midturn_runner(agent, thread):
    store = agent.conversation_store
    # LLM: 存储宿主需把canonical结果写入真实行，下一轮由公开重放函数读取。
    # 函数用途: 执行一轮并持久合成输入与结果。
    def run_turn(number, seed, context):
        request = f"request-{number}"
        text = f"本轮要求-{number}"
        store.messages.append({"thread_id": thread.thread_id, "role": "user", "content": text,
                               "metadata": {"conversation_request_id": request}})
        result = agent.run(text, params=RunParams(save=False, allowed_tools=["read_file", "skill_search"], context_scope="conversation",
            request_id=request, run_id=request, task_attributes={CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR: True,
                "conversation_thread_id": thread.thread_id}, conversation_history_seed=seed, compact_context=context))
        store.messages.append({"thread_id": thread.thread_id, "role": "assistant", "content": result.response,
            "metadata": {"conversation_request_id": request,
                         "canonical_native_messages": canonical_native_messages_envelope(result.canonical_native_messages)}})
        return result
    return run_turn


# LLM: 两轮都走公开agent.run，不能短路恢复与提交；保留原生工具对的容量用例独立测试。
# 函数用途: 比较本轮中途压缩的末请求与下一轮首请求的完整前缀。
@pytest.mark.parametrize("remote", [True, False], ids=["responses_compaction", "text_summary"])
@pytest.mark.parametrize("method_mode", ["none", "on", "off"])
def test_two_real_turns_extend_post_compact_provider_prefix(tmp_path, monkeypatch, remote, method_mode):
    agent, thread, prior = _prepare_run(tmp_path, remote)
    prior = _prepare_midturn_method((agent, thread, prior), monkeypatch, method_mode)
    store = agent.conversation_store
    business, summaries, executed = _install_fake_io(agent, thread, monkeypatch, remote)
    run_turn = _midturn_runner(agent, thread)

    first = run_turn(1, ConversationHistorySeed(canonical_messages=prior),
                     AppliedCompactContext(thread.thread_id, THREAD_COMPACT_SCOPE, resolve_compact_summary_view(agent, thread, THREAD_COMPACT_SCOPE)))
    current = store.threads.require(thread.thread_id)
    assert first.response == "完成" and current.compact_generation == 1 and executed and summaries, (
        first.response, current.compact_generation, len(executed), len(summaries), len(business),
        [len(str(row["input"])) for row in business],
        [str(item.get("output", ""))[:300] for item in business[-1]["input"] if item.get("type") == "function_call_output"])
    last = business[-1]
    carry_count = json.dumps(last["input"], ensure_ascii=False).count("[会话方法参考]")
    assert carry_count == (1 if method_mode == "on" else 0)
    if method_mode != "none":
        assert current.conversation_methods[0]["carried_generation"] == (1 if method_mode == "on" else 0)
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
    original_bytes = json.dumps(before, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    extended_bytes = json.dumps(after[:len(before)], ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    assert extended_bytes == original_bytes
    assert json.dumps(after, ensure_ascii=False).count("[会话方法参考]") == carry_count, "同代下一 run 不得再追加带回块"


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


@pytest.mark.parametrize("remote", [True, False], ids=["remote", "text"])
def test_compactcall_captures_full_live_wire_prefix(tmp_path, monkeypatch, remote):
    import json

    agent, thread, prior = _prepare_run(tmp_path, remote)
    business, summaries, _ = _install_fake_io(agent, thread, monkeypatch, remote)
    # 提前触发是为了验证窗口内可共享前缀；90%+大块结果会真实超窗，另测其瘦身/分段边界。
    agent.config.memory_compact_auto_trigger_percent = 50
    pairs = []
    send = agent.backend.request_json

    def capture(path, payload, headers, **options):
        previous = deepcopy(business[-1]) if business else None
        count = len(summaries)
        response = send(path, payload, headers, **options)
        if len(summaries) > count:
            pairs.append((previous, deepcopy(payload)))
        return response

    monkeypatch.setattr(agent.backend, "request_json", capture)
    agent.run("本轮要求", params=RunParams(
        save=False, allowed_tools=["read_file"], context_scope="conversation",
        request_id="compactcall", run_id="compactcall",
        task_attributes={CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR: True, "conversation_thread_id": thread.thread_id},
        conversation_history_seed=ConversationHistorySeed(canonical_messages=prior),
        compact_context=AppliedCompactContext(thread.thread_id, THREAD_COMPACT_SCOPE,
            resolve_compact_summary_view(agent, thread, THREAD_COMPACT_SCOPE)),
    ))
    assert pairs
    main, compact = pairs[0]
    def encode(value):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
    a, b = main["input"], compact["input"]
    first = next((i for i, (x, y) in enumerate(zip(a, b)) if encode(x) != encode(y)), min(len(a), len(b)))
    print("compactcall_fork", remote, first, str(a[first])[:180] if first < len(a) else "end",
          str(b[first])[:180] if first < len(b) else "end")
    for field in ("instructions", "tools", "tool_choice", "reasoning"):
        a, b = encode(main.get(field)), encode(compact.get(field))
        mismatch = next((i for i, (x, y) in enumerate(zip(a, b)) if x != y), min(len(a), len(b)))
        print("compactcall_live", remote, field, "sizes", len(a), len(b), "first_byte", mismatch,
              "tool_count", len(main.get("tools", [])), len(compact.get("tools", [])))
        assert a == b, field
    a, b = main["input"], compact["input"]
    mismatch = next((i for i, (x, y) in enumerate(zip(a, b)) if encode(x) != encode(y)), min(len(a), len(b)))
    print("compactcall_live", remote, "input", "counts", len(a), len(b), "first_item", mismatch)
    assert encode(b[:len(a)]) == encode(a)


@pytest.mark.parametrize("remote", [True, False], ids=["remote", "text"])
@pytest.mark.parametrize("last_role", ["user", "assistant"])
def test_compactcall_captures_full_preflight_wire_prefix(tmp_path, remote, last_role):
    import json

    from agent_py_agent.agent.conversation.compact import _CompactSummaryCall, _summarize
    from agent_py_agent.agent.conversation.compact_provider_surface import (
        ConversationCompactModelSurface,
        prepare_conversation_compact_provider_surface,
    )

    agent, thread, prior = _prepare_run(tmp_path, remote)
    if last_role == "user":
        agent.conversation_store.messages.append({"thread_id": thread.thread_id, "role": "user", "content": "历史末尾用户补充", "metadata": {}})
        prior = provider_history_messages_from_rows(agent.conversation_store.messages.recent(thread.thread_id, limit=0))
    payloads = []

    def send(_path, payload, _headers, **_options):
        payloads.append(deepcopy(payload))
        output = [BLOCK["item"]] if payload["input"][-1].get("type") == "compaction_trigger" else [
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "摘要"}]}]
        return {"status": "completed", "output": output}

    agent.backend.request_json = send
    agent.run("当前用户要求", params=RunParams(
        save=False, allowed_tools=["read_file"], context_scope="conversation", run_id="compactcall",
        task_attributes={"conversation_thread_id": thread.thread_id},
        conversation_history_seed=ConversationHistorySeed(canonical_messages=prior),
    ))
    surface = prepare_conversation_compact_provider_surface(agent, ConversationCompactModelSurface(
        allowed_tools=("read_file",), context_scope="conversation", thread_id=thread.thread_id), run_id="compactcall")
    rows = agent.conversation_store.messages.recent(thread.thread_id, limit=0)
    _summarize(agent, "", {}, rows, call=_CompactSummaryCall(provider_surface=surface, thread_id=thread.thread_id))
    main, compact = payloads[0], payloads[-1]
    def encode(value):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
    for field in ("instructions", "tools", "tool_choice", "reasoning"):
        a, b = encode(main.get(field)), encode(compact.get(field))
        mismatch = next((i for i, (x, y) in enumerate(zip(a, b)) if x != y), min(len(a), len(b)))
        print("compactcall_preflight", remote, field, "sizes", len(a), len(b), "first_byte", mismatch,
              "tool_count", len(main.get("tools", [])), len(compact.get("tools", [])))
        assert a == b, field
    count = next((i for i, item in enumerate(compact["input"]) if item.get("type") in {"configuration_update", "compaction_trigger"}), len(compact["input"]) - 1)
    a, b = main["input"], compact["input"][:count]
    print("compactcall_preflight", remote, last_role, "input", len(a), len(compact["input"]), "shared", len(b))
    assert encode(a[:len(b)]) == encode(b)
    assert len(b) == len(rows)


@pytest.mark.parametrize("remote", [True, False])
def test_compactcall_live_preserves_parent_volatile_item_bytes(remote):
    from types import SimpleNamespace

    from agent_py_agent.agent.backends.base import ProviderRequestOptions
    from agent_py_agent.agent.backends.tool_ir import UserTurn
    from agent_py_agent.agent.memory_archive.compact_semantic_summary import (
        LiveToolHistorySummaryRequest,
        summarize_live_tool_history,
    )
    from agent_py_agent.agent.prompting_parts.cache_layout import CacheStructuredPrompt
    from agent_py_agent.tests.test_compact_remote_provider import _subscription_backend
    from agent_py_agent.tests.test_native_tool_ir_compact_and_orphan_sweep import (
        _valid_live_handoff,
    )

    backend, config, _ = _subscription_backend()
    config.memory_compact_remote_enabled = remote
    config.model_context_window_tokens = 128_000
    payloads = []

    def send(_path, payload, _headers, **_options):
        payloads.append(deepcopy(payload))
        output = [BLOCK["item"]] if payload["input"][-1].get("type") == "compaction_trigger" else [
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": _valid_live_handoff()}]}]
        return {"status": "completed", "output": output}

    backend.request_json = send
    prompt = CacheStructuredPrompt("稳定", " \n原动态尾巴\n ")
    history = [{"role": "user", "content": [{"type": "text", "text": "历史"}]}]
    backend.generate(prompt, messages=history, request_options=ProviderRequestOptions(system_instruction="系统"))
    agent = SimpleNamespace(backend=backend, config=config)
    summarize_live_tool_history(LiveToolHistorySummaryRequest(
        history=[UserTurn("历史")], backend=backend, agent=agent, provider_prompt=prompt, system_instruction="系统"))
    main, compact = payloads[0], payloads[-1]
    assert compact["instructions"] == main["instructions"]
    assert not any(item.get("type") == "configuration_update" for item in main["input"])
    assert compact["input"][-2]["type"] == "configuration_update", "压缩运输确实执行降档，不只测无更新分支"
    assert compact["input"][:len(main["input"])] == main["input"]
