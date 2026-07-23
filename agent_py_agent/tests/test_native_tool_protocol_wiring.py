from __future__ import annotations

from types import SimpleNamespace

from agent_py_agent.agent.agent_core.native_tool_protocol import (
    native_tool_protocol_value,
    native_tool_use_active,
    resolve_native_tools,
)
from agent_py_agent.agent.tooling.registry import _tool_call_protocol

# --- prompt-side protocol switch -------------------------------------------


def test_text_protocol_keeps_tool_call_text_instruction():
    text = _tool_call_protocol("text")
    assert "[TOOL_CALL]" in text
    assert "[/TOOL_CALL]" in text


def test_native_protocol_drops_tool_call_text_instruction():
    text = _tool_call_protocol("native")
    assert "[TOOL_CALL]" not in text
    assert "原生工具调用" in text


def test_unknown_protocol_defaults_to_text_instruction():
    text = _tool_call_protocol("bogus")
    assert "[TOOL_CALL]" in text


# --- activation gate -------------------------------------------------------


def _agent(*, protocol: str, backend_name: str, enable_tools: bool = True):
    return SimpleNamespace(
        config=SimpleNamespace(tool_protocol=protocol, enable_tools=enable_tools),
        backend=SimpleNamespace(name=backend_name),
    )


def test_active_for_native_capable_backends():
    assert native_tool_use_active(_agent(protocol="native", backend_name="anthropic_compatible")) is True
    assert native_tool_use_active(_agent(protocol="native", backend_name="openai_compatible")) is True


def test_inactive_for_text_protocol():
    assert native_tool_use_active(_agent(protocol="text", backend_name="anthropic_compatible")) is False


def test_inactive_for_non_native_backend():
    assert native_tool_use_active(_agent(protocol="native", backend_name="echo")) is False


def test_inactive_when_tools_disabled():
    assert native_tool_use_active(
        _agent(protocol="native", backend_name="anthropic_compatible", enable_tools=False)
    ) is False


def test_native_protocol_value_normalizes():
    assert native_tool_protocol_value("native") == "native"
    assert native_tool_protocol_value("NATIVE") == "native"
    assert native_tool_protocol_value("text") == "text"
    assert native_tool_protocol_value("") == "text"
    assert native_tool_protocol_value(None) == "text"


# --- resolve_native_tools --------------------------------------------------


class _FakeSpec:
    def __init__(self, name: str, parameters: dict[str, str]):
        self.name = name
        self.description = f"{name} desc"
        self.parameters = parameters


class _FakeRegistry:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def specs(self, *, allowed_tools=None, include_orchestration=True):
        self.calls.append(
            {
                "allowed_tools": allowed_tools,
                "include_orchestration": include_orchestration,
            }
        )
        return [
            _FakeSpec("read_file", {"path": "文件路径"}),
            _FakeSpec("web_fetch", {"url": "完整 URL"}),
        ]


class _ProgressiveRegistry(_FakeRegistry):
    def model_visible_specs(
        self,
        *,
        allowed_tools=None,
        loaded_tool_names=None,
        runtime_snapshot=None,
    ):
        self.calls.append(
            {
                "allowed_tools": allowed_tools,
                "loaded_tool_names": loaded_tool_names,
                "runtime_snapshot": runtime_snapshot,
            }
        )
        specs = [_FakeSpec("tool_search", {"query": "query"})]
        if "create_subagents" in (loaded_tool_names or set()):
            specs.append(_FakeSpec("create_subagents", {"goal": "goal"}))
        return specs


def _agent_with_registry(*, protocol: str, backend_name: str):
    agent = _agent(protocol=protocol, backend_name=backend_name)
    agent.tools = _FakeRegistry()
    return agent


def test_resolve_returns_anthropic_tools_schema_when_active():
    agent = _agent_with_registry(protocol="native", backend_name="anthropic_compatible")
    params = SimpleNamespace(
        allowed_tools=["read_file"],
        tool_runtime_snapshot=None,
    )

    tools = resolve_native_tools(agent, params)

    assert tools is not None
    assert [t["name"] for t in tools] == ["read_file", "web_fetch"]
    assert tools[0]["input_schema"]["properties"]["path"]["type"] == "string"
    # scoping is forwarded to the registry
    assert agent.tools.calls[0]["allowed_tools"] == ["read_file"]


def test_resolve_returns_none_when_inactive():
    agent = _agent_with_registry(protocol="text", backend_name="anthropic_compatible")
    params = SimpleNamespace(allowed_tools=None)

    assert resolve_native_tools(agent, params) is None
    # must not even query the registry when protocol is text
    assert agent.tools.calls == []


def test_resolve_returns_none_when_no_specs():
    agent = _agent(protocol="native", backend_name="anthropic_compatible")

    class _Empty:
        def specs(self, **kwargs):
            return []

    agent.tools = _Empty()
    params = SimpleNamespace(allowed_tools=None)

    assert resolve_native_tools(agent, params) is None


def test_resolve_native_tools_uses_typed_discovery_state():
    agent = _agent(protocol="native", backend_name="openai_compatible")
    agent.tools = _ProgressiveRegistry()
    params = SimpleNamespace(
        allowed_tools=None,
        loaded_tool_names={"create_subagents"},
        tool_runtime_snapshot="snapshot",
    )

    tools = resolve_native_tools(agent, params)

    assert [tool["name"] for tool in tools] == ["tool_search", "create_subagents"]
    assert agent.tools.calls == [
        {
            "allowed_tools": None,
            "loaded_tool_names": {"create_subagents"},
            "runtime_snapshot": "snapshot",
        }
    ]
