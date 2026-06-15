
from __future__ import annotations

"""原生 tool_use 协议(tool_protocol=native)的启用判定与 tools schema 解析。

单一开关点：只有 config.tool_protocol=native 且当前 backend 是 anthropic_compatible
时才走原生协议，其余情况一律回退现有文本协议。prompt 侧(跳过 [TOOL_CALL] 指令)和
生成侧(给 backend.generate 传 tools schema)都用这里的同一个判定，避免两侧漂移。
"""

from typing import Any

_NATIVE_PROTOCOL = "native"
_NATIVE_BACKEND = "anthropic_compatible"


def native_tool_use_active(agent: object) -> bool:
    """Return True when this agent should use native Anthropic tool_use."""
    config = getattr(agent, "config", None)
    protocol = str(getattr(config, "tool_protocol", "text") or "text").strip().lower()
    if protocol != _NATIVE_PROTOCOL:
        return False
    if not bool(getattr(config, "enable_tools", False)):
        return False
    backend = getattr(agent, "backend", None)
    return str(getattr(backend, "name", "") or "") == _NATIVE_BACKEND


def native_tool_protocol_value(tool_protocol: object) -> str:
    """Normalize a raw tool_protocol value to 'native' or 'text'."""
    return _NATIVE_PROTOCOL if str(tool_protocol or "").strip().lower() == _NATIVE_PROTOCOL else "text"


def resolve_native_tools(agent: object, params: object) -> list[dict[str, Any]] | None:
    """Build the Anthropic tools schema for this turn, or None for text protocol.

    Respects the same allowed_tools / granted_capabilities scoping used to
    render the prompt catalog so the model is only offered authorized tools.
    """
    if not native_tool_use_active(agent):
        return None
    from ..backends.tool_schema import tool_specs_to_anthropic_tools

    registry = getattr(agent, "tools", None)
    if registry is None or not hasattr(registry, "specs"):
        return None
    specs = registry.specs(
        allowed_tools=getattr(params, "allowed_tools", None),
        granted_capabilities=getattr(params, "granted_capabilities", None),
        include_orchestration=True,
    )
    tools = tool_specs_to_anthropic_tools(specs)
    return tools or None


__all__ = [
    "native_tool_protocol_value",
    "native_tool_use_active",
    "resolve_native_tools",
]
