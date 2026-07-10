
from __future__ import annotations

"""Canonical extension discovery, validation, and activation chain.

插件只从管理员显式配置的 Python 模块或已安装 entry point 加载。运行时不会扫描
用户可写目录，避免把“目录里多了一个 .py 文件”变成隐式代码执行入口。
"""

from dataclasses import dataclass, field
from importlib import import_module, metadata
from typing import Protocol, runtime_checkable

EXTENSION_ENTRYPOINT_GROUP = "my_agent.plugins"


class ExtensionLoadError(RuntimeError):
    """Configured extension cannot be loaded or does not satisfy the contract."""


class ExtensionActivationError(RuntimeError):
    """Configured extension failed while registering a runtime surface."""


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
    """Ordered registry and the only activation path for extension plugins."""

    plugins: dict[str, ExtensionPlugin] = field(default_factory=dict)

    def register(self, plugin: ExtensionPlugin) -> None:
        if not plugin.name:
            raise ValueError("extension plugin name is required")
        if plugin.name in self.plugins:
            raise ValueError(f"duplicate extension plugin name: {plugin.name}")
        self.plugins[plugin.name] = plugin

    def get(self, name: str) -> ExtensionPlugin | None:
        return self.plugins.get(name)

    def names(self) -> list[str]:
        return sorted(self.plugins)

    def activate_agent(self, agent) -> None:
        targets = (
            ("register_tools", agent.tools),
            ("register_workflows", agent.subagents),
            ("register_memory_sources", agent.memory),
        )
        for plugin in self.plugins.values():
            for hook_name, target in targets:
                _call_plugin_hook(plugin, hook_name, target)

    def register_cli_commands(self, subparsers) -> None:
        for plugin in self.plugins.values():
            _call_plugin_hook(plugin, "register_commands", subparsers)


def load_extension_registry(specs: list[str] | tuple[str, ...] | None) -> ExtensionRegistry:
    """Load only explicitly configured extensions, preserving configured order.

    Supported spec forms:
    - ``entrypoint:<name>`` from the ``my_agent.plugins`` package entry-point group;
    - ``package.module:<attribute>`` for an importable, administrator-installed module.
    """

    registry = ExtensionRegistry()
    for raw_spec in specs or ():
        spec = str(raw_spec or "").strip()
        if not spec:
            continue
        try:
            loaded = _load_extension_spec(spec)
            plugin = _as_plugin(loaded, spec)
            registry.register(plugin)
        except ExtensionLoadError:
            raise
        except Exception as exc:
            raise ExtensionLoadError(
                f"extension load failed spec={spec!r} error={type(exc).__name__}: {exc}"
            ) from exc
    return registry


def _load_extension_spec(spec: str):
    if spec.startswith("entrypoint:"):
        name = spec.removeprefix("entrypoint:").strip()
        if not name:
            raise ExtensionLoadError("extension entrypoint name is required")
        matches = [item for item in _extension_entry_points() if item.name == name]
        if len(matches) != 1:
            raise ExtensionLoadError(
                f"extension entrypoint must resolve exactly once name={name!r} matches={len(matches)}"
            )
        return matches[0].load()
    module_name, separator, attribute = spec.partition(":")
    if not separator or not module_name.strip() or not attribute.strip():
        raise ExtensionLoadError(
            "extension spec must be 'entrypoint:<name>' or 'package.module:<attribute>'"
        )
    module = import_module(module_name.strip())
    try:
        return getattr(module, attribute.strip())
    except AttributeError as exc:
        raise ExtensionLoadError(
            f"extension attribute not found spec={spec!r}"
        ) from exc


def _extension_entry_points():
    discovered = metadata.entry_points()
    if hasattr(discovered, "select"):
        return list(discovered.select(group=EXTENSION_ENTRYPOINT_GROUP))
    return list(discovered.get(EXTENSION_ENTRYPOINT_GROUP, ()))


def _as_plugin(loaded, spec: str) -> ExtensionPlugin:
    candidate = loaded() if isinstance(loaded, type) else loaded
    if not isinstance(candidate, ExtensionPlugin) and callable(candidate):
        candidate = candidate()
    if not isinstance(candidate, ExtensionPlugin):
        raise ExtensionLoadError(f"extension does not implement ExtensionPlugin spec={spec!r}")
    return candidate


def _call_plugin_hook(plugin: ExtensionPlugin, hook_name: str, target) -> None:
    try:
        getattr(plugin, hook_name)(target)
    except Exception as exc:
        raise ExtensionActivationError(
            f"extension activation failed plugin={plugin.name!r} hook={hook_name!r} "
            f"error={type(exc).__name__}: {exc}"
        ) from exc
