# LLM: 仅验证同源插话包与原生 UserTurn 的内部身份，以及后端出站时不泄露身份。
# 模块用途: 防止同文不同插话被误认成同一输入，同时保持普通用户输入的旧构造方式。
from __future__ import annotations

from types import SimpleNamespace

from agent_py_agent.agent.agent_core.runtime.guidance import _inject_claimed_guidance
from agent_py_agent.agent.agent_core.tool_ir_history import record_user_turn_ir
from agent_py_agent.agent.backends.message_adapter import AnthropicMessageAdapter
from agent_py_agent.agent.backends.tool_ir import UserTurn
from agent_py_agent.tests._tool_runtime_harness import make_test_protocol_snapshot


def test_same_text_with_different_input_ids_remains_two_user_turns() -> None:
    params = SimpleNamespace(tool_ir_history=[])

    record_user_turn_ir(params, "继续原任务", input_ids=("guidance-a",))
    record_user_turn_ir(params, "继续原任务", input_ids=("guidance-b",))

    assert params.tool_ir_history == [
        UserTurn("继续原任务", input_ids=("guidance-a",)),
        UserTurn("继续原任务", input_ids=("guidance-b",)),
    ]
    assert UserTurn("普通初始输入").input_ids == ()


def test_guidance_packet_and_native_ir_share_input_ids_without_provider_leak() -> None:
    params = SimpleNamespace(
        tool_protocol_snapshot=make_test_protocol_snapshot(),
        tool_context=[],
        tool_ir_history=[],
        active_turn_user_inputs=[],
        live_archive_state={},
    )
    entries = [
        SimpleNamespace(guidance_id="guidance-a", message="继续原任务", metadata={}),
        SimpleNamespace(guidance_id="guidance-b", message="继续原任务", metadata={}),
    ]

    _inject_claimed_guidance(params, entries[:1], params.tool_context)
    _inject_claimed_guidance(params, entries[1:], params.tool_context)

    assert [packet["input_ids"] for packet in params.active_turn_user_inputs] == [
        ["guidance-a"], ["guidance-b"],
    ]
    assert [turn.input_ids for turn in params.tool_ir_history] == [
        tuple(packet["input_ids"]) for packet in params.active_turn_user_inputs
    ]
    assert [turn.text for turn in params.tool_ir_history] == ["继续原任务", "继续原任务"]
    messages = AnthropicMessageAdapter().to_provider_messages(params.tool_ir_history)
    assert messages == [
        {"role": "user", "content": [{"type": "text", "text": "继续原任务"}]},
        {"role": "user", "content": [{"type": "text", "text": "继续原任务"}]},
    ]
