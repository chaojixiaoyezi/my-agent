from __future__ import annotations

"""LLM: contract tests for the extension plugin registry.

给人看的解释：
这个测试保证未来 log_analysis/BAS/code_review 插件能通过统一注册入口接入。
"""

import pytest

from agent_py_agent.agent.extensions import ExtensionRegistry


class DemoPlugin:
    name = "demo"
    version = "0.1"

    def register_tools(self, registry) -> None:
        registry.append("tool")

    def register_commands(self, registry) -> None:
        registry.append("command")

    def register_workflows(self, registry) -> None:
        registry.append("workflow")

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
