
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
from .agent_budget import ToolAgentBudgetRequest, check_tool_agent_budget


@dataclass(frozen=True)
class ToolAgentBudgetStageRequest:
    __test__: ClassVar[bool] = False

    agent: object
    execute_request: ToolCallExecuteParams
    payload: object


def maybe_block_tool_agent_budget(request: ToolAgentBudgetStageRequest) -> ToolHandlerOutcome | None:
    execute_request = request.execute_request
    result = check_tool_agent_budget(
        ToolAgentBudgetRequest(
            agent=request.agent,
            run_id=_runtime_run_id(request.agent, execute_request),
            tool_name=_tool_name(request.payload),
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
