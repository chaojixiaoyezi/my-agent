
from __future__ import annotations

from ..._runtime_params import ToolLoopExecuteParams

_ORCHESTRATION_TOOLS = {
    "create_subagents",
    "dispatch_subagents",
    "schedule_child_subagents",
}


def executed_subagent_orchestration(params: ToolLoopExecuteParams) -> bool:
    return any(str(item or "") in _ORCHESTRATION_TOOLS for item in params.executed_tools or [])
