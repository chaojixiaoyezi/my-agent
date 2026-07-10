"""审计 #8(部分,high/任务)真测:工具协议按模型能力运行时降级。

原状:tool_protocol 全局静态,自选非 native 模型(如某些 anthropic 兼容端点的 Text-01)上 native
静默失效(0 工具调用 + 幻觉完成),无运行时降级。在单一开关点 native_tool_use_active 加按模型能力
判定:命中 tool_protocol_text_models 子串的模型强制回退 text。空配置=默认行为不变。治 MEMORY 记的
"自选模型须按模型配协议"(reasoning→native / 非 reasoning→text)。
"""

from __future__ import annotations

from types import SimpleNamespace

from agent_py_agent.agent.agent_core.native_tool_protocol import native_tool_use_active


def _agent(*, protocol="native", enable=True, backend="anthropic_compatible", model="claude-opus-4-8", text_models=None):
    config = SimpleNamespace(
        tool_protocol=protocol,
        enable_tools=enable,
        model_name=model,
        tool_protocol_text_models=text_models or [],
    )
    return SimpleNamespace(config=config, backend=SimpleNamespace(name=backend))


def test_native_active_by_default_for_capable_model() -> None:
    assert native_tool_use_active(_agent()) is True  # 默认(空降级表)行为完全不变


def test_downgrade_for_configured_non_native_model() -> None:
    agent = _agent(model="abab-text-01", text_models=["text-01"])
    assert native_tool_use_active(agent) is False  # 命中 → 强制回退 text(防 native 静默失效)


def test_pattern_is_case_insensitive_substring() -> None:
    agent = _agent(model="MiniMax-Text-01-Pro", text_models=["minimax"])
    assert native_tool_use_active(agent) is False


def test_non_matching_model_stays_native() -> None:
    agent = _agent(model="claude-sonnet-4-6", text_models=["text-01", "minimax"])
    assert native_tool_use_active(agent) is True  # 不命中 → 保持 native


def test_empty_model_name_keeps_native() -> None:
    assert native_tool_use_active(_agent(model="")) is True  # 判断不了就不降级(不误伤)


def test_text_protocol_unchanged() -> None:
    assert native_tool_use_active(_agent(protocol="text")) is False  # 现有判定不受影响


def test_non_native_backend_unchanged() -> None:
    assert native_tool_use_active(_agent(backend="echo")) is False


def test_openai_compatible_backend_is_native_capable() -> None:
    assert native_tool_use_active(_agent(backend="openai_compatible", model="gpt-4.1")) is True


def test_tools_disabled_unchanged() -> None:
    assert native_tool_use_active(_agent(enable=False)) is False
