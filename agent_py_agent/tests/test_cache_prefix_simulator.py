"""前缀缓存模拟器（cache_prefix_simulator.py）的纯单测（cachesim2）。

为什么单独一个文件：模拟器是纯函数 + 一个进程内类，可以脱离真实链路直接钉住规则；
端到端护栏（test_cache_prefix_regression.py）负责真实回合，这里负责模拟器本身的两条新规则——
思考模式下的 tool_choice 限制、tool_choice=none 不渲染工具定义（都来自 3a 官网实测，见
decision-evidence/deepseek-cache-audit-1004/ds_probe4.out.txt / ds_probe5.out.txt）。
"""

from __future__ import annotations

import json

from agent_py_agent.tests.cache_prefix_simulator import (
    TOOL_CHOICE_MESSAGE,
    PrefixCacheSimulator,
    prefix_units,
    unsupported_tool_choice,
)

MESSAGES = [{"role": "user", "content": "看一下工作区。"}]
TOOLS = [{"type": "function", "function": {"name": "list_files", "parameters": {"type": "object"}}}]
NAMED_CHOICE = {"type": "function", "function": {"name": "list_files"}}


# 函数用途: 造一个最小可判定的请求 payload。
def _payload(*, tool_choice: object = "auto", tools: list | None = TOOLS) -> dict:
    payload = {"model": "deepseek-v4-flash", "messages": list(MESSAGES), "tool_choice": tool_choice}
    if tools is not None:
        payload["tools"] = tools
    return payload


# 函数用途: 思考模式只认 auto / none；required 与指定工具名都会被拒（含结构化判定本身）。
def test_thinking_mode_rejects_required_and_named_tool_choice():
    assert unsupported_tool_choice(_payload(tool_choice="required"))
    assert unsupported_tool_choice(_payload(tool_choice=NAMED_CHOICE))
    assert not unsupported_tool_choice(_payload(tool_choice="auto"))
    assert not unsupported_tool_choice(_payload(tool_choice="none"))
    assert not unsupported_tool_choice(_payload(tool_choice=None))

    simulator = PrefixCacheSimulator()
    for choice in ("required", NAMED_CHOICE):
        response, record = simulator.serve(_payload(tool_choice=choice))
        assert record.rejected, f"tool_choice={choice!r} 在思考模式下没有被拒"
        assert response["error"]["message"] == TOOL_CHOICE_MESSAGE, response
    # auto / none 正常接受，且被拒的请求不进命中表。
    for choice in ("auto", "none"):
        response, record = simulator.serve(_payload(tool_choice=choice))
        assert not record.rejected and "error" not in response, (choice, response)


# 函数用途: none 的前缀等于“不带 tools”的请求；auto 的前缀多一条 tools，两者在 system 之后分叉。
def test_none_tool_choice_prefix_equals_a_request_without_tools():
    none_units = prefix_units(_payload(tool_choice="none"))
    bare_units = prefix_units({"model": "deepseek-v4-flash", "messages": list(MESSAGES)})
    assert none_units == bare_units, "none 的前缀应当和不带 tools 的请求逐条相同"
    auto_units = prefix_units(_payload(tool_choice="auto"))
    assert len(auto_units) == len(none_units) + 1 and auto_units != none_units

    # 端到端：auto 先建立前缀，none 只能命中 system 那一条（system 之后分叉）；
    # 反过来，不带 tools 的请求能完整命中 none 留下的缓存。
    simulator = PrefixCacheSimulator()
    simulator.serve(_payload(tool_choice="auto"))
    _, record = simulator.serve(_payload(tool_choice="none"))
    assert record.reused_prefix_units == 1, f"none 不应命中 auto 的 tools 段：{record}"

    simulator = PrefixCacheSimulator()
    simulator.serve(_payload(tool_choice="none"))
    _, record = simulator.serve({"model": "deepseek-v4-flash", "messages": list(MESSAGES)})
    assert record.reused_prefix_units == len(none_units), (
        f"不带 tools 的请求应当完整命中 none 留下的缓存：{record}")


# 函数用途: 关思考分区不受 tool_choice 限制（400 只在思考模式生效），带 required 也能被接受。
def test_tool_choice_restriction_only_applies_to_thinking_mode():
    simulator = PrefixCacheSimulator()
    payload = _payload(tool_choice="required")
    payload["thinking"] = {"type": "disabled"}
    response, record = simulator.serve(payload)
    assert not record.rejected and "error" not in response, response


# 函数用途: 探针形状的请求照常返回结构化工具调用（回归：wire 不因新规则变化）。
def test_wire_still_answers_the_capability_probe():
    simulator = PrefixCacheSimulator()
    request = type("R", (), {"payload": {
        "model": "deepseek-v4-flash",
        "messages": [{"role": "user", "content": "Call my_agent_capability_probe exactly once with nonce abc123."}],
        "tools": [{"type": "function", "function": {"name": "my_agent_capability_probe"}}],
    }})()
    response = simulator.wire(request)
    call = response["choices"][0]["message"]["tool_calls"][0]
    arguments = json.loads(call["function"]["arguments"])
    assert call["function"]["name"] == "my_agent_capability_probe" and arguments["nonce"] == "abc123"
    assert not simulator.calls, "探针请求不应进入缓存命中统计"
