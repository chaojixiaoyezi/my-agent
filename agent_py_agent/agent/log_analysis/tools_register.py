from __future__ import annotations

"""Tool registration for log analysis extension.

This module provides the register_tools function that registers all log analysis
tools with the agent's tool registry.
"""

from pathlib import Path

from .tools_handlers import SecurityHuntIpTool, SecurityQueryTool, SecurityTraceCaseTool


def register_tools(registry, store_root: Path | None = None) -> None:
    """Register all log analysis tools with the given registry.

    Args:
        registry: Tool registry that supports .register() method.
        store_root: Optional root path for the log analysis store.
    """
    root = store_root or Path.cwd()
    registry.register(SecurityQueryTool(root))
    registry.register(SecurityHuntIpTool(root))
    registry.register(SecurityTraceCaseTool(root))