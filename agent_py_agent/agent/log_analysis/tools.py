
from __future__ import annotations

"""Log analysis tools — thin re-export for backward compatibility.

This module re-exports the public API from tools_functions, tools_handlers,
and tools_register for backward compatibility with code that imported from tools.py.
"""

from .tools_functions import (
    security_hunt_domain,
    security_hunt_ip,
    security_query,
    security_trace_case,
    trace_case,
)
from .tools_handlers import (
    SecurityHuntIpTool,
    SecurityQueryTool,
    SecurityTraceCaseTool,
)
from .tools_register import register_tools

__all__ = [
    "SecurityHuntIpTool",
    "SecurityQueryTool",
    "SecurityTraceCaseTool",
    "register_tools",
    "security_hunt_domain",
    "security_hunt_ip",
    "security_query",
    "security_trace_case",
    "trace_case",
]