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
from agent_py_agent.agent.model_guidance import (
    ACTION_AUTHORIZATION_GUIDANCE,
    VERIFICATION_EVIDENCE_GUIDANCE,
    provider_system_instruction,
)
from agent_py_agent.agent.tooling.models import (
    EffectResolverPolicy,
    ToolModelSpec,
    ToolRuntime,
    ToolRuntimePolicy,
    ToolRuntimeSnapshot,
)
from agent_py_agent.agent.tooling.registry import _tool_call_protocol
from agent_py_agent.agent.tooling.runtime_contracts import ProviderToolCapability

# --- prompt-side protocol switch -------------------------------------------


def test_evidence_guidance_reuses_valid_results_without_reducing_authorization():
    text = provider_system_instruction(SimpleNamespace(
        supports_system_instructions=True, supports_provider_request_options=True,
    ))
    assert text == VERIFICATION_EVIDENCE_GUIDANCE + ACTION_AUTHORIZATION_GUIDANCE
    assert "每次行动和最终回复前重新核对" not in text
    assert "版本、输入和观察点未变时复用已有有效结果" in text
    assert "针对实际改动验证" in text
    assert "宿主审批、owner 隔离及专用确认规则始终有效" in text


def test_text_protocol_instruction_is_rejected():
    """EXEC-31b: 文本协议已删除, 请求 text 指令直接 ValueError。"""
    with pytest.raises(ValueError, match="invalid tool protocol"):
        _tool_call_protocol("text")


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


def test_text_protocol_config_is_rejected():
    """EXEC-31b: 配置 text 直接 ValueError——不再有 text 快照/降级路径。"""
    agent = _agent(protocol="text", native_supported=True)
    with pytest.raises(ValueError, match="invalid tool protocol"):
        select_tool_protocol(agent, run_id="run-explicit-text")


def test_inactive_for_non_native_backend():
    agent = _agent(protocol="native", native_supported=False)
    with pytest.raises(ToolProtocolSelectionError):
        select_tool_protocol(agent, run_id="run-no-native")


def test_tools_disabled_still_marks_native():
    """EXEC-31b: 工具整体关闭时协议仍标 native(工具列表为空), 不再有 text 降级。"""
    agent = _agent(protocol="native", native_supported=True, enable_tools=False)
    snapshot = select_tool_protocol(agent, run_id="run-tools-disabled")
    assert native_tool_use_active(SimpleNamespace(tool_protocol_snapshot=snapshot)) is True
    assert snapshot.capability.evidence == "tools_disabled_for_run"


def test_missing_run_snapshot_is_not_inferred_from_backend_or_config():
    with pytest.raises(RuntimeError, match="snapshot is missing"):
        native_tool_use_active(_agent(protocol="native", native_supported=True))


def test_native_protocol_value_normalizes():
    assert native_tool_protocol_value("native") == "native"
    assert native_tool_protocol_value("NATIVE") == "native"
    with pytest.raises(ValueError, match="invalid tool protocol"):
        native_tool_protocol_value("text")
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


def test_resolve_returns_none_when_registry_missing():
    """EXEC-31b: 不再有 text 快照; resolve 的 None 分支=agent 无 tools 注册表
    (快照缺失是 RuntimeError, 由 test_missing_run_snapshot 覆盖)。"""
    agent = _agent(protocol="native", native_supported=True)
    snapshot = select_tool_protocol(agent, run_id="run-resolve-noregistry")
    params = SimpleNamespace(
        allowed_tools=None,
        tool_protocol_snapshot=snapshot,
        tool_runtime_snapshot="runtime-snapshot",
    )

    assert resolve_native_tools(agent, params) is None


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


class _SnapshotRegistry:
    def __init__(self, snapshot: ToolRuntimeSnapshot) -> None:
        self.snapshot = snapshot

    def model_visible_specs(self, **kwargs):
        assert kwargs["runtime_snapshot"] is self.snapshot
        return list(self.snapshot.specs)


# LLM: 测试夹具必须让 model spec 与 runtime effect 保持同一 typed snapshot，不能按名称伪造副作用。
# 函数用途: 构造一个纯只读、命令混合、参数混合和纯写入工具各一个的最小真实快照。
def _effect_guidance_snapshot() -> tuple[list[ToolModelSpec], ToolRuntimeSnapshot]:
    specs = [
        _fake_spec("read_file", {"path": "文件路径"}),
        _fake_spec("run_command", {"command": "命令"}),
        _fake_spec("process_session", {"action": "poll 或 stop"}),
        _fake_spec("write_file", {"path": "文件路径"}),
    ]
    policies = [
        ToolRuntimePolicy(effect_resolver=EffectResolverPolicy("read_only")),
        ToolRuntimePolicy(
            effect_resolver=EffectResolverPolicy(
                "read_only",
                strategy="command",
                command_parameter="command",
            )
        ),
        ToolRuntimePolicy(
            effect_resolver=EffectResolverPolicy(
                "read_only",
                by_parameter=(
                    ("action", (("poll", "read_only"), ("stop", "mutating"))),
                ),
            )
        ),
        ToolRuntimePolicy(effect_resolver=EffectResolverPolicy("mutating")),
    ]
    runtimes = tuple(
        ToolRuntime(
            model_spec=spec,
            runtime_policy=policy,
            handler=SimpleNamespace(model_spec=spec),
        )
        for spec, policy in zip(specs, policies, strict=True)
    )
    snapshot = ToolRuntimeSnapshot(
        run_id="run-effect-guidance",
        runtimes=runtimes,
        available_tool_names=frozenset(spec.name for spec in specs),
        unavailable_tools=(),
        allowed_tools=None,
    )
    return specs, snapshot


def test_resolve_native_tools_keeps_exact_specs_and_single_system_authorization():
    specs, snapshot = _effect_guidance_snapshot()
    agent = _agent(protocol="native", native_supported=True)
    agent.backend.supports_system_instructions = True
    agent.backend.supports_provider_request_options = True
    agent.tools = _SnapshotRegistry(snapshot)
    params = SimpleNamespace(
        allowed_tools=None,
        loaded_tool_names=set(),
        tool_runtime_snapshot=snapshot,
        tool_protocol_snapshot=select_tool_protocol(agent, run_id="run-effect-guidance"),
    )

    tools = resolve_native_tools(agent, params)

    by_name = {tool["name"]: tool for tool in tools or []}
    for spec in specs:
        assert by_name[spec.name]["description"] == spec.description
        assert by_name[spec.name]["input_schema"] == spec.input_schema
        assert ACTION_AUTHORIZATION_GUIDANCE not in by_name[spec.name]["description"]
    system = provider_system_instruction(agent.backend)
    assert system.count(ACTION_AUTHORIZATION_GUIDANCE) == 1
    assert "仅要求查看、解释或诊断时，做必要的只读检查" in system
    assert "只读分工须向子代理保留同样的检查范围" in system
    assert "角色、工具可用性和 Full Access 都不代表新增业务目标的授权" in system
    assert "宿主审批、owner 隔离及专用确认规则始终有效" in system
    assert snapshot.specs == tuple(specs)
