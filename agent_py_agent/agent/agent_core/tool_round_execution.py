# LLM: Tool round execution helpers keep ToolLoopService thin while preserving runner trace contracts.
# 模块用途: 执行一轮模型工具调用、回写 live context，并识别子代理 output.json 收口信号。

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from ..backends import ModelResponse
from ..tools import ToolExecutionResult
from ._runtime_params import ToolLoopExecuteParams
from .tool_call_context_reducer import (
    AssistantToolRoundContextRequest,
    render_assistant_tool_round_context,
)


# LLM: ToolCallRecordParams keeps tool record inputs bundled for trace/archive reducers.
# 类用途: 集中保存工具调用记录字段；调用方用它写 live context、runner trace 和外置工具输出记录。
@dataclass(frozen=True)
class ToolCallRecordParams:
    __test__: ClassVar[bool] = False

    params: ToolLoopExecuteParams
    tool_rounds: int
    idx: int
    payload: object
    result: ToolExecutionResult


# LLM: ToolCallExecuteParams keeps one tool execution request bundled before result recording.
# 类用途: 单次工具调用执行参数包，避免 runner trace 和执行入口继续增加散乱参数。
@dataclass(frozen=True)
class ToolCallExecuteParams:
    __test__: ClassVar[bool] = False

    params: ToolLoopExecuteParams
    tool_rounds: int
    idx: int
    payload: object


# LLM: ToolRoundExecutionRequest bundles callbacks needed to run one model tool round.
# 类用途: 保存一轮工具调用所需上下文和回调；模块本身不持有 agent service 状态。
@dataclass(frozen=True)
class ToolRoundExecutionRequest:
    __test__: ClassVar[bool] = False

    agent: object
    params: ToolLoopExecuteParams
    tool_rounds: int
    response: ModelResponse
    calls: list[dict[str, object]]
    execute_one: Callable[[ToolCallExecuteParams], ToolExecutionResult]
    record_one: Callable[[ToolCallRecordParams], None]


# LLM: execute_tool_round runs all calls in one assistant round and returns whether the runner completed.
# 函数用途: 回写本轮工具上下文、逐个执行和记录工具调用，并检测当前子代理是否写出自己的 output.json。
def execute_tool_round(request: ToolRoundExecutionRequest) -> bool:
    _append_assistant_tool_round_context(request)
    subagent_output_written = False
    for idx, payload in enumerate(request.calls, start=1):
        result = request.execute_one(
            ToolCallExecuteParams(request.params, request.tool_rounds, idx, payload)
        )
        request.record_one(ToolCallRecordParams(request.params, request.tool_rounds, idx, payload, result))
        subagent_output_written = subagent_output_written or _is_subagent_output_json_write(
            request.agent, payload, result
        )
    return subagent_output_written


# LLM: subagent_output_json_response turns the just-written output.json into the final runner contract.
# 函数用途: 读取当前子代理 output.json 并包成 SUBAGENT_RESULT，避免为了收口再发一轮模型请求。
def subagent_output_json_response(agent, fallback: ModelResponse) -> ModelResponse:
    run_id = str(getattr(agent, "_current_subagent_run_id", "") or "")
    try:
        task = agent.subagents.load(run_id)
        payload = json.loads(Path(task.output_json).read_text(encoding="utf-8"))
    except Exception:
        return fallback
    if not isinstance(payload, dict):
        return fallback
    text = (
        "[SUBAGENT_RESULT]\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n"
        "[/SUBAGENT_RESULT]\n\n"
        "系统检测到当前子代理已写出 output.json，已结束工具循环并等待父级验收。"
    )
    return ModelResponse(text=text, backend=fallback.backend)


# LLM: _append_assistant_tool_round_context protects the next live prompt from large tool payloads.
# 函数用途: 把模型刚生成的工具调用摘要写回 tool_context；大正文只保留长度/hash/预览，不反复塞进后续提示词。
def _append_assistant_tool_round_context(request: ToolRoundExecutionRequest) -> None:
    rendered = render_assistant_tool_round_context(
        AssistantToolRoundContextRequest(request.response.text, request.calls)
    )
    request.params.tool_context.append(
        f"[assistant-tool-round-{request.tool_rounds}]\n{rendered}"
    )


# LLM: _is_subagent_output_json_write detects the runner's own structured completion artifact.
# 函数用途: 判断本轮工具调用是否成功写入当前子代理的 output.json；只用于提前收敛 runner。
def _is_subagent_output_json_write(agent, payload: object, result: ToolExecutionResult) -> bool:
    if not (result.ok and result.tool == "write_file" and isinstance(payload, dict)):
        return False
    run_id = str(getattr(agent, "_current_subagent_run_id", "") or "")
    if not run_id:
        return False
    try:
        task = agent.subagents.load(run_id)
    except Exception:
        return False
    return _same_path(payload.get("path"), getattr(task, "output_json", ""))


# LLM: _same_path compares model paths as filesystem literals without glob behavior.
# 函数用途: 将工具入参路径和任务 output_json 路径解析后比较，无法解析时保守返回 False。
def _same_path(left: object, right: object) -> bool:
    left_text = str(left or "").strip()
    right_text = str(right or "").strip()
    if not left_text or not right_text:
        return False
    try:
        return Path(left_text).expanduser().resolve() == Path(right_text).expanduser().resolve()
    except OSError:
        return False
