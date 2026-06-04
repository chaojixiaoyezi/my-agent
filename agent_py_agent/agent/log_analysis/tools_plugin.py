
from __future__ import annotations

"""Log analysis plugin implementing ExtensionPlugin interface.

This module provides LogAnalysisPlugin which wraps the log analysis tools
and registers them via the ExtensionPlugin protocol.
"""

from pathlib import Path
from typing import TYPE_CHECKING

from ..extensions.plugin import ExtensionPlugin

if TYPE_CHECKING:
    from ..extensions.plugin import ExtensionRegistry


class LogAnalysisPlugin:
    """ExtensionPlugin implementation for log analysis tools.

    This class wraps the log analysis tools and registers them via the
    ExtensionPlugin protocol for integration with the agent's extension system."""

    name: str = "log_analysis"
    version: str = "1.0.0"

    def __init__(self, store_root: Path | None = None) -> None:
        """Initialize plugin with optional store root."""
        self.store_root = store_root or Path.cwd()

    def register_tools(self, registry) -> None:
        """Register log analysis tools with the tool registry."""
        from .tools import register_tools
        register_tools(registry, self.store_root)

    def register_commands(self, registry) -> None:
        """Register CLI/API commands (not implemented for this version)."""
        pass

    def register_workflows(self, registry) -> None:
        """Register workflow templates (not implemented for this version)."""
        pass

    def register_memory_sources(self, registry) -> None:
        """Register memory sources (not implemented for this version)."""
        pass


# For convenience, expose the plugin instance
_plugin_instance: LogAnalysisPlugin | None = None


def get_plugin(store_root: Path | None = None) -> LogAnalysisPlugin:
    """Get or create the LogAnalysisPlugin singleton."""
    global _plugin_instance
    if _plugin_instance is None:
        _plugin_instance = LogAnalysisPlugin(store_root)
    return _plugin_instance
