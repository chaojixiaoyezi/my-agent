"""LLM contract: public API for deterministic long-term memory rule routing.

这个包负责把'用户这句话可能需要读哪些长期规则文件'算出来，并提供一个
runtime context 服务按安全边界读取短正文。它不修改主循环，也不写长期规则文件。
"""

from .context import RoutedMemoryContext, build_routed_memory_context
from .loader import load_memory_routes, load_routes, parse_json_routes, parse_markdown_routes
from .matcher import build_read_receipt, match_routes, resolve_required_paths
from .models import MemoryPathResolution, MemoryReadReceipt, MemoryRoute, MemoryRouteMatch
from .validator import validate_routes

__all__ = [
    "MemoryPathResolution",
    "MemoryReadReceipt",
    "MemoryRoute",
    "MemoryRouteMatch",
    "RoutedMemoryContext",
    "build_read_receipt",
    "build_routed_memory_context",
    "load_memory_routes",
    "load_routes",
    "match_routes",
    "parse_json_routes",
    "parse_markdown_routes",
    "resolve_required_paths",
    "validate_routes",
]
