# LLM: 真实 Chat 序列化、skill_search、read_file、线程/checkpoint/CAS 与 canonical 重放；只替换供应商 HTTP 运输。
# 模块用途: 证明运行开头的正文/引用带回在工具轮和下一运行只追加，且不减少模拟器的绝对命中量。
import json
from copy import deepcopy
from dataclasses import replace

import pytest

from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.backends import http
from agent_py_agent.agent.backends.factory import get_backend
from agent_py_agent.agent.capability import method_carry
from agent_py_agent.agent.capability.package_snapshot import package_read_parameters
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
from agent_py_agent.agent.memory_archive import estimate_tokens
from agent_py_agent.tests.cache_prefix_simulator import PrefixCacheSimulator, prefix_units
from agent_py_agent.tests.test_cache_prefix_regression import _completion, _tool_call
from agent_py_agent.tests.test_conversation_method_gateway import _compact_gateway_thread
from agent_py_agent.tests.test_conversation_method_reference import _reference
from agent_py_agent.tests.test_midturn_compact_prefix import _prepare_run, _seed_method


# LLM: 只替换 HTTP 运输，供应商探针和业务请求都经同一模拟器；不替换序列化及工具执行。
# 函数用途: 捕获三个业务 payload，首轮生成真实文件读取调用。
def _install_chat_transport(monkeypatch):
    simulator, payloads = PrefixCacheSimulator(), []

    def wire(request):
        payload = deepcopy(request.payload)
        names = [row.get("function", {}).get("name") for row in payload.get("tools", [])]
        if names == ["my_agent_capability_probe"]:
            return simulator.wire(request)
        payloads.append(payload)
        response, _record = simulator.serve(payload)
        if "error" in response:
            return response
        if len(payloads) == 1:
            return _tool_call(payload, "read_file", {"path": "sample.txt"})
        return _completion(payload, "本轮完成", reasoning_content="合成思考")

    monkeypatch.setattr(http, "post_json", wire)
    return payloads, simulator


# LLM: 每次从真实 canonical Store 重建种子，不能复用上次出站 messages 对象伪造前缀。
# 函数用途: 走公开运行入口并保存合成结果供下一 run 重放。
def _startup_run_turn(agent, thread, number):
    store = agent.conversation_store
    current = store.threads.require(thread.thread_id)
    view = resolve_compact_summary_view(agent, current, THREAD_COMPACT_SCOPE)
    rows = store.messages.recent(thread.thread_id, limit=0)
    seed = ConversationHistorySeed(compact_summary=view.summary, compact_generation=view.generation,
        canonical_messages=provider_history_messages_from_rows(rows))
    request, text = f"chat-carry-{number}", f"本轮要求-{number}"
    store.messages.append({"thread_id": thread.thread_id, "role": "user", "content": text,
        "metadata": {"conversation_request_id": request}})
    result = agent.run(text, params=RunParams(save=False, allowed_tools=["read_file", "skill_search"],
        context_scope="conversation", request_id=request, run_id=request,
        task_attributes={CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR: True, "conversation_thread_id": thread.thread_id},
        conversation_history_seed=seed, compact_context=AppliedCompactContext(thread.thread_id, THREAD_COMPACT_SCOPE, view)))
    assert result.response == "本轮完成"
    store.messages.append({"thread_id": thread.thread_id, "role": "assistant", "content": result.response,
        "metadata": {"conversation_request_id": request,
                     "canonical_native_messages": canonical_native_messages_envelope(result.canonical_native_messages)}})
    return result


# LLM: 开关对照各自新线程，除现读开关和引用预算外走相同的真实协议装配。
# 函数用途: 准备已压缩的方法并执行工具轮和下一回合。
def _scenario(tmp_path, monkeypatch, enabled, reference_only):
    agent, thread, prior = _prepare_run(tmp_path, False)
    _seed_method(agent, thread, prior, monkeypatch)
    _compact_gateway_thread(agent, thread.thread_id)
    package = agent.current_skill_snapshot().resolve_package("midturn-method")
    arguments = package_read_parameters(package.to_ref())
    budget = estimate_tokens(method_carry._CARRY_HEADER + "\n\n" + _reference(arguments)) + 4 if reference_only else 3000
    path = capability_config_path_for(agent)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"conversation_method_carry_enabled: {str(enabled).lower()}\ncapability_bundle_max_tokens: {budget}\n", encoding="utf-8")
    agent.config = replace(agent.config, model_backend="openai_compatible", api_base="https://api.deepseek.com/v1",
        model_name="deepseek-v4-flash", model_auth_ref={}, model_reasoning_effort="off", stream_enabled=False)
    agent.backend = get_backend("openai_compatible", agent.config)
    (tmp_path / "sample.txt").write_text("合成文件，真实 read_file 读取。", encoding="utf-8")
    payloads, simulator = _install_chat_transport(monkeypatch)
    store = agent.conversation_store
    first = _startup_run_turn(agent, thread, 1)
    assert len(payloads) == 2, "真实 read_file 往返必须进入下一工具轮"
    row, = store.threads.require(thread.thread_id).conversation_methods
    assert row["carried_generation"] == (1 if enabled else 0)
    second = _startup_run_turn(agent, thread, 2)
    assert second.executed_tools == [] and len(payloads) == 3
    assert simulator.report()["rejected"] == [] and len(simulator.report()["partitions"]) == 1
    assert store.threads.require(thread.thread_id).compact_generation == 1
    return payloads, simulator, first, budget, arguments


# LLM: 两条 payload 独立装配后逐字节比较，稳定参数也逐键比较，不只比较共享对象身份。
# 函数用途: 核验一次工具轮或跨 run 请求只在尾部延伸。
def _assert_request_extension(before, after):
    original, extended = prefix_units(before), prefix_units(after)
    assert [part.encode("utf-8") for part in extended[:len(original)]] == [part.encode("utf-8") for part in original]
    for key in ("model", "tools", "tool_choice", "thinking", "reasoning_effort"):
        assert after.get(key) == before.get(key), key


@pytest.mark.parametrize("reference_only", [False, True], ids=["body", "reference"])
def test_startup_carry_extends_tool_and_next_run_prefix_without_lowering_cache_hits(tmp_path, monkeypatch, reference_only):
    off, baseline, _first, _budget, _arguments = _scenario(tmp_path / "off", monkeypatch, False, reference_only)
    on, simulator, first, budget, arguments = _scenario(tmp_path / "onx", monkeypatch, True, reference_only)
    for requests in (off, on):
        for before, after in zip(requests, requests[1:]):
            _assert_request_extension(before, after)
    assert all(after.hit_tokens >= before.hit_tokens for before, after in zip(baseline.calls[1:], simulator.calls[1:]))
    visible = [json.dumps(request["messages"], ensure_ascii=False) for request in on]
    assert [text.count("[会话方法参考]") for text in visible] == [1, 1, 1]
    assert all("[会话方法参考]" not in json.dumps(request["messages"], ensure_ascii=False) for request in off)
    canonical = json.dumps(first.canonical_native_messages, ensure_ascii=False)
    assert canonical.count("[会话方法参考]") == 1
    carry = next(row["content"][0]["text"] for row in first.canonical_native_messages
                 if row.get("role") == "user" and "[会话方法参考]" in json.dumps(row, ensure_ascii=False))
    assert estimate_tokens(carry) <= budget
    if reference_only:
        assert "正文这次没带回" in carry and "MIDTURN_METHOD_ENTRY" not in carry
        assert json.loads(carry.split("重读：", 1)[1].split("\n", 1)[0]) == arguments
    else:
        assert "MIDTURN_METHOD_ENTRY" in carry and "正文这次没带回" not in carry
    print("startup_cache", "reference" if reference_only else "body", "budget", budget,
          "carry_tokens", estimate_tokens(carry), "off_hits", [row.hit_tokens for row in baseline.calls],
          "on_hits", [row.hit_tokens for row in simulator.calls])
