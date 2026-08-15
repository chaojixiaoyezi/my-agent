from __future__ import annotations

"""LLM: contract tests for the extension plugin registry.

给人看的解释：
这个测试保证未来 log_analysis/BAS/code_review 插件能通过统一注册入口接入。
"""

import sys
from types import ModuleType, SimpleNamespace

import pytest

from agent_py_agent.agent.extensions import (
    ExtensionActivationError,
    ExtensionLoadError,
    ExtensionRegistry,
    load_extension_registry,
)


class DemoPlugin:
    name = "demo"
    version = "0.1"

    def register_tools(self, registry) -> None:
        registry.append("tool")

    def register_commands(self, registry) -> None:
        registry.append("command")

    def register_memory_sources(self, registry) -> None:
        registry.append("memory")


def test_extension_registry_registers_plugin() -> None:
    registry = ExtensionRegistry()

    registry.register(DemoPlugin())

    assert registry.names() == ["demo"]
    assert registry.get("demo") is not None


def test_extension_registry_requires_name() -> None:
    class BadPlugin(DemoPlugin):
        name = ""

    with pytest.raises(ValueError):
        ExtensionRegistry().register(BadPlugin())


def test_extension_registry_rejects_duplicate_names() -> None:
    registry = ExtensionRegistry()
    registry.register(DemoPlugin())

    with pytest.raises(ValueError, match="duplicate extension plugin name"):
        registry.register(DemoPlugin())


def test_configured_module_plugin_uses_one_ordered_activation_chain(monkeypatch) -> None:
    calls: list[tuple[str, object]] = []

    class ConfiguredPlugin(DemoPlugin):
        def register_tools(self, registry) -> None:
            calls.append(("tools", registry))

        def register_commands(self, registry) -> None:
            calls.append(("commands", registry))

        def register_memory_sources(self, registry) -> None:
            calls.append(("memory", registry))

    module = ModuleType("test_configured_extension")
    module.plugin = ConfiguredPlugin
    monkeypatch.setitem(sys.modules, module.__name__, module)
    registry = load_extension_registry(["test_configured_extension:plugin"])
    agent = SimpleNamespace(tools=object(), memory=object())
    subparsers = object()

    registry.activate_agent(agent)
    registry.register_cli_commands(subparsers)

    assert [name for name, _ in calls] == ["tools", "memory", "commands"]
    assert registry.names() == ["demo"]


def test_configured_plugin_missing_is_fail_closed() -> None:
    with pytest.raises(ExtensionLoadError, match="extension load failed"):
        load_extension_registry(["missing_my_agent_plugin:plugin"])


def test_configured_plugin_activation_failure_is_fail_closed() -> None:
    class BrokenPlugin(DemoPlugin):
        def register_tools(self, registry) -> None:
            raise RuntimeError("broken")

    registry = ExtensionRegistry()
    registry.register(BrokenPlugin())

    with pytest.raises(ExtensionActivationError, match="register_tools"):
        registry.activate_agent(SimpleNamespace(tools=object(), memory=object()))


def test_plugin_command_registers_on_canonical_parser_chain() -> None:
    class CommandPlugin(DemoPlugin):
        def register_commands(self, registry) -> None:
            command = registry.add_parser("demo-command")
            command.set_defaults(func=lambda args: 0)

    registry = ExtensionRegistry()
    registry.register(CommandPlugin())

    from agent_py_agent.cli.parser import build_parser

    parser = build_parser(registry)
    args = parser.parse_args(["demo-command"])
    assert args.func(args) == 0


def test_invalid_extension_spec_is_rejected() -> None:
    with pytest.raises(ExtensionLoadError, match="extension spec must"):
        load_extension_registry(["extensions/demo.py"])
