# LLM: 工具参数和本轮 ToolChoice 只来自宿主冻结事实；发送与容量共用选择规则，不更改运行时权限。
# 模块用途: 固定原生工具协议并渲染模型工具列表，避免工具说明重复堆叠系统提示。
from __future__ import annotations

"""Run-fixed tool protocol capability, selection and provider schema surface."""

from typing import Any

from ..tooling.runtime_contracts import ProviderToolCapability, ToolChoice, ToolProtocolSnapshot

_NATIVE_PROTOCOL = "native"


class ToolProtocolSelectionError(RuntimeError):
    """The configured run protocol has no explicitly authorized transport."""

    error_code = "TOOL_PROTOCOL_CAPABILITY_UNAVAILABLE"


def select_tool_protocol(agent: object, *, run_id: str) -> ToolProtocolSnapshot:
    """Probe once before the run; native is the only supported protocol."""

    config = getattr(agent, "config", None)
    requested = native_tool_protocol_value(getattr(config, "tool_protocol", "native"))
    backend = getattr(agent, "backend", None)
    if not bool(getattr(config, "enable_tools", False)):
        # 工具整体关闭:协议标记 native(工具列表为空),不再有 text 降级。
        capability = _declared_capability(
            backend,
            native_supported=True,
            evidence="tools_disabled_for_run",
        )
        return ToolProtocolSnapshot(run_id, _NATIVE_PROTOCOL, capability)
    if requested != _NATIVE_PROTOCOL:
        raise ValueError(f"invalid tool protocol: {requested}")

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
    # 删除 text 协议后:probe 未证明 native 支持=该模型无法使用工具,
    # 直接报错。
    # 消息带 evidence 帮助区分"模型没调工具/本地失败/协议失败"等真实原因。
    raise ToolProtocolSelectionError(
        "the run-start provider probe did not prove native tool support "
        f"for this model (evidence={capability.evidence}); coding tasks "
        "require a native tool-use capable model"
    )


def native_tool_use_active(params: object) -> bool:
    """Read the immutable run snapshot; never infer capability mid-run."""

    snapshot = getattr(params, "tool_protocol_snapshot", None)
    if not isinstance(snapshot, ToolProtocolSnapshot):
        raise RuntimeError("tool protocol snapshot is missing from this run")
    return snapshot.source_protocol == _NATIVE_PROTOCOL


# LLM: 仅消费结构化 required-action snapshot 或宿主 ToolChoice，不读模型正文或写运行账；生成和容量检查必须共用。
# 函数用途: 取得本轮真实工具选择，供发送、候选投影和预检使用同一份 schema 裁决。
def model_turn_tool_choice(params: object, tools: list[dict] | None) -> ToolChoice:
    from ..contracts.required_actions import tool_choice_for_required_actions

    snapshot = getattr(params, "effective_contract_snapshot", None)
    if snapshot is not None:
        visible_tools = tools
        if visible_tools is None:
            runtime_snapshot = getattr(params, "tool_runtime_snapshot", None)
            visible_tools = [
                {"name": name}
                for name in sorted(getattr(runtime_snapshot, "available_tool_names", ()) or ())
            ]
        return tool_choice_for_required_actions(snapshot, visible_tools)
    state = getattr(params, "live_archive_state", None)
    candidate = state.get("tool_choice") if isinstance(state, dict) else None
    return candidate if isinstance(candidate, ToolChoice) else ToolChoice.auto("ordinary_tool_turn")


def native_tool_protocol_value(tool_protocol: object) -> str:
    value = str(tool_protocol or "").strip().lower()
    if value in {"", _NATIVE_PROTOCOL}:
        return _NATIVE_PROTOCOL
    raise ValueError(f"invalid tool protocol: {value}")


# LLM: 原生工具只投影同一快照；通用授权文字由 provider_system_instruction 唯一承载，不按工具重复追加，也不改执行权限。
# 函数用途: 给模型生成本轮可用的原生工具和精确参数，避免每个工具再复制整段系统规则。
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
    "model_turn_tool_choice",
    "native_tool_protocol_value",
    "native_tool_use_active",
    "resolve_native_tools",
    "select_tool_protocol",
]
