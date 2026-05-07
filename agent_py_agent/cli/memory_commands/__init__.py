# LLM: CLI memory command helper; keep archive/query/doctor option shapes stable.
# 模块用途: 提供 memory 查询、诊断或归档相关命令入口。


from __future__ import annotations

from ..common import make_agent
from .memory_doctor_cmd import (
    _build_archive_doctor,
    _build_routing_doctor,
    _memory_config_payload,
    cmd_memory_doctor,
)

# Re-export helper functions for backward compatibility with tests
from .memory_query_cmd import (
    _config_warnings,
    _index_payload,
    _print_path_list,
    _resolve_auto_read_limit,
    _resolve_index_path,
    _resolve_route_mode,
    cmd_memory_route,
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
    "_memory_config_payload",
    "_print_path_list",
    "_build_routing_doctor",
    "_build_archive_doctor",
]