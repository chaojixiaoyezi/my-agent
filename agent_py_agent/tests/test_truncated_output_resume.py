"""LLM: 输出上限截断后的轮内续跑合同单测（R248）。

真机证据：两个子代理分别被 max-tokens 截断 3 次和 4 次，每次都靠管理器**重启整个 runner 轮次**
重试同样策略，共耗 4/5 个 attempt、约 35 分钟才完成。恢复机制必须先在轮内把断点续写指令回灌，
超限才按 unfinished 收口。

规则：只按结构化 truncated 事实判定；不解析正文；有界（默认 2 次）；不改主/子代理的既有收口语义。

2026-10-05 真机 NOV02-ds-t2：供应商长度截断的真实响应一定带 runtime 三件套
（unfinished / MODEL_RESPONSE_TRUNCATED / model_provider），会在通用 runtime-status break
之前被拦下，轮内续跑对真实截断 0 次可达；修复后同一预算对 provider 形态生效
（见 test_provider_shaped_* 三条）。
"""

from __future__ import annotations

import types

import pytest

from agent_py_agent.agent.agent_core.tool_loop.response_decision import (
    _TRUNCATED_OUTPUT_RESUME_COUNT,
    ToolLoopRepairCounters,
    _no_tool_calls_decision,
    _NoToolCallsRequest,
)
from agent_py_agent.agent.backends import ModelResponse


def _request(*, truncated: bool, counters: ToolLoopRepairCounters | None = None):
    params = types.SimpleNamespace(
        context_scope="task_local",
        task_attributes={},
        live_archive_state={},
        tool_context=[],
        executed_tools=[],
    )
    response = ModelResponse(
        text="我已经把第一段写完了，接下来还有两段",
        backend="openai_compatible",
        truncated=truncated,
    )
    return _NoToolCallsRequest(
        agent=types.SimpleNamespace(config=types.SimpleNamespace(enable_tools=True)),
        params=params,
        response=response,
        counters=counters or ToolLoopRepairCounters(),
        has_protected_marker=False,
    ), params


# LLM: 被截断的最终答复不能当完成；必须先把续写指令回灌并再走一轮。
# 函数用途: 验证首次截断返回 continue 且写下续跑指令。
def test_truncated_final_resumes_in_turn() -> None:
    request, params = _request(truncated=True)
    decision = _no_tool_calls_decision(request)
    assert decision.action == "continue"
    assert any("output-limit-resume" in str(item) for item in params.tool_context)
    assert decision.counters.truncated_output_repairs == 1


# LLM: 续跑必须有界：超限后仍按 unfinished 收口，不能变成新的死循环。
# 函数用途: 验证达到上限后返回 break 并标记 MODEL_RESPONSE_TRUNCATED。
def test_truncated_final_stops_after_limit() -> None:
    counters = ToolLoopRepairCounters(
        truncated_output_repairs=_TRUNCATED_OUTPUT_RESUME_COUNT
    )
    request, params = _request(truncated=True, counters=counters)
    decision = _no_tool_calls_decision(request)
    assert decision.action == "break"
    assert decision.response.runtime_status == "unfinished"
    assert decision.response.runtime_reason == "MODEL_RESPONSE_TRUNCATED"
    assert params.tool_context == []


# LLM: 未截断的正常收口一个字节都不改，避免续跑逻辑误伤普通回合。
# 函数用途: 验证未截断时仍直接 break。
def test_untruncated_final_breaks_unchanged() -> None:
    request, params = _request(truncated=False)
    decision = _no_tool_calls_decision(request)
    assert decision.action == "break"
    assert decision.response.runtime_status == "ok"
    assert params.tool_context == []


def _provider_request(
    *,
    text: str,
    counters: ToolLoopRepairCounters | None = None,
    runtime_status: str = "unfinished",
    runtime_reason: str = "MODEL_RESPONSE_TRUNCATED",
):
    """构造供应商归一后的真实截断响应（带 runtime 三件套，与 response_completion 同形）。"""
    params = types.SimpleNamespace(
        context_scope="task_local",
        task_attributes={},
        live_archive_state={},
        tool_context=[],
        executed_tools=[],
    )
    response = ModelResponse(
        text=text,
        backend="openai_compatible",
        truncated=True,
        runtime_status=runtime_status,
        runtime_reason=runtime_reason,
        runtime_source="model_provider",
    )
    return _NoToolCallsRequest(
        agent=types.SimpleNamespace(config=types.SimpleNamespace(enable_tools=True)),
        params=params,
        response=response,
        counters=counters or ToolLoopRepairCounters(),
        has_protected_marker=False,
    ), params


# LLM: 真实供应商截断响应带 runtime 三件套，必须在通用 runtime-status break 之前进入同一条续跑预算；
#   纯思考顶满（无正文无工具）与有正文被截断两种形态都要能续跑。
# 函数用途: 验证 provider 形态的长度截断返回 continue、写下续跑指令、计数 +1。
@pytest.mark.parametrize("text", ["", "我已经把第一段写完了，接下来还有两段"])
def test_provider_shaped_truncation_resumes_in_turn(text: str) -> None:
    request, params = _provider_request(text=text)
    decision = _no_tool_calls_decision(request)
    assert decision.action == "continue"
    instructions = [str(item) for item in params.tool_context]
    assert any("output-limit-resume" in item for item in instructions)
    assert any("不要再展开长推理" in item for item in instructions)
    assert decision.counters.truncated_output_repairs == 1


# LLM: 续跑有界：provider 形态达上限后按原终态收口，runtime 字段原样保留，不再追加指令。
# 函数用途: 验证 provider 形态超限返回 break 且不丢供应商终态。
def test_provider_shaped_truncation_stops_after_limit() -> None:
    counters = ToolLoopRepairCounters(truncated_output_repairs=_TRUNCATED_OUTPUT_RESUME_COUNT)
    request, params = _provider_request(text="", counters=counters)
    decision = _no_tool_calls_decision(request)
    assert decision.action == "break"
    assert decision.response.runtime_status == "unfinished"
    assert decision.response.runtime_reason == "MODEL_RESPONSE_TRUNCATED"
    assert decision.response.runtime_source == "model_provider"
    assert params.tool_context == []


# LLM: 其它 provider 终态（断流等）不得吃截断续跑预算；截断标志也在，仍按 status/reason 拒绝续跑。
# 函数用途: 验证非长度 runtime 状态仍立即 break、零续跑、零计数。
def test_other_runtime_status_breaks_without_truncation_resume() -> None:
    request, params = _provider_request(
        text="", runtime_status="error", runtime_reason="MODEL_STREAM_INCOMPLETE",
    )
    decision = _no_tool_calls_decision(request)
    assert decision.action == "break"
    assert decision.counters.truncated_output_repairs == 0
    assert params.tool_context == []
