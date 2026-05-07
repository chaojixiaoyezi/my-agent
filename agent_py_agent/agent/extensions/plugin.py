# LLM: Extension module; keep plugin registration hooks stable.
# 模块用途: 定义插件扩展点，让外部能力注册工具、命令、工作流或记忆源。

from __future__ import annotations

"""lightweight extension plugin contracts.

给人看的解释：
这是短期插件化接口。log_analysis、未来 BAS、代码审计等能力可以先实现这些
注册方法，再逐步从 core 中解耦出来。
"""

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


# LLM: ExtensionPlugin is a 插件扩展边界 boundary object; coordinate field or method changes with callers, docs, and focused tests.
# 类用途: Protocol for optional extension packages.
@runtime_checkable
class ExtensionPlugin(Protocol):
    """Protocol for optional extension packages."""

    name: str
    version: str

    # LLM: ExtensionPlugin.register_tools belongs to 插件扩展边界; keep caller-visible returns, errors, and side effects aligned with focused tests.
    # 函数用途: Register tools exposed by this extension.。
    def register_tools(self, registry) -> None:
        """Register tools exposed by this extension."""

    # LLM: ExtensionPlugin.register_commands belongs to 插件扩展边界; keep caller-visible returns, errors, and side effects aligned with focused tests.
    # 函数用途: Register CLI/API commands exposed by this extension.。
    def register_commands(self, registry) -> None:
        """Register CLI/API commands exposed by this extension."""

    # LLM: ExtensionPlugin.register_workflows belongs to 插件扩展边界; keep caller-visible returns, errors, and side effects aligned with focused tests.
    # 函数用途: Register workflow templates or routers exposed by this extension.。
    def register_workflows(self, registry) -> None:
        """Register workflow templates or routers exposed by this extension."""

    # LLM: ExtensionPlugin.register_memory_sources belongs to 插件扩展边界; keep caller-visible returns, errors, and side effects aligned with focused tests.
    # 函数用途: Register memory sources exposed by this extension.。
    def register_memory_sources(self, registry) -> None:
        """Register memory sources exposed by this extension."""


# LLM: ExtensionRegistry is a 插件扩展边界 boundary object; coordinate field or method changes with callers, docs, and focused tests.
# 类用途: Small registry for extension plugin instances.
@dataclass
class ExtensionRegistry:
    """Small registry for extension plugin instances."""

    plugins: dict[str, ExtensionPlugin] = field(default_factory=dict)

    # LLM: ExtensionRegistry.register belongs to 插件扩展边界; keep caller-visible returns, errors, and side effects aligned with focused tests.
    # 函数用途: 完成 插件扩展边界 里的 register 步骤，保持现有返回值、异常和副作用语义；会读取实例字段。
    def register(self, plugin: ExtensionPlugin) -> None:
        if not plugin.name:
            raise ValueError("extension plugin name is required")
        self.plugins[plugin.name] = plugin

    # LLM: ExtensionRegistry.get belongs to 插件扩展边界; keep caller-visible returns, errors, and side effects aligned with focused tests.
    # 函数用途: 查询已有记录、索引或配置并返回给上层调用方，返回结构需要保持稳定；它是 ExtensionRegistry 的方法，通常依赖实例字段。
    def get(self, name: str) -> ExtensionPlugin | None:
        return self.plugins.get(name)

    # LLM: ExtensionRegistry.names belongs to 插件扩展边界; keep caller-visible returns, errors, and side effects aligned with focused tests.
    # 函数用途: 完成 插件扩展边界 里的 names 步骤，保持现有返回值、异常和副作用语义；会读取实例字段。
    def names(self) -> list[str]:
        return sorted(self.plugins)

