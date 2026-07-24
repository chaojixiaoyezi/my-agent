"""Bug #1 钉子:MiniMax(native) 写长文档 content 被 max_tokens/SSE 截断的修复链。

复现根因:长 content 的 tool_use 参数 JSON 被截断 → 半截 JSON 被静默吞成 {} →
write_file 收到空参 → TOOL_PARAMETER_REQUIRED → 主代理反复重生成又截断,死循环无恢复。

三条断言(对应 BUG1_native_empty_args_analysis.md 验收 #2):
  ① 截断不再被静默当成功:流式半截 input_json_delta(+stop_reason=max_tokens 或提前 EOF)
     → ModelResponse.truncated=True(不再只是无声的 input={})。
  ② 截断触发恢复或降级:截断空参 write_file 激活长内容恢复(分块写);连续若干轮空参/截断
     的 native turn → 运行时降级 text。
  ③ 有限轮后有终止出口:同一截断空参 write_file 连续失败到上限 → 决策给 break(带证据),
     不无限重试。

最高优先级零回归:完整闭合 tool_use(JSON 合法、stop_reason=tool_use/见 message_stop)
必须 truncated=False、行为不变。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams
from agent_py_agent.agent.agent_core.native_tool_protocol import (
    _NATIVE_DOWNGRADE_THRESHOLD,
    native_tool_use_active,
    record_native_turn,
)
from agent_py_agent.agent.agent_core.tool_loop.response_decision import (
    _NATIVE_TRUNCATED_WRITE_LOOP_LIMIT,
    ToolLoopRepairCounters,
    ToolLoopResponseDecisionRequest,
    tool_loop_response_decision,
)
from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.backends.stream_parsers import (
    StreamCompletion,
    anthropic_stream_events,
)
from agent_py_agent.agent.backends.usage_metadata import (
    collect_anthropic_stream_with_completion,
    collect_anthropic_stream_with_tools,
)
from agent_py_agent.agent.tooling.content_recovery_mode import (
    LongContentRecoveryRequest,
    long_content_recovery_context,
)
from agent_py_agent.agent.tooling.models import ToolSpec

# --------------------------------------------------------------------------- #
# SSE 夹具:长 content 的 write_file tool_use 被 max_tokens 截断(半截 input_json)。
# --------------------------------------------------------------------------- #


def _truncated_write_sse_lines() -> list[str]:
    """write_file 的 input JSON 在 content 中途被切断 + stop_reason=max_tokens,无 message_stop。"""
    return [
        json.dumps({"type": "message_start", "message": {"usage": {"input_tokens": 9, "output_tokens": 0}}}),
        json.dumps(
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "tool_use", "id": "call_minimax_1", "name": "write_file", "input": {}},
            }
        ),
        json.dumps(
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "input_json_delta", "partial_json": '{"path": "report.md", "content": "# 长篇报告\\n第一章 '}}
        ),
        json.dumps(
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "input_json_delta", "partial_json": "正文很长很长" * 20}}
        ),
        # 被 max_tokens 切断:content_block_stop 出现但 JSON 没闭合,随后 message_delta=max_tokens,
        # 没有 message_stop(对端冲完部分缓冲就停)。
        json.dumps({"type": "content_block_stop", "index": 0}),
        json.dumps({"type": "message_delta", "delta": {"stop_reason": "max_tokens"}, "usage": {"output_tokens": 1024}}),
    ]


def _complete_write_sse_lines() -> list[str]:
    """对照组:JSON 完整闭合 + stop_reason=tool_use + message_stop(正常路径)。"""
    return [
        json.dumps({"type": "message_start", "message": {"usage": {"input_tokens": 9, "output_tokens": 0}}}),
        json.dumps(
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "tool_use", "id": "call_ok_1", "name": "write_file", "input": {}},
            }
        ),
        json.dumps(
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "input_json_delta", "partial_json": '{"path": "ok.md", "content": "done"}'}}
        ),
        json.dumps({"type": "content_block_stop", "index": 0}),
        json.dumps({"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 12}}),
        json.dumps({"type": "message_stop"}),
    ]


# --------------------------------------------------------------------------- #
# 断言①:截断不再被静默当成功。
# --------------------------------------------------------------------------- #


def test_truncated_stream_is_flagged_not_silently_empty():
    text, usage, blocks, completion = collect_anthropic_stream_with_completion(_truncated_write_sse_lines())

    assert completion.truncated is True, "max_tokens + 未闭合 tool 参数 = 截断,必须标记"
    # input 仍是合法 dict(下游契约不变),但截断信号已独立带出,不再是无声的 {}。
    assert blocks[0]["name"] == "write_file"
    assert blocks[0]["input"] == {}  # 半截 JSON 解析失败回空 dict,但有 truncated 标志兜底
    del text, usage


def test_eof_before_message_stop_is_truncated():
    # 流在 message_stop / stop_reason 之前就 EOF(代理截断)→ 截断(对照 claw 的 not(saw_stop or stop_reason))。
    lines = [
        json.dumps({"type": "message_start", "message": {"usage": {"input_tokens": 3}}}),
        json.dumps(
            {"type": "content_block_start", "index": 0,
             "content_block": {"type": "tool_use", "id": "c", "name": "write_file", "input": {}}}
        ),
        json.dumps(
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "input_json_delta", "partial_json": '{"path": "a.md"'}}
        ),
        # 没有 content_block_stop / message_delta / message_stop → EOF。
    ]
    _t, _u, _b, completion = collect_anthropic_stream_with_completion(lines)
    assert completion.truncated is True


def test_complete_tool_use_is_not_truncated_zero_regression():
    text, usage, blocks, completion = collect_anthropic_stream_with_completion(_complete_write_sse_lines())

    assert completion.truncated is False, "完整闭合 + message_stop 绝不能误判截断(最高优先零回归)"
    assert blocks == [{"id": "call_ok_1", "name": "write_file", "input": {"path": "ok.md", "content": "done"}}]
    del text, usage


def test_complete_text_only_stream_not_truncated():
    # 纯文本、正常 message_stop:truncated=False(text 协议行为不变)。
    lines = [
        json.dumps({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "hi"}}),
        json.dumps({"type": "message_delta", "delta": {"stop_reason": "end_turn"}}),
        json.dumps({"type": "message_stop"}),
    ]
    _t, _u, _b, completion = collect_anthropic_stream_with_completion(lines)
    assert completion.truncated is False


def test_three_tuple_collector_contract_unchanged():
    # 旧 3 元组 API 必须保持(大量调用方依赖)。
    result = collect_anthropic_stream_with_tools(_complete_write_sse_lines())
    assert isinstance(result, tuple) and len(result) == 3


def test_stream_completion_truncated_property():
    assert StreamCompletion(saw_message_stop=True, stop_reason="tool_use").truncated is False
    assert StreamCompletion(saw_message_stop=False, stop_reason="").truncated is True  # EOF
    assert StreamCompletion(stop_reason="max_tokens", open_tool_buffer=True).truncated is True
    # max_tokens 但无未闭合工具缓冲(纯文本被截)→ 不当工具截断处理(避免误伤文本)。
    assert StreamCompletion(stop_reason="max_tokens", open_tool_buffer=False).truncated is False


# --------------------------------------------------------------------------- #
# 断言②(降级):连续若干轮"有 block 但空参/截断"→ native 运行时降级 text(P1-3 补盲)。
# --------------------------------------------------------------------------- #


def _agent_with_write_file_required() -> SimpleNamespace:
    write_spec = ToolSpec(
        name="write_file",
        category="test",
        description="write",
        use_cases=[],
        avoid_when=[],
        keywords=[],
        parameters={"path": "path", "content": "content"},
        required_parameters=["path", "content"],
    )
    write_tool = SimpleNamespace(spec=write_spec)
    list_spec = ToolSpec(
        name="list_tools",
        category="test",
        description="list",
        use_cases=[],
        avoid_when=[],
        keywords=[],
        parameters={},
    )
    list_tool = SimpleNamespace(spec=list_spec)
    registry = SimpleNamespace(tools={"write_file": write_tool, "list_tools": list_tool})
    config = SimpleNamespace(
        tool_protocol="native", enable_tools=True, model_name="MiniMax-M2.7", tool_protocol_text_models=[]
    )
    return SimpleNamespace(config=config, backend=SimpleNamespace(name="anthropic_compatible"), tools=registry)


def _resp(blocks: list, *, truncated: bool = False) -> SimpleNamespace:
    return SimpleNamespace(tool_use_blocks=blocks, truncated=truncated)


def test_downgrade_on_consecutive_empty_required_write_blocks():
    agent = _agent_with_write_file_required()
    assert native_tool_use_active(agent) is True
    # 有 tool_use block 但 write_file 空 input(截断清空)——过去命中"有 block 就清零"永不降级。
    for _ in range(_NATIVE_DOWNGRADE_THRESHOLD):
        record_native_turn(agent, tools_offered=True, response=_resp([{"id": "x", "name": "write_file", "input": {}}]))
    assert native_tool_use_active(agent) is False, "连续空参 required write 也要累加 streak → 降级"


def test_downgrade_on_consecutive_truncated_turns():
    agent = _agent_with_write_file_required()
    for _ in range(_NATIVE_DOWNGRADE_THRESHOLD):
        record_native_turn(agent, tools_offered=True, response=_resp([{"id": "x", "name": "write_file", "input": {}}], truncated=True))
    assert native_tool_use_active(agent) is False


def test_valid_write_call_resets_streak_no_false_downgrade():
    agent = _agent_with_write_file_required()
    record_native_turn(agent, True, _resp([{"id": "x", "name": "write_file", "input": {}}]))
    record_native_turn(agent, True, _resp([{"id": "x", "name": "write_file", "input": {}}]))
    # 一次真实有参 write → 清零(不误降能正常工作的模型)。
    record_native_turn(agent, True, _resp([{"id": "y", "name": "write_file", "input": {"path": "a", "content": "b"}}]))
    record_native_turn(agent, True, _resp([{"id": "x", "name": "write_file", "input": {}}]))
    record_native_turn(agent, True, _resp([{"id": "x", "name": "write_file", "input": {}}]))
    assert native_tool_use_active(agent) is True


def test_empty_input_zero_param_tool_does_not_downgrade():
    # 合法 0 必填工具空 input 是正确调用,不算空转(排除误降)。
    agent = _agent_with_write_file_required()
    for _ in range(_NATIVE_DOWNGRADE_THRESHOLD + 2):
        record_native_turn(agent, True, _resp([{"id": "z", "name": "list_tools", "input": {}}]))
    assert native_tool_use_active(agent) is True


# --------------------------------------------------------------------------- #
# 断言②(恢复) + ③(终止出口):截断空参 write_file → 激活恢复;连续 N 次 → break。
# --------------------------------------------------------------------------- #


def _decision_agent(root: Path, guardrail_records: tuple = ()) -> SimpleNamespace:
    write_spec = SimpleNamespace(required_parameters=["path", "content"])
    registry = SimpleNamespace(tools={"write_file": SimpleNamespace(spec=write_spec)}, workspace_root=root)

    class _Tools:
        workspace_root = root

        def __init__(self) -> None:
            self.parsed_texts: list[str] = []

        def parse_tool_calls(self, text: str):
            self.parsed_texts.append(text)
            return []

    config = SimpleNamespace(
        enable_tools=True, tool_protocol="native", model_name="MiniMax-M2.7", tool_protocol_text_models=[]
    )
    agent = SimpleNamespace(
        backend=SimpleNamespace(name="anthropic_compatible"),
        config=config,
        root=root,
        tools=_Tools(),
    )
    # native_tool_use_active 还会查 agent.tools.specs?不需要;它只看 config/backend/downgrade。
    agent._tool_call_guardrail_records = guardrail_records
    # 给 spec 查询用的真实 registry(_tool_has_required_parameters 走 agent.tools.tools)。
    agent.tools.tools = registry.tools  # type: ignore[attr-defined]
    return agent


def _params() -> ToolLoopExecuteParams:
    return ToolLoopExecuteParams(
        user_prompt="写长报告",
        memories=[],
        runtime_injections=[],
        prompt_files=[],
        tool_catalog_section="",
        tool_recommendations_section="",
        tool_context=[],
        effective_on_chunk=None,
        allowed_tools=None,
        write_boundary=None,
        task_attributes={},
        request_id="req-1",
        run_id="run-1",
        task_id="task-1",
        one_shot_tool_calls=set(),
        executed_tools=[],
        archive_tool_calls=[],
        delivery_contract={},
    )


def _decide(agent, response: ModelResponse, params: ToolLoopExecuteParams):
    return tool_loop_response_decision(
        ToolLoopResponseDecisionRequest(
            agent=agent,
            params=params,
            response=response,
            counters=ToolLoopRepairCounters(),
        )
    )


def _truncated_empty_write_response() -> ModelResponse:
    # flatten 后空参 write_file(path/content 都丢) + truncated。
    return ModelResponse(
        text="",
        backend="anthropic_compatible",
        tool_use_blocks=[{"id": "call_minimax_1", "name": "write_file", "input": {}}],
        truncated=True,
    )


def _failure_record() -> dict:
    return {"tool_name": "write_file", "args_hash": "h", "failed": True, "failure_class": "code:TOOL_PARAMETER_REQUIRED"}


def test_truncated_empty_write_activates_recovery_continue(tmp_path: Path):
    agent = _decision_agent(tmp_path)
    params = _params()
    decision = _decide(agent, _truncated_empty_write_response(), params)

    assert decision.action == "continue", "首次截断空参 write → 激活恢复并 continue(不死循环)"
    joined = "\n".join(params.tool_context)
    assert "截断" in joined and "分" in joined, "恢复指令必须提示分块写"
    # 没退回文本解析(native 路径)。
    assert agent.tools.parsed_texts == []


def test_truncated_empty_write_breaks_after_limit(tmp_path: Path):
    # 已有 (LIMIT-1) 次连续同类失败记录在案,本轮第 LIMIT 次 → break 出口。
    prior = tuple(_failure_record() for _ in range(_NATIVE_TRUNCATED_WRITE_LOOP_LIMIT - 1))
    agent = _decision_agent(tmp_path, guardrail_records=prior)
    params = _params()
    decision = _decide(agent, _truncated_empty_write_response(), params)

    assert decision.action == "break", "连续达到上限必须有真正的循环出口"
    assert decision.response is not None
    assert decision.response.runtime_reason == "NATIVE_TRUNCATED_WRITE_LOOP"
    assert decision.response.runtime_status == "unfinished"


def test_non_truncated_empty_write_does_not_trigger_recovery(tmp_path: Path):
    # 没截断(模型纯粹漏参,truncated=False)→ 不进截断恢复分支,走常规执行(交给参数门)。
    agent = _decision_agent(tmp_path)
    params = _params()
    response = ModelResponse(
        text="",
        backend="anthropic_compatible",
        tool_use_blocks=[{"id": "c", "name": "write_file", "input": {}}],
        truncated=False,
    )
    decision = _decide(agent, response, params)
    assert decision.action == "run_tools", "非截断的空参不抢恢复/break(由参数门正常处理)"


def test_complete_write_with_truncated_flag_not_hijacked(tmp_path: Path):
    # 即便 truncated=True,只要 write_file 参数完整(path+content 齐),就不当截断空参处理 → 正常执行。
    agent = _decision_agent(tmp_path)
    params = _params()
    response = ModelResponse(
        text="",
        backend="anthropic_compatible",
        tool_use_blocks=[{"id": "c", "name": "write_file", "input": {"path": "a.md", "content": "x"}}],
        truncated=True,
    )
    decision = _decide(agent, response, params)
    assert decision.action == "run_tools"
    assert decision.calls[0]["path"] == "a.md"


# --------------------------------------------------------------------------- #
# 断言②(恢复触发集):content_recovery_mode 把"截断 + write_file + 缺参"纳入长内容恢复。
# --------------------------------------------------------------------------- #


def test_recovery_context_triggers_on_truncated_write_param_required():
    ctx = long_content_recovery_context(
        LongContentRecoveryRequest(
            payload={"tool": "write_file"},
            result_tool="write_file",
            result_ok=False,
            output="",
            result_error_code="TOOL_PARAMETER_REQUIRED",
            truncated=True,
        )
    )
    assert ctx, "截断+write_file+TOOL_PARAMETER_REQUIRED 必须激活长内容恢复"
    assert "long_content_recovery_mode" in ctx


def test_recovery_context_not_triggered_without_truncation():
    # 同样的 TOOL_PARAMETER_REQUIRED 但未截断 → 不激活(避免把纯粹漏参拽进长内容恢复)。
    ctx = long_content_recovery_context(
        LongContentRecoveryRequest(
            payload={"tool": "write_file"},
            result_tool="write_file",
            result_ok=False,
            output="",
            result_error_code="TOOL_PARAMETER_REQUIRED",
            truncated=False,
        )
    )
    assert ctx == ""


def test_recovery_context_ok_result_never_triggers():
    ctx = long_content_recovery_context(
        LongContentRecoveryRequest(
            payload={"tool": "write_file"},
            result_tool="write_file",
            result_ok=True,
            output="ok",
            truncated=True,
        )
    )
    assert ctx == ""


# --------------------------------------------------------------------------- #
# 生成器 return 值直接可取(供其它消费方)。
# --------------------------------------------------------------------------- #


def test_anthropic_stream_events_returns_completion_via_stopiteration():
    gen = anthropic_stream_events(_truncated_write_sse_lines())
    try:
        while True:
            next(gen)
    except StopIteration as stop:
        completion = stop.value
    assert isinstance(completion, StreamCompletion)
    assert completion.truncated is True
