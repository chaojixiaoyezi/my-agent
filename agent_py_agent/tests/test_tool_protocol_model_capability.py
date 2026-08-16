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
    text_models: list[str] | None = None,
) -> SimpleNamespace:
    backend = _Backend(native_supported=native_supported)
    return SimpleNamespace(
        config=SimpleNamespace(
            tool_protocol=protocol,
            enable_tools=enable_tools,
            tool_protocol_text_models=list(text_models or []),
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
        match="text fallback has been removed",
    ):
        select_tool_protocol(agent, run_id="run-native-fail")

    assert agent.backend.probes == 1


def test_explicit_text_protocol_is_rejected() -> None:
    # 2026-08-16: text 协议已删除——显式配置 text 一律拒绝(fail-closed)。
    agent = _agent(native_supported=True, protocol="text")

    with pytest.raises(ValueError, match="text protocol has been removed"):
        select_tool_protocol(agent, run_id="run-explicit-text")

    assert agent.backend.probes == 0


def test_tools_disabled_selects_non_native_snapshot_without_probe() -> None:
    agent = _agent(native_supported=True, enable_tools=False)

    snapshot = select_tool_protocol(agent, run_id="run-tools-disabled")

    assert snapshot.source_protocol == "native"
    assert snapshot.capability.evidence == "tools_disabled_for_run"
    assert agent.backend.probes == 0


def test_native_tool_use_requires_run_fixed_protocol_snapshot() -> None:
    with pytest.raises(RuntimeError, match="tool protocol snapshot is missing"):
        native_tool_use_active(SimpleNamespace())


def test_model_in_text_models_probes_native_as_usual() -> None:
    # 2026-08-16: text 协议已删除(用户指示固定 native)——text 模型名单机制随之删除,
    # 任何模型都走 native probe(deepseek-v4-flash 真机验证 native 工作正常)。
    agent = _agent(native_supported=True, text_models=["deepseek-v4-flash"])
    agent.backend.model_name = "deepseek-v4-flash"

    snapshot = select_tool_protocol(agent, run_id="run-native-model")

    assert snapshot.source_protocol == "native"
    assert agent.backend.probes == 1
    assert native_tool_use_active(SimpleNamespace(tool_protocol_snapshot=snapshot)) is True


def test_model_not_in_text_models_probes_native_as_usual() -> None:
    agent = _agent(native_supported=True, text_models=["other-model"])

    snapshot = select_tool_protocol(agent, run_id="run-native-other")

    assert snapshot.source_protocol == "native"
    assert agent.backend.probes == 1


def test_empty_text_models_leaves_native_probe_untouched() -> None:
    agent = _agent(native_supported=True, text_models=[])

    snapshot = select_tool_protocol(agent, run_id="run-native-empty-list")

    assert snapshot.source_protocol == "native"
    assert agent.backend.probes == 1


def test_text_models_no_longer_exist_when_protocol_explicitly_text() -> None:
    # 2026-08-16: text 协议已删除——显式 text + 名单配置同样被拒绝。
    agent = _agent(native_supported=True, protocol="text", text_models=["deepseek-v4-flash"])

    with pytest.raises(ValueError, match="text protocol has been removed"):
        select_tool_protocol(agent, run_id="run-text-explicit")

    assert agent.backend.probes == 0
