from __future__ import annotations

"""Run-fixed tool protocol capability, selection and provider schema surface."""

import time
from typing import Any

from ..tooling.runtime_contracts import (
    ProviderToolCapability,
    ToolProtocolSnapshot,
)

_NATIVE_PROTOCOL = "native"

#: probe 重试（2026-08-17 MiniMax 端点真机实锤）：探针是真实网络请求，
#: 端点偶发失败/慢（thinking 占满输出、连接抖动）不应误杀整个 run——
#: 重试 N 次（短退避）后才判失败。持续失败仍 fail-closed 拒绝（不静默降级）。
_PROBE_RETRIES = 3
_PROBE_RETRY_BACKOFF = 1.0


class ToolProtocolSelectionError(RuntimeError):
    """The configured run protocol has no explicitly authorized transport."""

    error_code = "TOOL_PROTOCOL_CAPABILITY_UNAVAILABLE"


def select_tool_protocol(agent: object, *, run_id: str) -> ToolProtocolSnapshot:
    """Probe（带重试）before the run and select exactly one protocol."""

    config = getattr(agent, "config", None)
    requested = native_tool_protocol_value(getattr(config, "tool_protocol", "native"))
    backend = getattr(agent, "backend", None)
    if not bool(getattr(config, "enable_tools", False)):
        # 工具禁用=无工具调用=协议无关, snapshot 声明 native(快照一致性,
        # runtime_contracts 要求 native snapshot 必须带 native 支持声明)。
        capability = _declared_capability(
            backend,
            native_supported=True,
            evidence="tools_disabled_for_run",
        )
        return ToolProtocolSnapshot(run_id, _NATIVE_PROTOCOL, capability)
    if requested == "text":
        # 2026-08-16 用户指示: 固定只用 native, text 协议(旧方法)删除——
        # 显式配置 text 一律拒绝(fail-closed), 不再有 text 降级路径。
        raise ToolProtocolSelectionError(
            "text protocol has been removed; tool_protocol=native is the only supported value"
        )
    probe = getattr(backend, "probe_tool_capability", None)
    capability: ProviderToolCapability | None = None
    last_evidence = "backend_has_no_capability_probe"
    attempts = 0
    while attempts < _PROBE_RETRIES:
        attempts += 1
        capability = probe() if callable(probe) else _declared_capability(
            backend,
            native_supported=False,
            evidence="backend_has_no_capability_probe",
        )
        if not isinstance(capability, ProviderToolCapability):
            raise TypeError("backend probe_tool_capability must return ProviderToolCapability")
        if capability.native_supported:
            return ToolProtocolSnapshot(run_id, _NATIVE_PROTOCOL, capability)
        last_evidence = str(capability.evidence or "")
        if attempts < _PROBE_RETRIES:
            time.sleep(_PROBE_RETRY_BACKOFF * attempts)
    # 安全边界:probe 持续未证明 native 支持时,必须显式拒绝(不静默降级)——防止 model 声称支持
    # native 但实际 probe 失败时,静默切 text 掩盖协议缺陷。换模型时若该模型确实不支持 native,
    # 部署方显式配置 tool_protocol=text(有测试/文档/边界),与目标书"native/text 不混用"一致。
    raise ToolProtocolSelectionError(
        "tool_protocol=native was configured, but the run-start provider probe "
        f"did not prove native tool support after {_PROBE_RETRIES} attempts "
        f"(last evidence: {last_evidence}); text fallback has been removed"
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
    if value != _NATIVE_PROTOCOL:
        raise ValueError(
            f"invalid tool protocol: {value!r}; text protocol has been removed, "
            "tool_protocol=native is the only supported value"
        )
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
