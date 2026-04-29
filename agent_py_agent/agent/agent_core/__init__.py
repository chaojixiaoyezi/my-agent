from __future__ import annotations

"""LLM: public support package for the split SimpleAgent core implementation.

给人看的解释：
SimpleAgent 的主循环、子代理、dispatch、prompt 模板、工具类和参数解析已经拆到这里。
`agent.core` 仍然是外部主入口，这个包服务内部组合。
"""

from .dispatch_mixin import SimpleAgentDispatchMixin
from .models import AgentRunResult
from .orchestration_tools import CreateSubagentsTool, DispatchSubagentsTool, SubagentBoardTool
from .runtime_mixin import SimpleAgentRuntimeMixin
from .subagent_mixin import SimpleAgentSubagentMixin

__all__ = [
    "AgentRunResult",
    "CreateSubagentsTool",
    "DispatchSubagentsTool",
    "SimpleAgentDispatchMixin",
    "SimpleAgentRuntimeMixin",
    "SimpleAgentSubagentMixin",
    "SubagentBoardTool",
]
