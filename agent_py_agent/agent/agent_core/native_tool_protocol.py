from __future__ import annotations

"""Run-fixed tool protocol capability, selection and provider schema surface."""

from dataclasses import replace
from typing import Any

from ..model_guidance import ACTION_AUTHORIZATION_GUIDANCE
from ..tooling.models import ToolModelSpec, ToolRuntimePolicy, ToolRuntimeSnapshot
from ..tooling.runtime_contracts import ProviderToolCapability, ToolProtocolSnapshot

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


def native_tool_protocol_value(tool_protocol: object) -> str:
    value = str(tool_protocol or "").strip().lower()
    if value in {"", _NATIVE_PROTOCOL}:
        return _NATIVE_PROTOCOL
    raise ValueError(f"invalid tool protocol: {value}")


# LLM: 原生工具说明必须从同一 ToolRuntimeSnapshot 投影；授权提示只影响模型选择，不能替代结构化执行门。
# 函数用途: 给模型生成本轮真实可用的原生工具列表，并在可能改状态的工具旁提醒用户授权边界。
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
    specs = _with_action_authorization_guidance(specs, snapshot)
    tools = tool_model_specs_to_anthropic_tools(specs)
    return tools or None


# LLM: 是否可能产生副作用只读取 canonical runtime policy，不得按工具名或说明文字维护名单。
# 函数用途: 为可能改状态的工具追加稳定提示；纯只读工具保持原说明，避免浪费上下文。
def _with_action_authorization_guidance(
    specs: list[ToolModelSpec],
    runtime_snapshot: object,
) -> list[ToolModelSpec]:
    if not isinstance(runtime_snapshot, ToolRuntimeSnapshot):
        return specs
    projected: list[ToolModelSpec] = []
    for spec in specs:
        runtime = runtime_snapshot.runtime(spec.name)
        if runtime is None or not _policy_may_change_state(runtime.runtime_policy):
            projected.append(spec)
            continue
        description = spec.description
        if ACTION_AUTHORIZATION_GUIDANCE not in description:
            description = f"{description}\n\n{ACTION_AUTHORIZATION_GUIDANCE}"
        projected.append(replace(spec, description=description))
    return projected


# LLM: command strategy 和任何 mutating/dangerous 变体都代表模型可选择有副作用参数；具体调用仍由执行门解析。
# 函数用途: 根据工具已有的 effect 声明判断它是否需要展示授权提示。
def _policy_may_change_state(policy: ToolRuntimePolicy) -> bool:
    resolver = policy.effect_resolver
    if resolver.strategy == "command" or resolver.default_effect != "read_only":
        return True
    return any(
        effect != "read_only"
        for _field_name, variants in resolver.by_parameter
        for _value, effect in variants
    ) or any(
        effect != "read_only"
        for _conditions, effect in resolver.by_parameter_combinations
    )


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
