
from __future__ import annotations

"""public support package for the split SimpleAgent core implementation.

SimpleAgent 的主循环、子代理、dispatch、prompt 模板、工具类和参数解析已经拆到这里。
`agent.core` 仍然是外部主入口，这个包服务内部组合。
"""

from .capability_request_tool import CapabilityRequestTool
from .models import AgentRunResult
from .orchestration.dispatch.mixin import SimpleAgentDispatchMixin
from .orchestration_tools import (
    CancelSubagentsTool,
    CreateSubagentsTool,
    InspectAgentTreeTool,
    RaiseEventTool,
    ResolveCapabilityRequestsTool,
    execute_cancel_subagents,
)
from .runtime.guidance_tool import SendGuidanceTool
from .runtime_mixin import SimpleAgentRuntimeMixin
from .subagent_mixin import SimpleAgentSubagentMixin
from .task_progress_tool import TaskProgressTool

__all__ = [
    "AgentRunResult",
    "CapabilityRequestTool",
    "CancelSubagentsTool",
    "ResolveCapabilityRequestsTool",
    "CreateSubagentsTool",
    "InspectAgentTreeTool",
    "RaiseEventTool",
    "SendGuidanceTool",
    "SimpleAgentDispatchMixin",
    "SimpleAgentRuntimeMixin",
    "SimpleAgentSubagentMixin",
    "TaskProgressTool",
    "WaitTool",
    "execute_cancel_subagents",
]
