
from __future__ import annotations

"""本模块把本地 LOG 查询能力包装成安全工具，返回摘要、预览行和 evidence refs，而不是整包原始日志。

新手说明:
这里的函数和类是 agent 能调用的"安全日志工具"。它们不会把所有日志直接塞进 prompt，
而是先按 IP、域名、时间窗口等条件查询本地 store，再把结果写成可追溯证据引用。
这样 analyst 可以引用 evidence refs 做判断，父会话也能回头检查证据。
查询函数和工具类分别放在 query_functions.py 和 tool_classes.py，本包公开当前工具 API。
"""

from .query_functions import (
    hunt_ip,
    security_hunt_domain,
    security_hunt_ip,
    security_query,
    security_trace_case,
    trace_case,
)
from .tool_classes import (
    SecurityHuntIpTool,
    SecurityQueryTool,
    SecurityTraceCaseTool,
)


def register_tools(registry, store_root=None) -> None:
    """Register all log-analysis tools with a tool registry."""

    from pathlib import Path

    root = Path.cwd() if store_root is None else Path(store_root)
    registry.register(SecurityQueryTool(root))
    registry.register(SecurityHuntIpTool(root))
    registry.register(SecurityTraceCaseTool(root))

__all__ = [
    "hunt_ip",
    "security_hunt_domain",
    "security_hunt_ip",
    "security_query",
    "security_trace_case",
    "trace_case",
    "SecurityHuntIpTool",
    "SecurityQueryTool",
    "SecurityTraceCaseTool",
    "register_tools",
]
