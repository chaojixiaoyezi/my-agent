
from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from ...tooling.models import (
    ToolFailureStage,
    ToolHandlerOutcome,
    apply_tool_execution_facts,
)
from ..runner.context import current_subagent_run_id
from ..tool_loop.round_execution import ToolCallExecuteParams
from .agent_budget import (
    ToolAgentBudgetRequest,
    check_tool_agent_budget,
    check_unknown_command_budget,
)


@dataclass(frozen=True)
class ToolAgentBudgetStageRequest:
    __test__: ClassVar[bool] = False

    agent: object
    execute_request: ToolCallExecuteParams
    payload: object


def maybe_block_tool_agent_budget(request: ToolAgentBudgetStageRequest) -> ToolHandlerOutcome | None:
    execute_request = request.execute_request
    tool_name = _tool_name(request.payload)
    command_text = _command_argument(request.payload)
    if command_text is not None:
        result = check_unknown_command_budget(
            request.agent,
            tool_name,
            command_text,
        )
        if result is None:
            return None
        apply_tool_execution_facts(
            result,
            failure_stage=ToolFailureStage.RUNTIME_GATE,
            handler_executed=False,
        )
        return result
    result = check_tool_agent_budget(
        ToolAgentBudgetRequest(
            agent=request.agent,
            run_id=_runtime_run_id(request.agent, execute_request),
            tool_name=tool_name,
        )
    )
    if result is None:
        return None
    apply_tool_execution_facts(
        result,
        failure_stage=ToolFailureStage.RUNTIME_GATE,
        handler_executed=False,
    )
    return result


def _runtime_run_id(agent: object, request: ToolCallExecuteParams) -> str:
    return str(current_subagent_run_id(agent) or request.params.run_id or "")


def _tool_name(payload: object) -> str:
    if isinstance(payload, dict):
        return str(payload.get("tool") or "unknown")
    return "unknown"


def _command_argument(payload: object) -> object | None:
    """run_command 才有 command 参数；其他工具走总预算闸，不走 unknown 命令额度。"""
    if isinstance(payload, dict) and _tool_name(payload) == "run_command":
        return payload.get("command")
    return None
