"""LLM: 「只思考截断降档」的接线与边界补测（mc4 review35 收编）。

覆盖以下必须成立的行为边界：
- 覆盖值绑定逻辑调用：同一次续跑的超时重试保持降档；自然回执/压缩/辅助等其它用途不消费它；
- 记录降档必须以有效出站参数变化为准（Responses 无更高档声明 / Chat budget / Anthropic 预算裁剪），
  且比较用的输出上限与真实出站同源（窗口裁剪、0 表示默认值两种边界）；
- 半截工具输出不算"只思考"（truncated_tool_names 是供应商给出的结构化事实）；
- 开关关闭时对外诊断保持旧结构，旧字段行为与基线一致。

全部用例走真实后端组包（Chat / Responses / Anthropic），只把网络传输替换为替身；
断言只读结构化事实与真实出站载荷，不解析思考内容。
"""

from __future__ import annotations

from copy import deepcopy

import pytest

from agent_py_agent.agent.agent_core import _tool_loop_service as loop
from agent_py_agent.agent.agent_core.runtime.loop_support import _tool_runtime_evidence
from agent_py_agent.agent.agent_core.tool_loop.response_decision import (
    ToolLoopRepairCounters,
    _no_tool_calls_decision,
    _NoToolCallsRequest,
)
from agent_py_agent.agent.backends import ModelResponse, get_backend
from agent_py_agent.agent.backends.errors import ProviderTimeoutError
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.settings.reasoning_effort import next_call_reasoning_level
from agent_py_agent.tests.test_thinking_truncation_downgrade import (
    _loop_params,
    _LoopAgent,
    _patch_loop,
    _RecordingPrompts,
)

# ---------------------------------------------------------------- helpers


# LLM: 三种协议的最小原始回复形态（长度截断 / 正常结束），只保留后端解析需要的结构化字段；
#   正文为空即"只思考"。fixture 不模拟网络，只当替身传输的返回值。
# 函数用途: 构造某个协议下一次模型调用的原始回复体。
def _provider_reply(protocol: str, *, truncated: bool = False, text: str = "") -> dict:
    if protocol == "openai_responses":
        return {
            "id": "response-wiring",
            "status": "incomplete" if truncated else "completed",
            "incomplete_details": {"reason": "max_output_tokens"} if truncated else None,
            "output": (
                [{"type": "reasoning", "id": "r", "summary": [], "encrypted_content": "fixture"}]
                if not text
                else [{"type": "message", "role": "assistant",
                       "content": [{"type": "output_text", "text": text}]}]
            ),
        }
    if protocol == "anthropic_compatible":
        return {
            "stop_reason": "max_tokens" if truncated else "end_turn",
            "content": [
                {"type": "thinking", "thinking": "opaque reasoning", "signature": "sig"},
                *([{"type": "text", "text": text}] if text else []),
            ],
        }
    return {
        "choices": [{
            "finish_reason": "length" if truncated else "stop",
            "message": {"content": text, "reasoning_content": "opaque reasoning"},
        }]
    }


# LLM: 真实后端 + 替身传输的完整工具循环：组包/解析/裁决都走产品代码，只有网络被替换；
#   rows 记录每次真实出站的 payload，供断言"有效参数"而不是配置值。
# 函数用途: 起一次真实后端的工具循环，返回 agent、params、出站载荷与 prompt 记录。
def _real_loop(monkeypatch, protocol: str = "openai_compatible", *, options: dict | None = None):
    options = options or {}
    script = options.get("script")
    level = str(options.get("level", "max"))
    control = str(options.get("control", "effort"))
    downgrade = bool(options.get("downgrade", True))
    max_tokens = int(options.get("max_tokens", 65536))
    # 窗口参与工厂的输出上限换算（effective_max_output_tokens）；不传时保持 AgentConfig 默认。
    window = options.get("window")
    config = AgentConfig(
        model_backend=protocol,
        model_name="wiring-model",
        api_base="https://wiring.invalid/v1",
        api_key="synthetic-fixture",
        stream_enabled=False,
        model_reasoning_control=control,
        model_reasoning_levels=[],
        model_reasoning_effort=level,
        model_truncation_thinking_downgrade=downgrade,
        max_tokens=max_tokens,
        model_context_window_tokens=window,
    )
    backend = get_backend(protocol, config)
    rows: list[dict] = []
    replies = iter(script) if script is not None else None

    def transport(_path, payload, _headers, **_kwargs):
        rows.append(deepcopy(payload))
        reply = next(replies) if replies is not None else _provider_reply(protocol, truncated=True)
        if isinstance(reply, Exception):
            raise reply
        return reply

    backend.request_json = transport
    prompts = _RecordingPrompts()
    agent = _LoopAgent(backend, prompts, downgrade=downgrade)
    agent.config.model_reasoning_effort = level
    agent.config.max_tokens = max_tokens
    agent.config.model_backend = protocol
    agent.config.model_context_window_tokens = window
    params = _loop_params(run_id="wiring-real")
    _patch_loop(monkeypatch, agent)
    return agent, params, rows, prompts


# ---------------------------------------------------------------- 1. 真实组包：降档-恢复全链路

# LLM: 三种协议 + 两档起点的真实组包回归：第 1 枪用户档位、第 2 枪续跑降一档、第 3 枪恢复；
#   断言的是真实出站字段（Chat 的 reasoning_effort / Responses 的 reasoning.effort /
#   Anthropic 的 budget_tokens 或 output_config.effort）。这组用例也是生产接线变异的抓取者。
# 函数用途: 验证真实后端组包下续跑降档、下一次恢复用户档位。
@pytest.mark.parametrize("protocol,case", [
    ("openai_compatible", ("effort", "max", ["max", "high", "max"])),
    ("openai_responses", ("effort", "high", ["high", "medium", "high"])),
    ("anthropic_compatible", ("effort", "max", ["max", "high", "max"])),
    ("anthropic_compatible", ("budget", "high", [12288, 6144, 12288])),
])
def test_real_backend_loop_payload_downgrade_restore(monkeypatch, protocol, case) -> None:
    control, level, expected = case
    agent, params, rows, _ = _real_loop(
        monkeypatch, protocol, options={"control": control, "level": level},
    )
    result = loop.execute_tool_loop(agent, params)

    if protocol == "openai_responses":
        values = [p.get("reasoning", {}).get("effort") for p in rows]
    elif protocol == "anthropic_compatible":
        values = [
            p.get("thinking", {}).get("budget_tokens") if control == "budget"
            else p.get("output_config", {}).get("effort")
            for p in rows
        ]
    else:
        values = [p.get("reasoning_effort") for p in rows]
    assert result.final_response.runtime_reason == "MODEL_RESPONSE_TRUNCATED"
    assert values == expected
    assert params.executed_tools == []
    assert next_call_reasoning_level(params) == ""


# ---------------------------------------------------------------- 2. 覆盖绑定逻辑调用：重试保持

# LLM: 同一次逻辑续跑里，生成层的超时重试（provider_declared / wall_clock 两种阶段）必须保持
#   降档后的强度；只有下一次逻辑调用才恢复。断言真实出站档位序列。
# 函数用途: 验证续跑超时重试不丢降档。
@pytest.mark.parametrize("stage", ["provider_declared", "wall_clock"])
def test_resume_retry_keeps_lower_effort(monkeypatch, stage) -> None:
    script = [
        _provider_reply("openai_compatible", truncated=True),
        ProviderTimeoutError("synthetic timeout", stage=stage),
        _provider_reply("openai_compatible", text="done"),
    ]
    agent, params, rows, _ = _real_loop(monkeypatch, options={"script": script})
    result = loop.execute_tool_loop(agent, params)

    assert result.final_response.text == "done"
    assert [row.get("reasoning_effort") for row in rows] == ["max", "high", "high"]


# ---------------------------------------------------------------- 3. 覆盖绑定用途：其它用途不消费

# LLM: 挂上续跑覆盖后插入一次自然回执轮（isolated 参数视图，与主轮共享同一份 live_archive_state）：
#   回执轮必须既不使用也不清除覆盖，随后的续跑仍拿到降档后的强度。
# 函数用途: 验证自然回执轮不消费续跑覆盖。
def test_interposed_natural_reply_does_not_consume_override(monkeypatch) -> None:
    from agent_py_agent.agent.agent_core.tool_loop.natural_user_reply import (
        queue_natural_user_reply,
    )

    agent, params, rows, _ = _real_loop(monkeypatch, options={"script": [
        _provider_reply("openai_compatible", text="background status"),
    ]})
    response = ModelResponse(
        text="", backend="openai_compatible", truncated=True,
        runtime_status="unfinished", runtime_reason="MODEL_RESPONSE_TRUNCATED",
        runtime_source="model_provider",
    )
    _no_tool_calls_decision(_NoToolCallsRequest(
        agent=agent, params=params, response=response,
        counters=ToolLoopRepairCounters(), has_protected_marker=False,
    ))
    assert next_call_reasoning_level(params) == "high"

    queue_natural_user_reply(params, kind="background_update", facts={"running_count": 1})
    turn = loop.next_tool_loop_model_response(agent, params, 0)

    assert turn.response.text == "background status"
    assert len(rows) == 1
    assert next_call_reasoning_level(params) == "high", "其它用途的调用消费了续跑覆盖"


# LLM: 压缩、辅助调用与子代理都不得消费父轮覆盖：辅助调用后覆盖仍在，子代理继承的是原档位
#   而不是降档值；最后主轮续跑仍拿到降档强度。
# 函数用途: 验证压缩/辅助/子代理三条旁路都不消费父轮覆盖。
def test_compact_auxiliary_and_child_do_not_consume_override(monkeypatch) -> None:
    from agent_py_agent.agent.conversation.auxiliary_model_call import (
        AuxiliaryModelCallRequest,
        generate_auxiliary_model_response,
    )
    from agent_py_agent.agent.settings.reasoning_effort import (
        CHILD_ATTR,
        inherit_reasoning_effort,
    )

    agent, params, rows, _ = _real_loop(monkeypatch, options={"script": [
        _provider_reply("openai_compatible", text="summary"),
        _provider_reply("openai_compatible", text="auxiliary"),
        _provider_reply("openai_compatible", text="resume done"),
    ]})
    response = ModelResponse(
        text="", backend="openai_compatible", truncated=True,
        runtime_status="unfinished", runtime_reason="MODEL_RESPONSE_TRUNCATED",
        runtime_source="model_provider",
    )
    _no_tool_calls_decision(_NoToolCallsRequest(
        agent=agent, params=params, response=response,
        counters=ToolLoopRepairCounters(), has_protected_marker=False,
    ))
    agent._current_run_params = params
    inherited: dict = {}
    inherit_reasoning_effort(inherited, agent)
    assert inherited[CHILD_ATTR]["level"] == "max", "子代理继承的是用户档位而不是降档值"

    for purpose in ["conversation_compact_summary", "wiring_auxiliary"]:
        generate_auxiliary_model_response(AuxiliaryModelCallRequest(
            agent=agent, prompt="synthetic only", thread_id="fixture-thread", purpose=purpose,
        ))
        assert next_call_reasoning_level(params) == "high", f"{purpose} 消费了续跑覆盖"

    assert rows[0]["reasoning_effort"] == "max"
    assert "reasoning_effort" not in rows[1]
    result = loop.execute_tool_loop(agent, params)
    assert result.final_response.text == "resume done"
    assert rows[2]["reasoning_effort"] == "high"
    assert next_call_reasoning_level(params) == ""


# ---------------------------------------------------------------- 4. 有效出站参数：无变化不算降档

# LLM: 三种"前后出站参数完全一样"的场景（Responses 无更高档声明 / Chat budget 只发开关 /
#   Anthropic 预算被输出上限裁剪）必须如实记 no_effective_change，不得记成成功降档。
# 函数用途: 验证记录降档必须以有效出站参数变化为准。
@pytest.mark.parametrize("protocol,control,cap", [
    ("openai_responses", "effort", 65536),
    ("openai_compatible", "budget", 65536),
    ("anthropic_compatible", "budget", 4096),
])
def test_recorded_downgrade_requires_effective_wire_change(monkeypatch, protocol, control, cap) -> None:
    agent, params, rows, _ = _real_loop(
        monkeypatch, protocol, options={"control": control, "max_tokens": cap},
    )
    loop.execute_tool_loop(agent, params)

    trace = params.live_archive_state["truncation_thinking_downgrade_trace"]
    # LLM: 有效参数没变化时每次都如实记 no_effective_change（不消耗唯一机会，所以第二次截断还会再记一条），
    #   但绝不允许出现 "downgraded"——那说明判据失效、又走了"算成功降档"的老路。
    assert trace
    assert all(row["outcome"] == "no_effective_change" for row in trace)
    control_keys = ("reasoning", "reasoning_effort", "thinking", "output_config")
    before = {k: rows[0][k] for k in control_keys if k in rows[0]}
    after = {k: rows[1][k] for k in control_keys if k in rows[1]}
    assert before == after, {"before": before, "after": after, "trace": trace}
    assert next_call_reasoning_level(params) == ""


# LLM: 3502 收口（mc4 review35b）：比较用的上限必须与真实出站同源——工厂按窗口裁剪、0 取默认值，
#   把结果冻结在 backend.max_tokens 上，Anthropic 组包读的就是它；决策层若用配置原始 max_tokens，
#   窗口小于 4×上限时会把不该降的降档当真（白耗唯一机会），配置为 0 时又会把上限当成 0、
#   误判"无变化"挡住本该发生的降档。本用例用真实工厂配置与真实组包复现这两种边界。
# 函数用途: 验证降档比较使用后端实际输出上限（窗口裁剪 / 0 默认值两种边界）。
@pytest.mark.parametrize("configured,window", [(65536, 32768), (0, 128000)])
def test_effective_budget_uses_actual_factory_output_cap(monkeypatch, configured, window) -> None:
    agent, params, rows, _ = _real_loop(
        monkeypatch, "anthropic_compatible",
        options={"control": "budget", "max_tokens": configured, "window": window},
    )
    loop.execute_tool_loop(agent, params)

    budgets = [p.get("thinking", {}).get("budget_tokens") for p in rows]
    trace = params.live_archive_state["truncation_thinking_downgrade_trace"]
    if configured:
        # 窗口裁剪后实际上限 8192：max 与 high 的预算都被夹成 7168，出站参数没有变化，
        # 必须如实记 no_effective_change（不消耗唯一机会，下一次截断还能再试）。
        assert len(set(budgets)) == 1, budgets
        assert trace[0]["outcome"] == "no_effective_change", trace
    else:
        # 配置 0 = 用默认上限：实际出站上限是工厂按窗口算出的 32000，max→high 必须真的降预算。
        assert trace[0]["outcome"] == "downgraded", trace
        assert budgets[1] < budgets[0], budgets


# ---------------------------------------------------------------- 5. 识别边界：半截工具与长度三件套

# LLM: 长度截断、正文为空、但供应商已输出半截工具参数时，后端会丢弃工具块并保留
#   truncated_tool_names；这种响应不算"只思考"，不得降档，也不得写降档 trace。
# 函数用途: 验证半截工具输出不被误判为只思考截断。
def test_partial_tool_output_is_not_thinking_only(monkeypatch) -> None:
    first = {
        "choices": [{
            "finish_reason": "length",
            "message": {
                "content": "",
                "tool_calls": [{
                    "id": "call-partial", "type": "function",
                    "function": {"name": "read_file", "arguments": "{"},
                }],
            },
        }],
    }
    agent, params, rows, _ = _real_loop(monkeypatch, options={"script": [
        first,
        _provider_reply("openai_compatible", text="done"),
    ]})
    loop.execute_tool_loop(agent, params)

    assert [p["reasoning_effort"] for p in rows] == ["max", "max"]
    assert "truncation_thinking_downgrade_trace" not in params.live_archive_state


# LLM: 识别只认长度截断三件套：任何一个字段不是供应商长度截断的归一形态都直接 break，
#   不降档、不写 trace。逐项变异由参数化覆盖。
# 函数用途: 验证长度截断三件套缺一不可。
@pytest.mark.parametrize("field,value", [
    ("truncated", False),
    ("runtime_status", "error"),
    ("runtime_reason", "MODEL_STREAM_INCOMPLETE"),
    ("runtime_source", "tool_loop"),
])
def test_detection_requires_each_provider_length_field(field, value) -> None:
    from dataclasses import replace

    from agent_py_agent.tests.test_thinking_truncation_downgrade import _request

    request, params = _request()
    request = replace(request, response=replace(request.response, **{field: value}))
    decision = _no_tool_calls_decision(request)

    assert decision.action == "break"
    assert next_call_reasoning_level(params) == ""
    assert "truncation_thinking_downgrade_trace" not in params.live_archive_state


# LLM: "零可见正文"只看 text.strip() 是否为空，不解析思考内容；空白字符也算空。
# 函数用途: 验证识别用可见正文空判定而不是含义判定。
@pytest.mark.parametrize("text,expected", [("", "high"), (" \n\t", "high"), ("visible", "")])
def test_detection_uses_visible_text_emptiness_not_meaning(text, expected) -> None:
    from agent_py_agent.tests.test_thinking_truncation_downgrade import _request

    request, params = _request(text=text)
    _no_tool_calls_decision(request)
    assert next_call_reasoning_level(params) == expected


# ---------------------------------------------------------------- 6. 开关关闭：对外诊断保持旧结构

# LLM: 开关关闭下走三次只思考截断裁决，返回对外诊断与内部计数的结构化快照。
# 函数用途: 构造开关关闭的 OFF 基线快照。
def _off_snapshot() -> dict:
    from dataclasses import asdict
    from types import SimpleNamespace

    from agent_py_agent.agent.settings.reasoning_effort import request_reasoning_options

    agent = SimpleNamespace(
        config=SimpleNamespace(
            enable_tools=True,
            model_reasoning_effort="max",
            model_truncation_thinking_downgrade=False,
        ),
        backend=SimpleNamespace(reasoning_control="effort"),
    )
    params = SimpleNamespace(
        context_scope="task_local", task_attributes={}, live_archive_state={},
        tool_context=[], executed_tools=[],
    )
    response = ModelResponse(
        text="", backend="openai_compatible", truncated=True,
        runtime_status="unfinished", runtime_reason="MODEL_RESPONSE_TRUNCATED",
        runtime_source="model_provider",
    )
    counters = ToolLoopRepairCounters()
    actions, efforts = [], []
    for _ in range(3):
        efforts.append(request_reasoning_options(agent, params, agent.backend, forced=False))
        decision = _no_tool_calls_decision(_NoToolCallsRequest(
            agent=agent, params=params, response=response,
            counters=counters, has_protected_marker=False,
        ))
        actions.append(decision.action)
        counters = decision.counters
    return {
        "actions": actions,
        "efforts": efforts,
        "counters": asdict(counters),
        "state": params.live_archive_state,
        "diagnostics": _tool_runtime_evidence(
            None, None, None, params.live_archive_state, response,
        ),
    }


# LLM: 开关关闭 = 对外完全是旧行为：诊断不得出现新字段、请求档位一次都不降、动作序列与基线一致；
#   内部 counters 保留新字段作为兼容升级（恒为默认 0），旧字段值不受影响——这是交接写明的兼容口径。
# 函数用途: 验证 OFF 的对外诊断保持旧结构、旧行为不变。
def test_switch_off_keeps_old_diagnostics_and_counter_compatibility() -> None:
    snapshot = _off_snapshot()

    assert snapshot["actions"] == ["continue", "continue", "break"]
    assert snapshot["efforts"] == [(False, "max")] * 3
    assert "truncation_thinking_downgrades" not in snapshot["diagnostics"]
    assert snapshot["counters"]["truncation_thinking_downgrades"] == 0
    assert snapshot["counters"]["truncated_output_repairs"] == 2
