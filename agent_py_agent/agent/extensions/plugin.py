
from __future__ import annotations

"""lightweight extension plugin contracts.

这是短期插件化接口。log_analysis、未来 BAS、代码审计等能力可以先实现这些
注册方法，再逐步从 core 中解耦出来。
"""

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@runtime_checkable
class ExtensionPlugin(Protocol):
    """Protocol for optional extension packages."""

    name: str
    version: str

    def register_tools(self, registry) -> None:
        """Register tools exposed by this extension."""

    def register_commands(self, registry) -> None:
        """Register CLI/API commands exposed by this extension."""

    def register_workflows(self, registry) -> None:
        """Register workflow templates or routers exposed by this extension."""

    def register_memory_sources(self, registry) -> None:
        """Register memory sources exposed by this extension."""


@dataclass
class ExtensionRegistry:
    """Small registry for extension plugin instances."""

    plugins: dict[str, ExtensionPlugin] = field(default_factory=dict)

    def register(self, plugin: ExtensionPlugin) -> None:
        if not plugin.name:
            raise ValueError("extension plugin name is required")
        self.plugins[plugin.name] = plugin

    def get(self, name: str) -> ExtensionPlugin | None:
        return self.plugins.get(name)

    def names(self) -> list[str]:
        return sorted(self.plugins)

