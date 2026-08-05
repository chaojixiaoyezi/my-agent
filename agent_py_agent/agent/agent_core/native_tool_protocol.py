
from __future__ import annotations

"""原生 tool_use 协议(tool_protocol=native)的启用判定与 tools schema 解析。

单一开关点：只有 config.tool_protocol=native 且当前 backend 支持原生工具协议
（Anthropic/OpenAI compatible）时才走原生协议，其余情况一律回退现有文本协议。prompt 侧和
生成侧(给 backend.generate 传 tools schema)都用这里的同一个判定，避免两侧漂移。
"""

from typing import Any

_NATIVE_PROTOCOL = "native"
_NATIVE_BACKENDS = frozenset({"anthropic_compatible", "openai_compatible"})


def native_tool_use_active(agent: object) -> bool:
    """Return True when this agent should use native Anthropic tool_use."""
    config = getattr(agent, "config", None)
    protocol = str(getattr(config, "tool_protocol", "text") or "text").strip().lower()
    if protocol != _NATIVE_PROTOCOL:
        return False
    if not bool(getattr(config, "enable_tools", False)):
        return False
    backend = getattr(agent, "backend", None)
    if str(getattr(backend, "name", "") or "") not in _NATIVE_BACKENDS:
        return False
    return _model_supports_native(config)  # 按模型能力降级(审计 #8):非 native 模型强制回退 text


def _model_supports_native(config: object) -> bool:
    """当前模型是否走 native:命中 tool_protocol_text_models 任一子串则强制回退 text(防非 native 模型静默失效)。"""
    model = str(getattr(config, "model_name", "") or "").strip().lower()
    if not model:
        return True
    for pattern in getattr(config, "tool_protocol_text_models", None) or []:
        token = str(pattern).strip().lower()
        if token and token in model:
            return False
    return True


def native_tool_protocol_value(tool_protocol: object) -> str:
    """Normalize a raw tool_protocol value to 'native' or 'text'."""
    return _NATIVE_PROTOCOL if str(tool_protocol or "").strip().lower() == _NATIVE_PROTOCOL else "text"


# LLM: native Schema 必须使用 ToolLoopExecuteParams 中固定的 run 快照，并仅叠加真实 tool_search 已加载名称。
# 函数用途: 为当前模型回合生成已授权且已就绪的 Anthropic/OpenAI 原生工具定义。
def resolve_native_tools(agent: object, params: object) -> list[dict[str, Any]] | None:
    """Build the canonical native tools schema for this turn, or None for text protocol.

    Respects the same run snapshot used by the prompt catalog and final
    execution so the model is only offered authorized, ready tools.
    """
    if not native_tool_use_active(agent):
        return None
    from ..backends.tool_schema import tool_specs_to_anthropic_tools

    registry = getattr(agent, "tools", None)
    if registry is None or not hasattr(registry, "specs"):
        return None
    if hasattr(registry, "model_visible_specs"):
        specs = registry.model_visible_specs(
            allowed_tools=getattr(params, "allowed_tools", None),
            loaded_tool_names=getattr(params, "loaded_tool_names", None),
            runtime_snapshot=getattr(params, "tool_runtime_snapshot", None),
        )
    else:
        specs = registry.specs(
            allowed_tools=getattr(params, "allowed_tools", None),
            include_orchestration=True,
        )
    tools = tool_specs_to_anthropic_tools(specs)
    return tools or None


__all__ = [
    "native_tool_protocol_value",
    "native_tool_use_active",
    "resolve_native_tools",
]
