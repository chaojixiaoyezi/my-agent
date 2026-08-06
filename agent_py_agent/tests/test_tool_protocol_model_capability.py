"""Run-start tool protocol selection uses observed capability and one frozen snapshot."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.native_tool_protocol import (
    ToolProtocolSelectionError,
    native_tool_use_active,
    select_tool_protocol,
)
from agent_py_agent.agent.tooling.runtime_contracts import ProviderToolCapability


class _Backend:
    name = "test-provider"
    model_name = "test-model"
    stream_enabled = False

    def __init__(self, *, native_supported: bool) -> None:
        self.native_supported = native_supported
        self.probes = 0

    def _tool_endpoint(self) -> str:
        return "local://tool-capability-test"

    def probe_tool_capability(self) -> ProviderToolCapability:
        self.probes += 1
        return ProviderToolCapability(
            provider=self.name,
            endpoint=self._tool_endpoint(),
            model=self.model_name,
            stream=self.stream_enabled,
            native_supported=self.native_supported,
            evidence="explicit_test_probe",
        )


def _agent(
    *,
    native_supported: bool,
    protocol: str = "native",
    enable_tools: bool = True,
) -> SimpleNamespace:
    backend = _Backend(native_supported=native_supported)
    return SimpleNamespace(
        config=SimpleNamespace(
            tool_protocol=protocol,
            enable_tools=enable_tools,
        ),
        backend=backend,
    )


def test_native_protocol_requires_positive_provider_capability_probe() -> None:
    agent = _agent(native_supported=True)

    snapshot = select_tool_protocol(agent, run_id="run-native")

    assert snapshot.source_protocol == "native"
    assert snapshot.capability.evidence == "explicit_test_probe"
    assert agent.backend.probes == 1
    assert native_tool_use_active(SimpleNamespace(tool_protocol_snapshot=snapshot)) is True


def test_failed_native_capability_probe_does_not_silently_select_text() -> None:
    agent = _agent(native_supported=False)

    with pytest.raises(
        ToolProtocolSelectionError,
        match="explicitly select tool_protocol=text",
    ):
        select_tool_protocol(agent, run_id="run-text")

    assert agent.backend.probes == 1


def test_explicit_text_protocol_does_not_probe_native_transport() -> None:
    agent = _agent(native_supported=True, protocol="text")

    snapshot = select_tool_protocol(agent, run_id="run-explicit-text")

    assert snapshot.source_protocol == "text"
    assert snapshot.capability.evidence == "explicit_text_protocol_configuration"
    assert agent.backend.probes == 0


def test_tools_disabled_selects_non_native_snapshot_without_probe() -> None:
    agent = _agent(native_supported=True, enable_tools=False)

    snapshot = select_tool_protocol(agent, run_id="run-tools-disabled")

    assert snapshot.source_protocol == "text"
    assert snapshot.capability.evidence == "tools_disabled_for_run"
    assert agent.backend.probes == 0


def test_native_tool_use_requires_run_fixed_protocol_snapshot() -> None:
    with pytest.raises(RuntimeError, match="tool protocol snapshot is missing"):
        native_tool_use_active(SimpleNamespace())
