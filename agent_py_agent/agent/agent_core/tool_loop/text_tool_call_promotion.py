
from __future__ import annotations

"""native 协议下「文本工具调用 → 结构化 IR」的严格门控提升（Step 5 修复网）。

迁移期弱模型（M2.7/mimo 等）有训练惯性：本该用原生 ``tool_use`` 发起调用，却把调用写成
正文里的 ``[TOOL_CALL]{...}`` 文本块。``response_decision`` 已有一道二选一兜底——本轮没有
结构化 ``tool_use_blocks`` 时回退 ``parse_tool_calls(text)``。这层把那道兜底「收紧成一道
严格门控的提升闸」（对标 通道运行时 ``tool-call-repair``：文本→结构化提升，严格门控；
长期助手：检测泄漏只观测、不引入重试）。

为什么需要更严的门控：``parse_tool_calls`` 只做「JSON 块是否合法」的解析校验，并**不**校验
工具名是否真的注册。native 下若放任，模型正文里偶然出现的、tool 名拼错或根本不存在的
``[TOOL_CALL]`` 块也会被当成真实调用执行（再在执行层报 auth/unknown 失败，污染 IR）。所以
这里只在「**全部**解析出的调用都精确命中已注册工具」时才提升；任一调用名不命中（含解析
错误哨兵 ``__parse_error__`` / ``unknown``）即整批不提升，落回无工具路径，让模型自己纠偏。

严格约束：本模块只服务 native 分支。text 协议的回退一字不改（仍直接用
``parse_tool_calls`` 结果），由既有 ``__parse_error__`` 修复环引导弱模型，不经过这道闸。
"""

from typing import Any

# parse_tool_calls 在块坏掉时塞的哨兵工具名；这些一律不算「命中已注册工具」。
_NON_TOOL_SENTINELS = frozenset({"__parse_error__", "unknown", ""})


def promote_text_tool_calls_if_native(
    agent: object,
    response: object,
    parsed_calls: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """native 下严格门控地决定是否把文本解析出的调用提升为可执行调用。

    返回值语义：
    - 提升通过 → 原样返回 ``parsed_calls``（交给下游正常执行 + 进 IR，合成 id 配对）；
    - 不提升 → 返回 ``[]``（本轮当作没有工具调用，落回 no-tool 路径让模型纠偏）。

    门控条件（全部满足才提升）：
    1. ``native_tool_use_active(agent)`` 为真（text 协议不进这道闸，调用方已保证）；
    2. 本轮确实没有结构化 ``tool_use_blocks``（调用方已保证：有 block 时根本不会调这里）；
    3. ``parsed_calls`` 非空且**每一个**调用的工具名都精确命中已注册工具。

    本函数不改 text 协议路径——调用方只在 native 且无 block 时才路由到这里。
    """
    if not parsed_calls:
        return parsed_calls
    registered = _registered_tool_names(agent)
    # 注册表不可用时（防御：极简 stub agent）保持「不收紧」语义，行为同既有兜底。
    if registered is None:
        return parsed_calls
    if _all_calls_hit_registered_tools(parsed_calls, registered):
        _note_text_tool_call_promotion(response, promoted=True)
        return parsed_calls
    _note_text_tool_call_promotion(response, promoted=False)
    return []


def _all_calls_hit_registered_tools(
    calls: list[dict[str, Any]], registered: frozenset[str]
) -> bool:
    for call in calls:
        name = _call_tool_name(call)
        if name in _NON_TOOL_SENTINELS or name not in registered:
            return False
    return True


def _call_tool_name(call: dict[str, Any]) -> str:
    if not isinstance(call, dict):
        return ""
    return str(call.get("tool") or call.get("tool_name") or "").strip()


def _registered_tool_names(agent: object) -> frozenset[str] | None:
    """已注册工具名集合；无法取到（非标准 registry）返回 None 表示「不收紧」。"""
    registry = getattr(agent, "tools", None)
    names = getattr(registry, "tools", None)
    if not isinstance(names, dict):
        return None
    return frozenset(str(name) for name in names)


def _note_text_tool_call_promotion(response: object, *, promoted: bool) -> None:
    """观测埋点（长期助手 风格：检测到弱模型把调用漏成正文，记一笔，不引入重试）。

    只在 ``ModelResponse`` 上挂两个轻量计数属性，供上层日志/指标采样弱模型退化频率；
    取不到属性（非标准响应对象）时静默跳过，绝不影响主流程。
    """
    try:
        if promoted:
            current = int(getattr(response, "native_text_tool_call_promotions", 0) or 0)
            response.native_text_tool_call_promotions = current + 1  # type: ignore[attr-defined]
        else:
            current = int(getattr(response, "native_text_tool_call_rejections", 0) or 0)
            response.native_text_tool_call_rejections = current + 1  # type: ignore[attr-defined]
    except (AttributeError, TypeError, ValueError):
        return


__all__ = ["promote_text_tool_calls_if_native"]
