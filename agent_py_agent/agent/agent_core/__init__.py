# LLM: Agent core orchestration module; keep planning, dispatch, tool-loop, and finalization contracts stable.
# 模块用途: 支撑主代理运行循环、计划、工具调用、子代理调度和收尾。

from __future__ import annotations

"""public support package for the split SimpleAgent core implementation.

SimpleAgent 的主循环、子代理、dispatch、prompt 模板、工具类和参数解析已经拆到这里。
`agent.core` 仍然是外部主入口，这个包服务内部组合。
"""

from .capability_request_tool import CapabilityRequestTool
from .dispatch_mixin import SimpleAgentDispatchMixin
from .hierarchy_tools import ScheduleChildSubagentsTool
from .models import AgentRunResult
from .orchestration_tools import (
    CreateSubagentsTool,
    DispatchSubagentsTool,
    InspectAgentTreeTool,
    RaiseEventTool,
)
from .runtime_guidance_tool import SendGuidanceTool
from .runtime_mixin import SimpleAgentRuntimeMixin
from .subagent_mixin import SimpleAgentSubagentMixin

__all__ = [
    "AgentRunResult",
    "CapabilityRequestTool",
    "CreateSubagentsTool",
    "DispatchSubagentsTool",
    "InspectAgentTreeTool",
    "RaiseEventTool",
    "ScheduleChildSubagentsTool",
    "SendGuidanceTool",
    "SimpleAgentDispatchMixin",
    "SimpleAgentRuntimeMixin",
    "SimpleAgentSubagentMixin",
]
