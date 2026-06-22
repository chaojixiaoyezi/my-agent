"""审计 #8(自动检测,high/任务)真测:native 连续空转 → 运行时自动降级到 text。

MEMORY 实证:native 在非 reasoning 模型上"0 工具调用 + 幻觉完成"静默失效。本测真造连续若干轮
"工具已供给但模型 0 tool_use"的响应,断言连续 K 次后 native_tool_use_active 自动转 False(降级 text);
有任一轮真用了工具即清零(不误降 capable 模型);没供工具的轮不算空转。text 协议任何模型都能工作,
降级永远安全。学 [[project_native_protocol_model_pairing]]。
"""

from __future__ import annotations

from types import SimpleNamespace

from agent_py_agent.agent.agent_core.native_tool_protocol import (
    _NATIVE_DOWNGRADE_THRESHOLD,
    native_tool_use_active,
    record_native_turn,
)


def _capable_agent() -> SimpleNamespace:
    config = SimpleNamespace(
        tool_protocol="native", enable_tools=True, model_name="some-model", tool_protocol_text_models=[]
    )
    return SimpleNamespace(config=config, backend=SimpleNamespace(name="anthropic_compatible"))


def _resp(blocks: list) -> SimpleNamespace:
    return SimpleNamespace(tool_use_blocks=blocks)


def test_downgrade_after_k_consecutive_empty_native_turns() -> None:
    agent = _capable_agent()
    assert native_tool_use_active(agent) is True  # 初始走 native
    for _ in range(_NATIVE_DOWNGRADE_THRESHOLD):
        record_native_turn(agent, tools_offered=True, response=_resp([]))  # 工具供给但 0 tool_use
    assert native_tool_use_active(agent) is False  # 连续 K 次空转 → 运行时降级 text


def test_tool_use_resets_streak() -> None:
    agent = _capable_agent()
    record_native_turn(agent, True, _resp([]))
    record_native_turn(agent, True, _resp([]))  # 差一次到阈值
    record_native_turn(agent, True, _resp([{"type": "tool_use", "name": "x"}]))  # 真用了工具 → 清零
    record_native_turn(agent, True, _resp([]))
    record_native_turn(agent, True, _resp([]))  # 又攒 2 次,仍未连续 K 次
    assert native_tool_use_active(agent) is True  # capable 模型不被误降


def test_turns_without_tools_offered_not_counted() -> None:
    agent = _capable_agent()
    for _ in range(_NATIVE_DOWNGRADE_THRESHOLD + 2):
        record_native_turn(agent, tools_offered=False, response=_resp([]))  # 本轮没供工具
    assert native_tool_use_active(agent) is True  # 不算空转 → 不降级


def test_downgrade_is_sticky() -> None:
    agent = _capable_agent()
    for _ in range(_NATIVE_DOWNGRADE_THRESHOLD):
        record_native_turn(agent, True, _resp([]))
    assert native_tool_use_active(agent) is False  # 已降级
    record_native_turn(agent, True, _resp([{"type": "tool_use", "name": "x"}]))  # 之后即便有工具
    assert native_tool_use_active(agent) is False  # 降级状态保持(text 安全,不反复横跳)


def test_record_native_turn_never_raises_on_unsettable_agent() -> None:
    class _Frozen:
        __slots__ = ()  # 不能设属性 → 内部设 _native_empty_streak 会抛

    record_native_turn(_Frozen(), True, _resp([]))  # 异常隔离:不抛
