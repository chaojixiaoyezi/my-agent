from __future__ import annotations

from agent_py_agent.agent.backends.message_adapter import (
    AnthropicMessageAdapter,
    MessageAdapter,
)
from agent_py_agent.agent.backends.tool_ir import AssistantTurn, UserTurn
from agent_py_agent.tests._tool_runtime_harness import (
    canonical_history_call,
    canonical_history_result,
)


def _adapter() -> AnthropicMessageAdapter:
    return AnthropicMessageAdapter()


def _call(call_id: str, name: str, arguments: dict[str, object]):
    return canonical_history_call(name, arguments, call_id=call_id)


def _result(call_id: str, content: str, *, ok: bool = True):
    call = _call(call_id, "test_tool", {})
    return canonical_history_result(call, content, ok=ok)


def test_adapter_is_messageadapter_subclass():
    assert isinstance(_adapter(), MessageAdapter)


# --- outbound: assistant text + tool_use in the SAME message -----------------


def test_assistant_text_and_tool_use_share_one_message():
    history = [
        AssistantTurn(
            text="let me read it",
            tool_calls=[_call("toolu_1", "read_file", {"path": "README.md"})],
        )
    ]

    messages = _adapter().to_provider_messages(history)

    assert messages == [
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "let me read it"},
                {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "read_file",
                    "input": {"path": "README.md"},
                },
            ],
        }
    ]


def test_assistant_replays_ordered_thinking_blocks_and_uses_canonical_tool_input():
    turn = AssistantTurn(
        text="我先读取。",
        tool_calls=[
            _call("toolu_1", "read_file", {"path": "README.md"})
        ],
        content_blocks=[
            {
                "type": "thinking",
                "thinking": "内部推理",
                "signature": "sig-1",
                "output_only": "drop-me",
            },
            {"type": "text", "text": "我先读取。", "citations": None},
            {
                "type": "tool_use",
                "id": "toolu_1",
                "name": "wrong-name",
                "input": {"path": "raw-secret-path"},
                "caller": {"type": "direct"},
            },
        ],
    )

    messages = _adapter().to_provider_messages([turn])

    assert messages == [
        {
            "role": "assistant",
            "content": [
                {
                    "type": "thinking",
                    "thinking": "内部推理",
                    "signature": "sig-1",
                },
                {"type": "text", "text": "我先读取。"},
                {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "read_file",
                    "input": {"path": "README.md"},
                },
            ],
        }
    ]

def test_assistant_text_block_omitted_when_text_empty():
    history = [
        AssistantTurn(tool_calls=[_call("toolu_1", "read_file", {"path": "a"})])
    ]

    messages = _adapter().to_provider_messages(history)

    assert messages[0]["content"] == [
        {"type": "tool_use", "id": "toolu_1", "name": "read_file", "input": {"path": "a"}}
    ]


def test_text_only_turn_yields_text_block_only():
    messages = _adapter().to_provider_messages([AssistantTurn(text="final answer")])

    assert messages == [
        {"role": "assistant", "content": [{"type": "text", "text": "final answer"}]}
    ]


def test_empty_turn_is_dropped():
    assert _adapter().to_provider_messages([AssistantTurn()]) == []


def test_current_turn_user_input_keeps_its_chronological_position():
    history = [
        AssistantTurn(tool_calls=[_call("t1", "read_file", {})]),
        _result("t1", "old result"),
        UserTurn("请改为先验证标准 wheel。"),
        AssistantTurn(tool_calls=[_call("t2", "run_command", {})]),
        _result("t2", "new result"),
    ]

    messages = _adapter().to_provider_messages(history)

    assert [message["role"] for message in messages] == [
        "assistant",
        "user",
        "user",
        "assistant",
        "user",
    ]
    assert messages[2] == {
        "role": "user",
        "content": [{"type": "text", "text": "请改为先验证标准 wheel。"}],
    }


# --- outbound: tool_result blocks -------------------------------------------


def test_tool_result_becomes_user_message_block_with_is_error():
    result = _result("toolu_1", "file body")
    history = [result]

    messages = _adapter().to_provider_messages(history)

    assert messages == [
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_1",
                    "content": result.render_for_prompt(),
                    "is_error": False,
                }
            ],
        }
    ]


def test_error_result_sets_is_error_true():
    messages = _adapter().to_provider_messages(
        [_result("t", "boom", ok=False)]
    )

    assert messages[0]["content"][0]["is_error"] is True


# --- outbound: contiguous tool_result merge (Anthropic requirement) ----------


def test_multiple_results_in_one_batch_merge_into_single_user_message():
    history = [
        [
            _result("toolu_1", "a"),
            _result("toolu_2", "b"),
        ]
    ]

    messages = _adapter().to_provider_messages(history)

    assert len(messages) == 1
    assert messages[0]["role"] == "user"
    assert [b["tool_use_id"] for b in messages[0]["content"]] == ["toolu_1", "toolu_2"]


def test_adjacent_result_items_merge_across_batches():
    # Two separate history items, both results -> still one user message.
    history = [
        _result("toolu_1", "a"),
        _result("toolu_2", "b"),
    ]

    messages = _adapter().to_provider_messages(history)

    assert len(messages) == 1
    assert [b["tool_use_id"] for b in messages[0]["content"]] == ["toolu_1", "toolu_2"]


# --- outbound: a full interleaved round-trip ---------------------------------


def test_full_round_assistant_then_results_then_final_text():
    history = [
        AssistantTurn(
            text="reading two files",
            tool_calls=[
                _call("toolu_1", "read_file", {"path": "a"}),
                _call("toolu_2", "read_file", {"path": "b"}),
            ],
        ),
        [
            _result("toolu_1", "A"),
            _result("toolu_2", "B"),
        ],
        AssistantTurn(text="done"),
    ]

    messages = _adapter().to_provider_messages(history)

    assert [m["role"] for m in messages] == ["assistant", "user", "assistant"]
    # round 1 assistant: text + two tool_use blocks
    assert messages[0]["content"][0] == {"type": "text", "text": "reading two files"}
    assert [b["type"] for b in messages[0]["content"]] == ["text", "tool_use", "tool_use"]
    # results merged into one user message, in order
    assert [b["tool_use_id"] for b in messages[1]["content"]] == ["toolu_1", "toolu_2"]
    # final assistant turn
    assert messages[2]["content"] == [{"type": "text", "text": "done"}]


def test_two_separate_tool_rounds_do_not_cross_merge():
    history = [
        AssistantTurn(tool_calls=[_call("t1", "read_file", {})]),
        _result("t1", "A"),
        AssistantTurn(tool_calls=[_call("t2", "read_file", {})]),
        _result("t2", "B"),
    ]

    messages = _adapter().to_provider_messages(history)

    # assistant / user / assistant / user — the intervening assistant breaks the merge.
    assert [m["role"] for m in messages] == ["assistant", "user", "assistant", "user"]
    assert messages[1]["content"][0]["tool_use_id"] == "t1"
    assert messages[3]["content"][0]["tool_use_id"] == "t2"


# Inbound authority lives only in backends.tool_protocol_adapter, where run,
# schema and attempt identity are available.  A history translator must never
# manufacture calls from provider response dictionaries.
def test_history_adapter_has_no_inbound_tool_call_authority() -> None:
    assert not hasattr(_adapter(), "tool_calls_from_response")
