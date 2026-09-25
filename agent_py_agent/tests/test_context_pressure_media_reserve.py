"""已知图块按 input_media_token_reserve 折进上下文估算：预检与自动压缩判定不再低估多图上下文。"""
from __future__ import annotations

from types import SimpleNamespace

from agent_py_agent.agent.agent_core.model.context_pressure import (
    model_visible_context_tokens,
    projected_model_context_components,
)
from agent_py_agent.agent.agent_core.tool_request_projection import ToolLoopRequestProjection
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.tests._tool_runtime_harness import make_test_protocol_snapshot


def _image_block(index: int) -> dict:
    return {"type": "image", "source": {"type": "local_file", "path": f"att/{index}.png", "sha256": "a" * 64}}


def _projection(messages: list[dict]) -> ToolLoopRequestProjection:
    return ToolLoopRequestProjection("ready", provider_prompt="继续", system_instruction="系统", messages=messages, tools=None)


def test_known_media_blocks_add_reserve_to_messages_and_total():
    messages = [{"role": "user", "content": [{"type": "text", "text": "看这两张图"}, _image_block(1), _image_block(2)]}]
    plain_total, plain_parts = projected_model_context_components(_projection(messages))
    total, parts = projected_model_context_components(_projection(messages), media_token_reserve=1600)

    assert total == plain_total + 2 * 1600
    assert sum(parts.values()) == total, "状态条分类加总必须仍等于总量"
    assert parts["messages_tokens"] - plain_parts["messages_tokens"] >= 2 * 1600 - 4, "预留落在 messages 分类里"
    assert set(parts) == set(plain_parts), "不新增分类键，快照 schema 不变"


def test_media_outside_transport_expansion_set_and_zero_reserve_do_not_count():
    nested = [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": [_image_block(1)]}]}]
    assistant = [{"role": "assistant", "content": [_image_block(1)]}]
    base64_block = [{"role": "user", "content": [{"type": "image", "source": {"type": "base64", "data": "AAAA"}}]}]
    for messages in (nested, assistant, base64_block):
        plain, _ = projected_model_context_components(_projection(messages))
        reserved, _ = projected_model_context_components(_projection(messages), media_token_reserve=1600)
        assert reserved == plain
    with_media = [{"role": "user", "content": [_image_block(1)]}]
    plain, _ = projected_model_context_components(_projection(with_media))
    zero, _ = projected_model_context_components(_projection(with_media), media_token_reserve=0)
    assert zero == plain


def test_preflight_estimate_reads_configured_reserve_through_real_entry(monkeypatch):
    agent = SimpleNamespace(
        config=AgentConfig(
            auto_save_memory=True, enable_tools=True, tool_protocol="native", model_name="native-test-model",
            model_context_window_tokens=200_000, input_media_token_reserve=1600,
        ),
        backend=SimpleNamespace(context_window_tokens=200_000, name="anthropic_compatible"),
    )
    monkeypatch.setattr("agent_py_agent.agent.agent_core.model.context_pressure.resolve_native_tools", lambda _a, _p: [])
    history = [{"role": "user", "content": [{"type": "text", "text": "图在这里"}, _image_block(1), _image_block(2), _image_block(3)]}]

    def params(reserve: int) -> SimpleNamespace:
        agent.config.input_media_token_reserve = reserve
        return SimpleNamespace(
            context_scope="conversation", tool_protocol_snapshot=make_test_protocol_snapshot(source_protocol="native"),
            live_archive_state={}, tool_context=[], tool_ir_history=[], provider_history_messages=list(history),
        )

    without = model_visible_context_tokens(agent, params(0), "继续")
    with_reserve = model_visible_context_tokens(agent, params(1600), "继续")
    assert with_reserve - without == 3 * 1600
