# LLM: 2026-10-08 压缩缓存面修复的回归护栏：预算与实际发送同源的压缩输出预留、超预算先瘦身旧工具输出再发单次请求、
#   GPT-6 Responses 用 configuration_update 项给压缩降档而不改请求级档位、TUI 会话累计命中率。假传输只截获 JSON 载荷，不联网。
# 模块用途: 锁住“sol/astra/MiniMax 按 90% 触发时压缩也走单次缓存面请求”这组合同；改预算公式、瘦身规则或降档项时同步本文件。
"""压缩请求装进窗口、复用主请求前缀、降档不破坏缓存。"""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.model.call_runtime import (
    compact_summary_output_reserve_tokens,
    max_output_tokens,
)
from agent_py_agent.agent.backends import get_backend
from agent_py_agent.agent.backends.base import ModelResponse
from agent_py_agent.agent.conversation import compact_request_budget as budget_module
from agent_py_agent.agent.conversation.auxiliary_model_call import (
    AuxiliaryModelCallRequest,
    generate_auxiliary_model_response,
)
from agent_py_agent.agent.conversation.compact_message_source import (
    TOOL_OUTPUT_PLACEHOLDER,
    CompactMessageSource,
    shrink_tool_outputs,
)
from agent_py_agent.agent.conversation.model_metrics import (
    public_model_metrics,
    publish_model_metrics,
)
from agent_py_agent.agent.prompting_parts.cache_layout import CacheStructuredPrompt
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.settings.memory import normalize_memory_settings
from agent_py_agent.cli.chat_parts.tui_model_metrics import render_model_metrics
from agent_py_agent.tests.test_tui_model_metrics import fixture as metrics_fixture
from agent_py_agent.tests.test_tui_model_metrics import settled as metrics_settled

SOL_WINDOW = 272_000
MINIMAX_WINDOW = 262_144


# 函数用途: 造一个只带窗口、输出上限和压缩配置的最小 agent 替身。
def _agent(window: int, *, backend_max_tokens: int = 32_000, reserve: int = 16_384) -> SimpleNamespace:
    return SimpleNamespace(
        backend=SimpleNamespace(max_tokens=backend_max_tokens),
        config=SimpleNamespace(model_context_window_tokens=window, memory_compact_summary_max_output_tokens=reserve),
    )


def test_compact_output_reserve_follows_config_but_never_exceeds_the_main_cap():
    assert compact_summary_output_reserve_tokens(_agent(SOL_WINDOW)) == 16_384
    assert compact_summary_output_reserve_tokens(_agent(SOL_WINDOW, reserve=0)) == 32_000
    assert compact_summary_output_reserve_tokens(_agent(SOL_WINDOW, reserve=50_000)) == 32_000
    unknown = _agent(SOL_WINDOW, backend_max_tokens=0, reserve=16_384)
    unknown.backend = SimpleNamespace()
    unknown.config.max_tokens = 0
    assert max_output_tokens(unknown) == 0 and compact_summary_output_reserve_tokens(unknown) == 16_384


# LLM: 这是 10-08 事故的算术本身：预留按主请求 32000 算时 sol/astra 和 MiniMax 的 90% 触发线都高于单次预算。
# 函数用途: 钉住三个真实窗口下"触发线 ≤ 单次缓存面预算"，以及旧口径（预留 0 = 沿用 32000）确实装不下。
@pytest.mark.parametrize("window", [SOL_WINDOW, MINIMAX_WINDOW])
def test_ninety_percent_trigger_fits_the_single_request_budget(window):
    trigger = int(window * 0.9)
    assert budget_module.compact_cache_surface_budget(_agent(window)) == window - 16_384 >= trigger
    assert budget_module.compact_summary_budget(_agent(window)) == int(window * 0.8) - 16_384
    assert budget_module.compact_cache_surface_budget(_agent(window, reserve=0)) == window - 32_000 < trigger


# 函数用途: 造一段带若干工具结果的可重放来源；每条结果正文长度固定，方便判断谁被换成占位。
def _tool_history(count: int, chars: int = 3_000) -> list[dict]:
    messages = [{"role": "user", "content": [{"type": "text", "text": "开始任务"}]}]
    for index in range(count):
        messages.append({"role": "assistant", "content": [
            {"type": "tool_use", "id": f"call-{index}", "name": "run_command", "input": {"cmd": f"step {index}"}}]})
        messages.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": f"call-{index}", "content": f"output-{index} " + "x" * chars, "is_error": False}]})
    return messages


def test_shrink_replaces_oldest_tool_outputs_and_keeps_the_newest_two():
    messages = _tool_history(5)
    source = CompactMessageSource(lambda: iter(deepcopy(messages)))
    shrunk = shrink_tool_outputs(source, excess_tokens=1)
    assert shrunk is not None and shrunk.replaced == 1
    rows = list(shrunk.source)
    assert rows[2]["content"][0]["content"] == TOOL_OUTPUT_PLACEHOLDER.format(chars=len(messages[2]["content"][0]["content"]))
    assert rows[2]["content"][0]["tool_use_id"] == "call-0"
    assert [row["content"][0]["content"] for row in rows[4::2]] == [row["content"][0]["content"] for row in messages[4::2]]
    assert list(shrunk.source) == rows, "两遍重放必须逐字一致"
    assert messages == _tool_history(5), "原始消息不能被改"
    bigger = shrink_tool_outputs(CompactMessageSource(lambda: iter(deepcopy(messages))), excess_tokens=2_000)
    assert bigger is not None and bigger.replaced == 3
    assert all(row["content"][0]["content"].startswith("output-") for row in list(bigger.source)[8::2]), "最近两条保持原文"
    assert shrink_tool_outputs(CompactMessageSource(lambda: iter(deepcopy(messages))), excess_tokens=10_000) is None


def test_shrink_skips_media_results_and_needs_tool_outputs():
    messages = _tool_history(3)
    messages[2]["content"][0]["content"] = [{"type": "text", "text": "a" * 2_000}, {"type": "image", "source": {"data": "x"}}]
    shrunk = shrink_tool_outputs(CompactMessageSource(lambda: iter(deepcopy(messages))), excess_tokens=1)
    assert shrunk is None, "带图的工具结果不能替换，而唯一可替换的两条属于最近两条"
    plain = [{"role": "user", "content": [{"type": "text", "text": "没有工具结果" * 500}]}]
    assert shrink_tool_outputs(CompactMessageSource(lambda: iter(plain)), excess_tokens=1) is None


# LLM: 单次路径以外的分段合同（test_compact_request_budget）不变：这里只验证“超预算 → 先瘦身 → 仍单次”这一段新链路。
# 函数用途: 超出缓存面预算时先把旧工具输出换占位，仍以一次带工具、auto 的请求发出；省不够才分段。
def test_over_budget_request_shrinks_old_tool_outputs_before_segmenting(monkeypatch):
    agent = _agent(5_000, backend_max_tokens=256, reserve=0)
    messages = _tool_history(6, chars=4_000)
    request = AuxiliaryModelCallRequest(
        agent=agent, prompt="请总结历史", system_instruction="稳定系统前缀", tools=[{"name": "run_command"}],
        messages=None, purpose="conversation_compact_summary",
    )
    source = CompactMessageSource(lambda: iter(deepcopy(messages)))
    assert budget_module._request_tokens(request, source) > budget_module.compact_cache_surface_budget(agent)
    calls = []
    monkeypatch.setattr(budget_module, "generate_auxiliary_model_response",
                        lambda r: calls.append(r) or ModelResponse(text="摘要", backend="fake"))
    response = budget_module.generate_bounded_compact_response(request, message_source=source)
    assert response.text == "摘要"
    assert len(calls) == 1 and calls[0].tools == request.tools and calls[0].tool_choice.mode == "auto"
    sent = calls[0].messages
    assert sent[-1]["content"][0]["content"].startswith("output-5 ") and sent[-3]["content"][0]["content"].startswith("output-4 ")
    assert sent[2]["content"][0]["content"].startswith("[compact-omitted-tool-output chars=")
    assert budget_module._request_tokens(calls[0]) <= budget_module.compact_cache_surface_budget(agent)
    assert list(source) == messages, "原始来源不能被瘦身改掉（分段路径还要用它）"


def test_shrink_that_cannot_fit_falls_back_to_segments(monkeypatch):
    agent = _agent(1_200, backend_max_tokens=128, reserve=0)
    messages = _tool_history(2, chars=2_000)
    request = AuxiliaryModelCallRequest(
        agent=agent, prompt="请总结历史", system_instruction="稳定系统前缀", tools=[{"name": "run_command"}],
        messages=None, purpose="conversation_compact_summary",
    )
    calls = []
    monkeypatch.setattr(budget_module, "generate_auxiliary_model_response",
                        lambda r: calls.append(r) or ModelResponse(text="段落摘要", backend="fake"))
    budget_module.generate_bounded_compact_response(request, message_source=CompactMessageSource(lambda: iter(deepcopy(messages))))
    assert calls and all(call.tools == [] and call.tool_choice.mode == "none" for call in calls), "最近两条不能换，省不够只能分段"


# 函数用途: 造一个不联网的真实适配器：记录序列化载荷，返回合成回复。
def _capturing_backend(name: str, **overrides):
    config = AgentConfig(model_backend=name, api_key="fake", stream_enabled=False, **overrides)
    backend = get_backend(name, config)
    payloads = []

    def request_json(_path, payload, _headers, **_options):
        payloads.append(deepcopy(payload))
        if name == "openai_responses":
            return {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": "好"}]}]}
        return {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}

    backend.request_json = request_json
    thread = SimpleNamespace(reasoning_effort="xhigh")
    agent = SimpleNamespace(backend=backend, config=config, conversation_store=SimpleNamespace(
        threads=SimpleNamespace(load=lambda requested: thread if requested == "thread-fit" else None)))
    return backend, agent, payloads


def _compact_request(agent, purpose: str = "conversation_compact_summary"):
    return AuxiliaryModelCallRequest(
        agent=agent, prompt=CacheStructuredPrompt("稳定前缀", "COMPACT INSTRUCTION"),
        messages=[{"role": "user", "content": [{"type": "text", "text": "历史"}]},
                  {"role": "assistant", "content": [{"type": "text", "text": "回答"}]}],
        tools=[{"name": "read_file", "description": "read", "input_schema": {"type": "object"}}],
        thread_id="thread-fit", purpose=purpose,
    )


def test_compact_call_sends_the_compact_output_cap_on_chat_payload():
    backend, agent, payloads = _capturing_backend("openai_compatible", model_name="deepseek-v4-flash",
                                                  api_base="https://api.deepseek.com", model_context_window_tokens=SOL_WINDOW)
    assert backend.max_tokens == 65_536  # 显式窗口 27.2 万：主请求上限按 min(65536, 窗口/4)
    generate_auxiliary_model_response(_compact_request(agent))
    assert payloads[-1]["max_tokens"] == 16_384
    generate_auxiliary_model_response(_compact_request(agent, purpose="auxiliary"))
    assert payloads[-1]["max_tokens"] == 65_536, "非压缩目的不改输出上限"


@pytest.mark.parametrize(("model", "declared", "expect_item"), [
    ("gpt-6.1-sol", "auto", True),
    ("gpt-6-astra", "auto", True),
    ("gpt-5.5", "auto", False),
    ("gpt-5.5", "on", True),
    ("gpt-6.1-sol", "off", False),
])
def test_responses_compact_downshifts_with_configuration_update_and_keeps_request_effort(model, declared, expect_item):
    backend, agent, payloads = _capturing_backend(
        "openai_responses", model_name=model, api_base="https://api.openai.com/v1", model_reasoning_control="effort",
        model_reasoning_levels=["low", "medium", "high", "xhigh"], model_reasoning_update_items=declared,
    )
    generate_auxiliary_model_response(_compact_request(agent))
    payload = payloads[-1]
    assert payload["reasoning"] == {"effort": "xhigh"}, "请求级档位必须沿线程，前缀才不失配"
    updates = [item for item in payload["input"] if item.get("type") == "configuration_update"]
    if not expect_item:
        assert updates == []
        return
    assert updates == [{"type": "configuration_update", "reasoning": {"effort": "low"}}]
    assert payload["input"][-1] == {"role": "user", "content": "COMPACT INSTRUCTION"}
    assert payload["input"][-2] == updates[0], "降档项紧贴在压缩指令之前、已缓存历史之后"
    assert "max_output_tokens" in payload and payload["max_output_tokens"] == 16_384
    main = backend.generate("普通请求")
    assert main.text == "好" and all(item.get("type") != "configuration_update" for item in payloads[-1]["input"])


def test_responses_compact_without_configured_level_or_with_same_level_adds_no_item():
    backend, agent, payloads = _capturing_backend(
        "openai_responses", model_name="gpt-6.1-sol", api_base="https://api.openai.com/v1", model_reasoning_control="effort",
        model_reasoning_levels=["low", "medium", "high", "xhigh"], memory_compact_reasoning_level="",
    )
    generate_auxiliary_model_response(_compact_request(agent))
    assert all(item.get("type") != "configuration_update" for item in payloads[-1]["input"])
    agent.config = AgentConfig(**{**vars(agent.config), "memory_compact_reasoning_level": "xhigh"})
    generate_auxiliary_model_response(_compact_request(agent))
    assert all(item.get("type") != "configuration_update" for item in payloads[-1]["input"]), "和请求级同档不插项"


def test_memory_settings_parse_the_new_compact_keys():
    assert AgentConfig().memory_compact_summary_max_output_tokens == 16_384
    assert AgentConfig().memory_compact_reasoning_level == "low"
    settings, warnings = normalize_memory_settings({})
    assert settings.memory_compact_summary_max_output_tokens == 16_384 and settings.memory_compact_reasoning_level == "low"
    assert warnings == []
    settings, warnings = normalize_memory_settings({"memory_compact_summary_max_output_tokens": 0,
                                                     "memory_compact_reasoning_level": "medium"})
    assert settings.memory_compact_summary_max_output_tokens == 0 and settings.memory_compact_reasoning_level == "medium"
    assert warnings == []
    settings, warnings = normalize_memory_settings({"memory_compact_summary_max_output_tokens": -5})
    assert settings.memory_compact_summary_max_output_tokens == 16_384
    assert [item.field_name for item in warnings] == ["memory_compact_summary_max_output_tokens"]


def test_session_cache_percent_is_cumulative_and_only_shown_when_reported(tmp_path):
    agent, params, clock, _runtime, _thread = metrics_fixture(tmp_path)
    response = metrics_settled(agent, params, clock)
    metrics = publish_model_metrics(agent, params, pending=False, tool_count=1, response=response, call_id="call-1")
    assert metrics["cache_percent"] == 75 and metrics["cache_percent_session"] == 75
    assert metrics["cache_read_input_tokens"] == 750 and metrics["cache_read_reported_calls"] == 1
    text = "".join(piece for _, piece in render_model_metrics(metrics, 200)[0])
    assert "缓存 会话 75% · 最近 75%" in text
    cold = metrics_settled(agent, params, clock, call_id="call-2", usage={"prompt_tokens": 1000, "completion_tokens": 50,
                                                                      "prompt_tokens_details": {"cached_tokens": 0}})
    later = publish_model_metrics(agent, params, pending=False, tool_count=1, response=cold, call_id="call-2")
    assert later["cache_percent"] == 0 and later["cache_percent_session"] == 37.5
    assert public_model_metrics({"schema": "model_runtime_metrics.v1", "input_tokens": 100})["cache_percent_session"] is None
