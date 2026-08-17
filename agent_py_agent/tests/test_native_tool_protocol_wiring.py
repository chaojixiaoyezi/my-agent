from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.native_tool_protocol import (
    ToolProtocolSelectionError,
    native_tool_protocol_value,
    native_tool_use_active,
    resolve_native_tools,
    select_tool_protocol,
)
from agent_py_agent.agent.tooling.models import ToolModelSpec
from agent_py_agent.agent.tooling.registry import _tool_call_protocol
from agent_py_agent.agent.tooling.runtime_contracts import ProviderToolCapability

# --- prompt-side protocol switch -------------------------------------------


def test_text_protocol_keeps_tool_call_text_instruction():
    text = _tool_call_protocol("text")
    assert "[TOOL_CALL]" in text
    assert "[/TOOL_CALL]" in text


def test_native_protocol_drops_tool_call_text_instruction():
    text = _tool_call_protocol("native")
    assert "[TOOL_CALL]" not in text
    assert "原生工具调用" in text


def test_unknown_catalog_protocol_is_rejected():
    with pytest.raises(ValueError, match="invalid tool protocol"):
        _tool_call_protocol("bogus")


# --- activation gate -------------------------------------------------------


class _CapabilityBackend:
    def __init__(self, *, native_supported: bool, name: str = "test") -> None:
        self.name = name
        self.model_name = "test-model"
        self.stream_enabled = False
        self.native_supported = native_supported

    def probe_tool_capability(self) -> ProviderToolCapability:
        return ProviderToolCapability(
            provider=self.name,
            endpoint="local://test",
            model=self.model_name,
            stream=self.stream_enabled,
            native_supported=self.native_supported,
            evidence="test_probe",
        )


def _agent(
    *,
    protocol: str,
    native_supported: bool,
    enable_tools: bool = True,
    backend_name: str = "test",
):
    return SimpleNamespace(
        config=SimpleNamespace(
            tool_protocol=protocol,
            enable_tools=enable_tools,
        ),
        backend=_CapabilityBackend(
            native_supported=native_supported,
            name=backend_name,
        ),
    )


def test_selects_native_only_from_observed_capability():
    agent = _agent(protocol="native", native_supported=True)
    snapshot = select_tool_protocol(agent, run_id="run-native")

    assert snapshot.source_protocol == "native"
    assert native_tool_use_active(SimpleNamespace(tool_protocol_snapshot=snapshot)) is True


def test_runtime_response_history_cannot_override_configured_native_protocol():
    agent = _agent(protocol="native", native_supported=True)
    snapshot = select_tool_protocol(agent, run_id="run-fixed")
    # Older builds persisted this process-local marker after ordinary no-tool
    # replies. Protocol selection now belongs only to explicit configuration
    # and backend/model capability facts.
    agent._native_downgraded = True
    params = SimpleNamespace(tool_protocol_snapshot=snapshot)
    assert native_tool_use_active(params) is True


def test_inactive_for_text_protocol():
    agent = _agent(protocol="text", native_supported=True)
    snapshot = select_tool_protocol(agent, run_id="run-explicit-text")
    assert native_tool_use_active(SimpleNamespace(tool_protocol_snapshot=snapshot)) is False


def test_inactive_for_non_native_backend():
    agent = _agent(protocol="native", native_supported=False)
    with pytest.raises(ToolProtocolSelectionError):
        select_tool_protocol(agent, run_id="run-no-native")


def test_inactive_when_tools_disabled():
    agent = _agent(protocol="native", native_supported=True, enable_tools=False)
    snapshot = select_tool_protocol(agent, run_id="run-tools-disabled")
    assert native_tool_use_active(SimpleNamespace(tool_protocol_snapshot=snapshot)) is False


def test_missing_run_snapshot_is_not_inferred_from_backend_or_config():
    with pytest.raises(RuntimeError, match="snapshot is missing"):
        native_tool_use_active(_agent(protocol="native", native_supported=True))


def test_native_protocol_value_normalizes():
    assert native_tool_protocol_value("native") == "native"
    assert native_tool_protocol_value("NATIVE") == "native"
    assert native_tool_protocol_value("text") == "text"
    assert native_tool_protocol_value("") == "native"
    assert native_tool_protocol_value(None) == "native"
    with pytest.raises(ValueError, match="invalid tool protocol"):
        native_tool_protocol_value("bogus")


# --- resolve_native_tools --------------------------------------------------


def _fake_spec(name: str, parameters: dict[str, str]) -> ToolModelSpec:
    return ToolModelSpec(
        name=name,
        description=f"{name} desc",
        input_schema={
            "type": "object",
            "properties": {
                key: {"type": "string", "description": description}
                for key, description in parameters.items()
            },
            "additionalProperties": False,
        },
    )


class _FakeRegistry:
    def __init__(self) -> None:
        self.calls: list[dict] = []

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
        specs = [
            _fake_spec("read_file", {"path": "文件路径"}),
            _fake_spec("web_fetch", {"url": "完整 URL"}),
        ]
        if allowed_tools is None:
            return specs
        allowed = set(allowed_tools)
        return [spec for spec in specs if spec.name in allowed]


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
        specs = [_fake_spec("tool_search", {"query": "query"})]
        if "create_subagents" in (loaded_tool_names or set()):
            specs.append(_fake_spec("create_subagents", {"goal": "goal"}))
        return specs


def _agent_with_registry(*, protocol: str, native_supported: bool):
    agent = _agent(protocol=protocol, native_supported=native_supported)
    agent.tools = _FakeRegistry()
    return agent


def test_resolve_returns_anthropic_tools_schema_when_active():
    agent = _agent_with_registry(protocol="native", native_supported=True)
    protocol_snapshot = select_tool_protocol(agent, run_id="run-resolve-native")
    params = SimpleNamespace(
        allowed_tools=["read_file"],
        loaded_tool_names=set(),
        tool_runtime_snapshot="runtime-snapshot",
        tool_protocol_snapshot=protocol_snapshot,
    )

    tools = resolve_native_tools(agent, params)

    assert tools is not None
    assert [t["name"] for t in tools] == ["read_file"]
    assert tools[0]["input_schema"]["properties"]["path"]["type"] == "string"
    # scoping is forwarded to the registry
    assert agent.tools.calls[0]["allowed_tools"] == ["read_file"]
    assert agent.tools.calls[0]["runtime_snapshot"] == "runtime-snapshot"


def test_resolve_returns_none_when_inactive():
    agent = _agent_with_registry(protocol="text", native_supported=True)
    params = SimpleNamespace(
        allowed_tools=None,
        tool_protocol_snapshot=select_tool_protocol(agent, run_id="run-resolve-text"),
    )

    assert resolve_native_tools(agent, params) is None
    # must not even query the registry when protocol is text
    assert agent.tools.calls == []


def test_resolve_returns_none_when_no_specs():
    agent = _agent(protocol="native", native_supported=True)

    class _Empty:
        def model_visible_specs(self, **kwargs):
            return []

    agent.tools = _Empty()
    params = SimpleNamespace(
        allowed_tools=None,
        loaded_tool_names=set(),
        tool_runtime_snapshot="runtime-snapshot",
        tool_protocol_snapshot=select_tool_protocol(agent, run_id="run-resolve-empty"),
    )

    assert resolve_native_tools(agent, params) is None


def test_resolve_native_tools_uses_typed_discovery_state():
    agent = _agent(protocol="native", native_supported=True)
    agent.tools = _ProgressiveRegistry()
    params = SimpleNamespace(
        allowed_tools=None,
        loaded_tool_names={"create_subagents"},
        tool_runtime_snapshot="snapshot",
        tool_protocol_snapshot=select_tool_protocol(agent, run_id="run-discovery"),
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


def test_probe_retries_transient_failure_then_succeeds():
    """2026-08-17 MiniMax 端点实锤：探针是真实网络请求，偶发失败不应误杀
    run——前两次失败第三次成功 → 重试后选中 native。"""
    import itertools

    class _FlakyBackend:
        def __init__(self) -> None:
            self.calls = 0
            self.name = "flaky"
            self.model_name = "test-model"
            self.stream_enabled = False

        def probe_tool_capability(self) -> ProviderToolCapability:
            self.calls += 1
            if self.calls < 3:
                return ProviderToolCapability(
                    provider=self.name, endpoint="local://flaky",
                    model=self.model_name, stream=False,
                    native_supported=False, evidence="live_probe_failed:transient",
                )
            return ProviderToolCapability(
                provider=self.name, endpoint="local://flaky",
                model=self.model_name, stream=False,
                native_supported=True, evidence="live_probe_returned_structured_tool_call",
            )

    backend = _FlakyBackend()
    agent = SimpleNamespace(
        config=SimpleNamespace(tool_protocol="native", enable_tools=True),
        backend=backend,
    )
    snapshot = select_tool_protocol(agent, run_id="run-flaky")
    assert snapshot.source_protocol == "native"
    assert backend.calls == 3


def test_probe_retries_exhausted_still_fail_closed():
    """持续失败（重试耗尽）→ 仍 fail-closed 拒绝（不静默降级）。"""
    agent = _agent(protocol="native", native_supported=False)
    import pytest as _pytest

    with _pytest.raises(ToolProtocolSelectionError) as exc_info:
        select_tool_protocol(agent, run_id="run-fail")
    assert "3 attempts" in str(exc_info.value)
