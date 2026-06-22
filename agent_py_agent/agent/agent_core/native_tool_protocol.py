
from __future__ import annotations

"""原生 tool_use 协议(tool_protocol=native)的启用判定与 tools schema 解析。

单一开关点：只有 config.tool_protocol=native 且当前 backend 是 anthropic_compatible
时才走原生协议，其余情况一律回退现有文本协议。prompt 侧(跳过 [TOOL_CALL] 指令)和
生成侧(给 backend.generate 传 tools schema)都用这里的同一个判定，避免两侧漂移。
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)

_NATIVE_PROTOCOL = "native"
_NATIVE_BACKEND = "anthropic_compatible"
# native 下连续这么多轮"工具被供给但模型 0 tool_use"即判定该模型不支持 native,运行时降级 text(审计 #8)。
_NATIVE_DOWNGRADE_THRESHOLD = 3


def native_tool_use_active(agent: object) -> bool:
    """Return True when this agent should use native Anthropic tool_use."""
    if bool(getattr(agent, "_native_downgraded", False)):
        return False  # 运行时已自动降级(审计 #8:native 连续空转判定模型不支持)
    config = getattr(agent, "config", None)
    protocol = str(getattr(config, "tool_protocol", "text") or "text").strip().lower()
    if protocol != _NATIVE_PROTOCOL:
        return False
    if not bool(getattr(config, "enable_tools", False)):
        return False
    backend = getattr(agent, "backend", None)
    if str(getattr(backend, "name", "") or "") != _NATIVE_BACKEND:
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


def record_native_turn(agent: object, tools_offered: bool, response: object) -> None:
    """跟踪 native 协议下"工具被供给但模型 0 tool_use"的连续空转,连续 K 次 → 运行时降级到 text。

    审计 #8 的运行时自动降级:模型用了工具(或本轮没供工具)即清零;连续 K 次空转 = 该模型大概率
    不支持 native(非 reasoning),降级到 text(text 协议在任何模型都能工作,降级永远安全)。
    异常隔离:跟踪/降级出错只吞不冒泡,绝不影响生成主流程(热路径)。
    """
    try:
        _record_native_turn(agent, tools_offered, response)
    except Exception:
        pass


def _record_native_turn(agent: object, tools_offered: bool, response: object) -> None:
    if not tools_offered:
        return  # 本轮没给 native 工具,不是空转信号
    if getattr(response, "tool_use_blocks", None) or []:
        agent._native_empty_streak = 0  # 用了 native 工具 → 清零
        return
    streak = int(getattr(agent, "_native_empty_streak", 0) or 0) + 1
    agent._native_empty_streak = streak
    if streak >= _NATIVE_DOWNGRADE_THRESHOLD:
        agent._native_downgraded = True
        logger.warning(
            "native 协议连续 %d 轮 0 tool_use(工具已供给),运行时降级到 text 协议(模型疑不支持 native)", streak
        )


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
