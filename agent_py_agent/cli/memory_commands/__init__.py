"""LLM: thin entry point that registers all memory CLI subcommands.

给人看的解释：
memory 命令是 CLI 的顶层入口，实际路由和 doctor 逻辑委托给专门的子模块。
"""

from __future__ import annotations

from ..common import make_agent
from .memory_query_cmd import cmd_memory_route
from .memory_doctor_cmd import cmd_memory_doctor

# Re-export helper functions for backward compatibility with tests
from .memory_query_cmd import (
    _resolve_index_path,
    _resolve_route_mode,
    _resolve_auto_read_limit,
    _config_warnings,
    _index_payload,
    _print_path_list,
)
from .memory_doctor_cmd import (
    _build_routing_doctor,
    _build_archive_doctor,
)

__all__ = [
    "cmd_memory_route",
    "cmd_memory_doctor",
    "make_agent",
    # Helper functions for tests
    "_resolve_index_path",
    "_resolve_route_mode",
    "_resolve_auto_read_limit",
    "_config_warnings",
    "_index_payload",
    "_print_path_list",
    "_build_routing_doctor",
    "_build_archive_doctor",
]