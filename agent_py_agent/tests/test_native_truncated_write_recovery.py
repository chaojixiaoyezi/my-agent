"""Bug #1 钉子:MiniMax(native) 写长文档 content 被 max_tokens/SSE 截断的修复链。

复现根因:长 content 的 tool_use 参数 JSON 被截断 → 半截 JSON 被静默吞成 {} →
write_file 收到空参 → TOOL_PARAMETER_REQUIRED → 主代理反复重生成又截断,死循环无恢复。

三条断言(对应 BUG1_native_empty_args_analysis.md 验收 #2):
  ① 截断不再被静默当成功:流式半截 input_json_delta(+stop_reason=max_tokens 或提前 EOF)
     → ModelResponse.truncated=True(不再只是无声的 input={})。
  ② 截断触发恢复:截断空参 write_file 激活长内容恢复(分块写)，不把正常的无工具回复
     误判成协议不兼容。
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
from agent_py_agent.agent.backends.tool_protocol_adapter import (
    ProviderToolCallRequest,
    canonical_tool_calls_from_response,
)
from agent_py_agent.agent.backends.usage_metadata import (
    collect_anthropic_stream_with_completion,
    collect_anthropic_stream_with_tools,
)
from agent_py_agent.agent.tooling.content_recovery_mode import (
    LongContentRecoveryRequest,
    long_content_recovery_context,
)
from agent_py_agent.tests._tool_runtime_harness import (
    make_test_model_spec,
    make_test_protocol_snapshot,
    runtime_snapshot_for_model_specs,
)

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
    # R231 起坏参数不再猜成 {} 生成候选；R232 把被丢弃的工具名单独带出，
    # 让工具循环仍能识别"这次截断的是 write_file"并给分块恢复。
    assert blocks == [], "半截参数不得生成可执行候选"
    assert completion.incomplete_reason == "invalid_tool_arguments"
    assert completion.tool_names == ("write_file",)
    del text, usage


def test_eof_before_message_stop_is_truncated():
    # 流在 message_stop / stop_reason 之前就 EOF(代理截断)→ 截断(对照 参考实现 的 not(saw_stop or stop_reason))。
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
    # R232: truncated 只表示"这次响应不完整"，长度上限同样是不完整；是否续跑由 typed 原因决定。
    assert StreamCompletion(stop_reason="max_tokens", open_tool_buffer=False).truncated is True
    assert StreamCompletion(saw_message_stop=True, stop_reason="end_turn").truncated is False


# --------------------------------------------------------------------------- #
# 断言②(恢复) + ③(终止出口):截断空参 write_file → 激活恢复;连续 N 次 → break。
# --------------------------------------------------------------------------- #


def _decision_agent(root: Path, guardrail_records: tuple = ()) -> SimpleNamespace:
    config = SimpleNamespace(
        enable_tools=True,
        tool_protocol="native",
        model_name="MiniMax-M2.7",
    )
    agent = SimpleNamespace(
        backend=SimpleNamespace(name="anthropic_compatible"),
        config=config,
        root=root,
    )
    agent._tool_call_guardrail_records = guardrail_records
    return agent


def _params() -> ToolLoopExecuteParams:
    write_spec = make_test_model_spec(
        "write_file",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
    )
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
        tool_runtime_snapshot=runtime_snapshot_for_model_specs(
            (write_spec,),
            run_id="run-1",
        ),
        tool_protocol_snapshot=make_test_protocol_snapshot(
            run_id="run-1",
            source_protocol="native",
        ),
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
    # 截断响应：不提供可执行候选，只带真实工具名（R231 的安全边界 + R232 的恢复事实）。
    return ModelResponse(
        text="",
        backend="anthropic_compatible",
        tool_use_blocks=[],
        truncated=True,
        truncated_tool_names=["write_file"],
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
    assert decision.calls == []


def test_truncated_empty_write_breaks_after_limit(tmp_path: Path):
    # 整轮零执行不再产生 write_file 失败记录，上限改按本轮已发生的分块纠偏次数计：
    # 已有 (LIMIT-1) 次纠偏在案,本轮第 LIMIT 次 → break 出口。
    agent = _decision_agent(tmp_path)
    params = _params()
    counters = ToolLoopRepairCounters(
        truncated_write_repairs=_NATIVE_TRUNCATED_WRITE_LOOP_LIMIT - 1
    )
    decision = tool_loop_response_decision(
        ToolLoopResponseDecisionRequest(
            agent=agent,
            params=params,
            response=_truncated_empty_write_response(),
            counters=counters,
        )
    )

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
    # 未截断的完整写入不得被恢复分支抢走：不带 truncated_tool_names 时决策层只走常规工具路径。
    agent = _decision_agent(tmp_path)
    params = _params()
    response = ModelResponse(
        text="",
        backend="anthropic_compatible",
        tool_use_blocks=[{"id": "c", "name": "write_file", "input": {"path": "a.md", "content": "x"}}],
    )
    decision = _decide(agent, response, params)
    assert decision.action == "run_tools"
    assert decision.calls[0].arguments["path"] == "a.md"
    assert decision.calls[0].source_protocol == "native"


def test_complete_write_with_truncated_flag_stays_zero_execution(tmp_path: Path):
    # R231 的 G5 边界优先：即便参数完整，只要本轮被判定不完整就不执行，也不被改成恢复轮次。
    agent = _decision_agent(tmp_path)
    params = _params()
    response = ModelResponse(
        text="",
        backend="anthropic_compatible",
        tool_use_blocks=[{"id": "c", "name": "write_file", "input": {"path": "a.md", "content": "x"}}],
        truncated=True,
    )
    decision = _decide(agent, response, params)
    # R248：判定不完整仍然整轮零执行；区别是现在会先给一次**轮内**续跑（有界），而不是直接收口。
    # 不变式：续跑指令不得点名任何工具、不得假定上一轮调用了哪个工具（那才是"伪造恢复指令"）。
    assert decision.calls == []
    resumes = [item for item in params.tool_context if "output-limit-resume" in str(item)]
    assert len(resumes) == 1
    for name in ("write_file", "apply_patch", "edit_file"):
        assert name not in resumes[0], f"续跑指令不得点名工具: {name}"


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


# --------------------------------------------------------------------------- #
# R232 端到端:真实 SSE → 适配器 → 决策，恢复链必须可达（不是只测手搓 ModelResponse）。
# --------------------------------------------------------------------------- #


def test_truncated_stream_reaches_recovery_through_real_adapter(tmp_path: Path):
    # LLM: 这条用例守的是 R231 合入时踩过的坑——适配器对不完整响应返回空 calls，
    # 若恢复只认 calls，截断写就永远进不了分块纠偏。必须走真实 SSE 解析与真实适配器。
    # 函数用途: 证明"截断 → 零执行 → 仍给分块恢复"这条链真的可达。
    _text, _usage, _blocks, completion = collect_anthropic_stream_with_completion(
        _truncated_write_sse_lines()
    )
    response = ModelResponse(
        text="",
        backend="anthropic_compatible",
        tool_use_blocks=[],
        truncated=completion.truncated,
        stop_reason=completion.stop_reason,
        truncated_tool_names=list(completion.tool_names),
    )
    agent = _decision_agent(tmp_path)
    params = _params()

    adapted = canonical_tool_calls_from_response(
        ProviderToolCallRequest(
            response=response,
            protocol=params.tool_protocol_snapshot,
            runtime_snapshot=params.tool_runtime_snapshot,
            turn_id="run-1:model:1",
            attempt_id="attempt-1",
        )
    )
    assert adapted.calls == (), "不完整响应整轮零执行是 G5 边界，不能被恢复绕开"

    decision = _decide(agent, response, params)
    assert decision.action == "continue"
    assert decision.calls == []
    joined = "\n".join(params.tool_context)
    assert "截断" in joined
    assert 'mode="append"' in joined, "恢复指令必须真的教模型分块续写，而不是通用格式纠偏"


def test_truncated_stream_recovery_breaks_after_limit(tmp_path: Path):
    # LLM: 零执行流程不再产生 write_file 失败记录，上限改由本轮纠偏计数决定；这条守住硬出口。
    # 函数用途: 证明连续截断最终会给出带证据的终止出口，而不是无限重试。
    _text, _usage, _blocks, completion = collect_anthropic_stream_with_completion(
        _truncated_write_sse_lines()
    )
    response = ModelResponse(
        text="",
        backend="anthropic_compatible",
        tool_use_blocks=[],
        truncated=True,
        stop_reason=completion.stop_reason,
        truncated_tool_names=list(completion.tool_names),
    )
    params = _params()
    decision = tool_loop_response_decision(
        ToolLoopResponseDecisionRequest(
            agent=_decision_agent(tmp_path),
            params=params,
            response=response,
            counters=ToolLoopRepairCounters(
                truncated_write_repairs=_NATIVE_TRUNCATED_WRITE_LOOP_LIMIT - 1
            ),
        )
    )
    assert decision.action == "break"
    assert decision.response is not None
    assert decision.response.runtime_reason == "NATIVE_TRUNCATED_WRITE_LOOP"
