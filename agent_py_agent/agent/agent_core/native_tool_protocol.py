
from __future__ import annotations

"""原生 tool_use 协议(tool_protocol=native)的启用判定与 tools schema 解析。

单一开关点：只有 config.tool_protocol=native 且当前 backend 支持原生工具协议
（Anthropic/OpenAI compatible）时才走原生协议，其余情况一律回退现有文本协议。prompt 侧和
生成侧(给 backend.generate 传 tools schema)都用这里的同一个判定，避免两侧漂移。
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)

_NATIVE_PROTOCOL = "native"
_NATIVE_BACKENDS = frozenset({"anthropic_compatible", "openai_compatible"})
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
    blocks = list(getattr(response, "tool_use_blocks", None) or [])
    if blocks and not _native_turn_is_empty_or_truncated(agent, response, blocks):
        agent._native_empty_streak = 0  # 真实有效地用了 native 工具 → 清零
        return
    # 根因B 补盲:没有 block(原判据),或虽有 block 但全是"空参/参数被截断"——
    # 后者过去命中"有 block 就清零"导致 streak 永远到不了阈值、永不降级。两者都计空转。
    streak = int(getattr(agent, "_native_empty_streak", 0) or 0) + 1
    agent._native_empty_streak = streak
    if streak >= _NATIVE_DOWNGRADE_THRESHOLD:
        agent._native_downgraded = True
        logger.warning(
            "native 协议连续 %d 轮 0 有效 tool_use(工具已供给;空 tool_use 或参数被截断),"
            "运行时降级到 text 协议(模型疑不支持 native 或持续截断)", streak
        )


def _native_turn_is_empty_or_truncated(agent: object, response: object, blocks: list) -> bool:
    """本轮所有 tool_use_block 都"无效"(空参且工具有 required_parameters)或整轮被截断时 True。

    只要有任意一个 block 是真实有效调用就返回 False(保守清零,绝不误降能正常工作的模型)。
    限定"工具有 required_parameters 却给空 input"——合法 0 参工具(input={} 本就正确)不计入。
    response.truncated 为 True 时整轮判为截断空转(MiniMax 长 content 写入被切断的形态)。
    """
    if bool(getattr(response, "truncated", False)):
        return True
    for block in blocks:
        if not _block_is_empty_required_call(agent, block):
            return False  # 存在一个真实有效调用 → 不是空转
    return True


def _block_is_empty_required_call(agent: object, block: object) -> bool:
    if not isinstance(block, dict):
        return False
    tool_input = block.get("input")
    if isinstance(tool_input, dict) and tool_input:
        return False  # 有参数 = 有效调用,不算空参
    name = str(block.get("name", "") or "").strip()
    return _tool_has_required_parameters(agent, name)


def _tool_has_required_parameters(agent: object, tool_name: str) -> bool:
    """该工具是否声明了 required_parameters(空 input 对它就是缺参)。异常一律保守返回 False。"""
    if not tool_name:
        return False
    try:
        registry = getattr(agent, "tools", None)
        tools = getattr(registry, "tools", None)
        tool = tools.get(tool_name) if isinstance(tools, dict) else None
        spec = getattr(tool, "spec", None)
        required = getattr(spec, "required_parameters", None) or []
        return bool(required)
    except Exception:
        return False


def resolve_native_tools(agent: object, params: object) -> list[dict[str, Any]] | None:
    """Build the canonical native tools schema for this turn, or None for text protocol.

    Respects the same allowed_tools / granted_capabilities scoping used to
    render the prompt catalog so the model is only offered authorized tools.
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
            granted_capabilities=getattr(params, "granted_capabilities", None),
            loaded_tool_names=getattr(params, "loaded_tool_names", None),
        )
    else:
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
