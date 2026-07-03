"""文本响应撞输出上限的自动续写(§3 报满不早停):触发/不触发/失败保底/工具截断不受扰。"""

from __future__ import annotations

import json
from dataclasses import replace

from agent_py_agent.agent.backends.base import AnthropicCompatibleBackend, BackendOptions

_OPTIONS = BackendOptions(
    api_base="https://api.example.com",
    api_key="key",
    model_name="test",
    request_timeout=60,
    max_tokens=64,
    temperature=0.2,
    stream_enabled=True,
)


def _sse(events: list[dict]) -> list[str]:
    return [json.dumps(event, ensure_ascii=False) for event in events]


def _text_stream(text: str, stop_reason: str) -> list[str]:
    return _sse(
        [
            {"type": "content_block_delta", "delta": {"text": text}},
            {"type": "message_delta", "delta": {"stop_reason": stop_reason}},
            {"type": "message_stop"},
        ]
    )


def _backend_with_streams(streams: list[list[str]]) -> tuple[AnthropicCompatibleBackend, list[dict]]:
    backend = AnthropicCompatibleBackend(_OPTIONS)
    calls: list[dict] = []

    def fake_stream(path: str, payload: dict, headers: dict) -> list[str]:
        calls.append(json.loads(json.dumps(payload)))
        index = min(len(calls), len(streams)) - 1
        return streams[index]

    backend.request_stream = fake_stream  # type: ignore[method-assign]
    return backend, calls


def test_max_tokens_text_auto_continues_and_concatenates() -> None:
    backend, calls = _backend_with_streams(
        [_text_stream("条目1..条目40", "max_tokens"), _text_stream(";条目41..条目50", "end_turn")]
    )

    response = backend.generate("逐条判 50 条")

    assert response.text == "条目1..条目40;条目41..条目50"
    assert response.stop_reason == "end_turn"
    assert response.truncated is False
    assert len(calls) == 2
    # 续写请求=原对话 + 已收文本作 assistant 预填(模型从中断处接续,不重复)。
    assert calls[1]["messages"][-1] == {"role": "assistant", "content": "条目1..条目40"}
    assert calls[1]["messages"][0] == calls[0]["messages"][0]


def test_end_turn_does_not_continue() -> None:
    backend, calls = _backend_with_streams([_text_stream("完整答案", "end_turn")])

    response = backend.generate("prompt")

    assert response.text == "完整答案"
    assert len(calls) == 1


def test_continuation_rounds_are_bounded() -> None:
    backend, calls = _backend_with_streams([_text_stream("段", "max_tokens")] * 10)

    response = backend.generate("prompt")

    assert response.text == "段" * 4  # 首段 + 3 轮续写上限
    assert len(calls) == 4
    assert response.stop_reason == "max_tokens"


def test_continuation_failure_keeps_partial_text() -> None:
    backend = AnthropicCompatibleBackend(_OPTIONS)
    calls: list[dict] = []

    def fake_stream(path: str, payload: dict, headers: dict) -> list[str]:
        calls.append(payload)
        if len(calls) == 1:
            return _text_stream("半截产出", "max_tokens")
        raise RuntimeError("prefill rejected")

    backend.request_stream = fake_stream  # type: ignore[method-assign]

    response = backend.generate("prompt")

    assert response.text == "半截产出"
    assert response.stop_reason == "max_tokens"
    assert len(calls) == 2


def test_open_tool_buffer_truncation_not_text_continued() -> None:
    # 半截 tool_use 参数(native 截断恢复的领地):不做文本续写,truncated 照旧为 True。
    lines = _sse(
        [
            {"type": "content_block_delta", "delta": {"text": "先说两句"}},
            {
                "type": "content_block_start",
                "index": 1,
                "content_block": {"type": "tool_use", "id": "t1", "name": "write_file"},
            },
            {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '{"pa'}},
            {"type": "message_delta", "delta": {"stop_reason": "max_tokens"}},
            {"type": "message_stop"},
        ]
    )
    backend, calls = _backend_with_streams([lines])

    response = backend.generate("prompt", tools=[{"name": "write_file"}])

    assert response.truncated is True
    assert response.text == "先说两句"
    assert len(calls) == 1


def test_tool_use_stop_reason_not_continued() -> None:
    lines = _sse(
        [
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "tool_use", "id": "t1", "name": "read_file"},
            },
            {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": '{"path":"a"}'}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
            {"type": "message_stop"},
        ]
    )
    backend, calls = _backend_with_streams([lines])

    response = backend.generate("prompt", tools=[{"name": "read_file"}])

    assert len(calls) == 1
    assert response.tool_use_blocks and response.tool_use_blocks[0]["name"] == "read_file"
    assert response.stop_reason == "tool_use"


def test_non_stream_records_stop_reason() -> None:
    backend = AnthropicCompatibleBackend(replace(_OPTIONS, stream_enabled=False))
    backend.request_json = lambda path, payload, headers: {  # type: ignore[method-assign]
        "content": [{"type": "text", "text": "答案"}],
        "stop_reason": "max_tokens",
        "usage": {"output_tokens": 64},
    }

    response = backend.generate("prompt")

    assert response.text == "答案"
    assert response.stop_reason == "max_tokens"
