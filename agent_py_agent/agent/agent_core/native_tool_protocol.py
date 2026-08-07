from __future__ import annotations

"""Run-fixed tool protocol capability, selection and provider schema surface."""

from typing import Any

from ..tooling.runtime_contracts import (
    ProviderToolCapability,
    ToolProtocolSnapshot,
)

_NATIVE_PROTOCOL = "native"
_TEXT_PROTOCOL = "text"


class ToolProtocolSelectionError(RuntimeError):
    """The configured run protocol has no explicitly authorized transport."""

    error_code = "TOOL_PROTOCOL_CAPABILITY_UNAVAILABLE"


def select_tool_protocol(agent: object, *, run_id: str) -> ToolProtocolSnapshot:
    """Probe once before the run and select exactly one protocol."""

    config = getattr(agent, "config", None)
    requested = native_tool_protocol_value(getattr(config, "tool_protocol", "native"))
    backend = getattr(agent, "backend", None)
    if not bool(getattr(config, "enable_tools", False)):
        capability = _declared_capability(
            backend,
            native_supported=False,
            evidence="tools_disabled_for_run",
        )
        return ToolProtocolSnapshot(run_id, _TEXT_PROTOCOL, capability)
    if requested == _TEXT_PROTOCOL:
        capability = _declared_capability(
            backend,
            native_supported=False,
            evidence="explicit_text_protocol_configuration",
        )
        return ToolProtocolSnapshot(run_id, _TEXT_PROTOCOL, capability)
    if _model_declared_text_protocol(config, backend):
        # 模型名单显式声明(配置 tool_protocol_text_models):该模型不参与 native probe,
        # 直接走 text。非 reasoning 模型配 native 会静默失效(0 工具调用+幻觉,真机实证
        # 2026-08-07 deepseek-v4-flash),名单是部署方显式配对,不是猜测。
        capability = _declared_capability(
            backend,
            native_supported=False,
            evidence="model_declared_text_protocol_configuration",
        )
        return ToolProtocolSnapshot(run_id, _TEXT_PROTOCOL, capability)

    probe = getattr(backend, "probe_tool_capability", None)
    capability = probe() if callable(probe) else _declared_capability(
        backend,
        native_supported=False,
        evidence="backend_has_no_capability_probe",
    )
    if not isinstance(capability, ProviderToolCapability):
        raise TypeError("backend probe_tool_capability must return ProviderToolCapability")
    if capability.native_supported:
        return ToolProtocolSnapshot(run_id, _NATIVE_PROTOCOL, capability)
    # 安全边界:probe 未证明 native 支持时,必须显式选 text(不静默降级)——防止 model 声称支持
    # native 但实际 probe 失败时,静默切 text 掩盖协议缺陷。换模型时若该模型确实不支持 native,
    # 部署方显式配置 tool_protocol=text(有测试/文档/边界),与目标书"native/text 不混用"一致。
    raise ToolProtocolSelectionError(
        "tool_protocol=native was configured, but the run-start provider probe "
        "did not prove native tool support; explicitly select tool_protocol=text"
    )


def native_tool_use_active(params: object) -> bool:
    """Read the immutable run snapshot; never infer capability mid-run."""

    snapshot = getattr(params, "tool_protocol_snapshot", None)
    if not isinstance(snapshot, ToolProtocolSnapshot):
        raise RuntimeError("tool protocol snapshot is missing from this run")
    return snapshot.source_protocol == _NATIVE_PROTOCOL


def native_tool_protocol_value(tool_protocol: object) -> str:
    value = str(tool_protocol or "").strip().lower()
    if not value:
        return _NATIVE_PROTOCOL
    if value not in {_NATIVE_PROTOCOL, _TEXT_PROTOCOL}:
        raise ValueError(f"invalid tool protocol: {value}")
    return value


def resolve_native_tools(agent: object, params: object) -> list[dict[str, Any]] | None:
    """Render schemas from the immutable runtime snapshot for a native turn."""

    if not native_tool_use_active(params):
        return None
    from ..backends.tool_schema import tool_model_specs_to_anthropic_tools

    registry = getattr(agent, "tools", None)
    snapshot = getattr(params, "tool_runtime_snapshot", None)
    if registry is None or snapshot is None:
        return None
    specs = registry.model_visible_specs(
        allowed_tools=getattr(params, "allowed_tools", None),
        loaded_tool_names=getattr(params, "loaded_tool_names", None),
        runtime_snapshot=snapshot,
    )
    tools = tool_model_specs_to_anthropic_tools(specs)
    return tools or None


def _model_declared_text_protocol(config: object, backend: object) -> bool:
    """True when the backend's model name is in the explicit text-protocol list."""
    configured = getattr(config, "tool_protocol_text_models", None)
    if not isinstance(configured, list) or not configured:
        return False
    model = str(getattr(backend, "model_name", "") or "").strip()
    if not model:
        return False
    return model in {str(item or "").strip() for item in configured if str(item or "").strip()}


def _declared_capability(
    backend: object,
    *,
    native_supported: bool,
    evidence: str,
) -> ProviderToolCapability:
    endpoint_resolver = getattr(backend, "_tool_endpoint", None)
    endpoint = (
        str(endpoint_resolver() or "").strip()
        if callable(endpoint_resolver)
        else f"local://{getattr(backend, 'name', 'unknown')}"
    )
    return ProviderToolCapability(
        provider=str(getattr(backend, "name", "unknown") or "unknown"),
        endpoint=endpoint,
        model=str(getattr(backend, "model_name", "") or ""),
        stream=bool(getattr(backend, "stream_enabled", False)),
        native_supported=native_supported,
        evidence=evidence,
    )


__all__ = [
    "ToolProtocolSelectionError",
    "native_tool_protocol_value",
    "native_tool_use_active",
    "resolve_native_tools",
    "select_tool_protocol",
]
