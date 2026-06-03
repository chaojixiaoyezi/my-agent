
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import ClassVar

from ...backends import ModelResponse
from ...tools import ToolExecutionResult
from .._runtime_params import ToolLoopExecuteParams
from .round_context_archive import append_assistant_tool_round_context
from .round_subagent_output import (
    SubagentOutputWriteCheck,
    is_subagent_output_json_write,
    subagent_output_json_response,
)

_STATEFUL_ORCHESTRATION_TOOLS = {
    "create_subagents",
    "schedule_child_subagents",
}
_DEPENDENT_ORCHESTRATION_TOOLS = {
    "create_subagents",
    "dispatch_subagents",
    "inspect_agent_tree",
    "schedule_child_subagents",
    "send_guidance",
}


@dataclass(frozen=True)
class ToolCallRecordParams:
    __test__: ClassVar[bool] = False

    params: ToolLoopExecuteParams
    tool_rounds: int
    idx: int
    payload: object
    result: ToolExecutionResult


@dataclass(frozen=True)
class ToolCallExecuteParams:
    __test__: ClassVar[bool] = False

    params: ToolLoopExecuteParams
    tool_rounds: int
    idx: int
    payload: object


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


def execute_tool_round(request: ToolRoundExecutionRequest) -> bool:
    append_assistant_tool_round_context(request)
    subagent_output_written = False
    stateful_orchestration_seen = False
    for idx, payload in enumerate(request.calls, start=1):
        tool_name = _tool_name(payload)
        if stateful_orchestration_seen and tool_name in _DEPENDENT_ORCHESTRATION_TOOLS:
            result = _deferred_orchestration_result(tool_name)
        else:
            result = request.execute_one(
                ToolCallExecuteParams(request.params, request.tool_rounds, idx, payload)
            )
        request.record_one(ToolCallRecordParams(request.params, request.tool_rounds, idx, payload, result))
        subagent_output_written = subagent_output_written or is_subagent_output_json_write(
            SubagentOutputWriteCheck(request.agent, request.params, payload, result)
        )
        stateful_orchestration_seen = (
            stateful_orchestration_seen or tool_name in _STATEFUL_ORCHESTRATION_TOOLS
        )
    return subagent_output_written


def _tool_name(payload: object) -> str:
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("tool") or "").strip()


def _deferred_orchestration_result(tool_name: str) -> ToolExecutionResult:
    return ToolExecutionResult(
        tool_name or "unknown",
        False,
        "同一轮已经执行过会创建或改变子代理树的工具调用，"
        "后续编排工具已延后。请先读取上一条工具的真实输出，"
        "下一轮再使用返回的 created_run_ids/actionable_run_ids 调用 dispatch_subagents。",
    )
