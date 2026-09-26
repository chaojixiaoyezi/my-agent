"""后端适配器请求载荷约定检查。"""

import json
from dataclasses import replace

import pytest

from agent_py_agent.agent.backends import (
    AnthropicCompatibleBackend,
    BackendOptions,
    OpenAICompatibleBackend,
)

_OPENAI_OPTIONS = BackendOptions(api_base="http://fake/v1", api_key="k", model_name="m")
_ANTHROPIC_OPTIONS = BackendOptions(api_base="http://fake/anthropic", api_key="k", model_name="m")


class FakeOpenAI(OpenAICompatibleBackend):
    def __init__(self, *, stream_enabled=False):
        super().__init__(replace(_OPENAI_OPTIONS, stream_enabled=stream_enabled))
        self.seen = None

    def request_json(self, path, payload, headers):
        self.seen = (path, payload, headers)
        return {"choices": [{"message": {"content": "openai ok"}}]}

    def request_stream(self, path, payload, headers):
        self.seen = (path, payload, headers)
        return [
            json.dumps({"choices": [{"delta": {"content": "openai "}}]}),
            json.dumps({"choices": [{"delta": {"content": "stream ok"}}]}),
            "[DONE]",
        ]


class FakeAnthropic(AnthropicCompatibleBackend):
    def __init__(self, *, stream_enabled=False):
        super().__init__(replace(_ANTHROPIC_OPTIONS, stream_enabled=stream_enabled))
        self.seen = None

    def request_json(self, path, payload, headers):
        self.seen = (path, payload, headers)
        return {"content": [{"type": "text", "text": "anthropic ok"}]}

    def request_stream(self, path, payload, headers):
        self.seen = (path, payload, headers)
        return [
            json.dumps({"type": "content_block_delta", "delta": {"text": "anthropic "}}),
            json.dumps({"type": "content_block_delta", "delta": {"text": "stream ok"}}),
            json.dumps({"type": "message_stop"}),
        ]


def test_openai_payload():
    backend = FakeOpenAI()
    response = backend.generate("hello")
    assert response.text == "openai ok"
    path, payload, headers = backend.seen
    assert path == "/chat/completions"
    assert payload["messages"][0]["content"] == "hello"
    assert headers["Authorization"] == "Bearer k"


def test_anthropic_payload():
    backend = FakeAnthropic()
    response = backend.generate("hello")
    assert response.text == "anthropic ok"
    path, payload, headers = backend.seen
    assert path == "/v1/messages"
    assert payload["messages"][0]["content"] == "hello"
    assert headers["Authorization"] == "Bearer k"
    assert "anthropic-version" in headers


def test_openai_streaming_parses_sse_chunks():
    backend = FakeOpenAI(stream_enabled=True)
    response = backend.generate("hello")
    assert response.text == "openai stream ok"
    path, payload, headers = backend.seen
    assert path == "/chat/completions"


def test_anthropic_streaming_parses_sse_events():
    backend = FakeAnthropic(stream_enabled=True)
    response = backend.generate("hello")
    assert response.text == "anthropic stream ok"
    path, payload, headers = backend.seen
    assert path == "/v1/messages"


def test_streaming_fallback_to_non_stream():
    """stream_enabled=False 时走 request_json 路径。"""
    backend = FakeOpenAI(stream_enabled=False)
    response = backend.generate("hello")
    assert response.text == "openai ok"


def test_streaming_skips_malformed_json():
    """流式解析跳过无法解析的 JSON 行。"""

    class PartialBroken(OpenAICompatibleBackend):
        def __init__(self):
            super().__init__(replace(_OPENAI_OPTIONS, stream_enabled=True))

        def request_stream(self, path, payload, headers):
            return [
                json.dumps({"choices": [{"delta": {"content": "ok "}}]}),
                "not-json",
                json.dumps({"choices": [{"delta": {"content": "fine"}}]}),
                "[DONE]",
            ]

    backend = PartialBroken()
    response = backend.generate("hello")
    assert response.text == "ok fine"


# LLM: 用真实适配器和 gateway 的发送前内容作 oracle；只替换物理 I/O，不能把投影返回值当发送结果。
# 函数用途: 捕获非流式和 SSE 原组包后的完整 JSON，验证预览完全无副作用。
def _capture_provider_payload(monkeypatch, *, anthropic):
    from agent_py_agent.agent.backends import gateway_helpers, http

    seen = []

    def capture_json(request):
        seen.append(json.loads(json.dumps(request.payload, ensure_ascii=False)))
        if anthropic:
            return {"content": [{"type": "text", "text": "ok"}]}
        return {"choices": [{"message": {"content": "ok"}}]}

    def capture_stream(request):
        seen.append(json.loads(json.dumps(request.payload, ensure_ascii=False)))
        if anthropic:
            return iter([
                json.dumps({"type": "content_block_delta", "delta": {"text": "ok"}}),
                json.dumps({"type": "message_stop"}),
            ])
        return iter([json.dumps({"choices": [{"delta": {"content": "ok"}}]}), "[DONE]"])

    monkeypatch.setattr(http, "post_json", capture_json)
    monkeypatch.setattr(gateway_helpers, "_stream_with_watchdog", capture_stream)
    return seen


# 参数来自协议能力而非生产型号映射；三条真实候选通道的窗口在上层分别验证。


@pytest.mark.parametrize("backend_type", [AnthropicCompatibleBackend, OpenAICompatibleBackend])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("mode", ["auto", "required", "none"])
@pytest.mark.parametrize("structured", [False, True])
def test_project_generate_payload_matches_actual_wire_without_side_effects(
    monkeypatch, tmp_path, backend_type, stream, mode, structured,
):
    from copy import deepcopy

    from agent_py_agent.agent.backends.base import ProviderRequestOptions
    from agent_py_agent.agent.prompting_parts.cache_layout import CacheStructuredPrompt
    from agent_py_agent.agent.tooling.runtime_contracts import ToolChoice

    backend = backend_type(BackendOptions(
        api_base="https://opencode.ai/zen/v1", api_key="not-a-real-secret", model_name="candidate",
        stream_enabled=stream, max_tokens=3072, top_p=0.7, temperature=0.1, temperature_explicit=True,
    ))
    tools = [{"name": "read_file", "description": "读文件", "input_schema": {
        "type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"],
    }}]
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "实际 child 输入"}]},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "call-1", "name": "read_file", "input": {"path": "a.txt"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call-1", "content": "真实工具结果"}]},
    ] if structured else None
    prompt = CacheStructuredPrompt(stable_prefix="稳定规则", volatile_suffix="当前事实") if structured else "实际用户输入"
    options = ProviderRequestOptions(system_instruction="宿主 system", thinking_disabled=mode != "auto")
    choice = ToolChoice.required() if mode == "required" else ToolChoice.none() if mode == "none" else ToolChoice.auto()
    original = deepcopy((tools, messages))
    backend_before = dict(backend.__dict__)
    seen = _capture_provider_payload(monkeypatch, anthropic=backend_type is AnthropicCompatibleBackend)
    private_dump = tmp_path / "wire.jsonl"
    monkeypatch.setenv("MY_AGENT_PROVIDER_DUMP", str(private_dump))

    projected = backend.project_generate_payload(prompt, tools=tools, tool_choice=choice, messages=messages, request_options=options)
    assert seen == []
    assert not private_dump.exists()
    assert backend.__dict__ == backend_before
    assert (tools, messages) == original
    assert projected["max_tokens"] == 3072
    assert "not-a-real-secret" not in json.dumps(projected)

    backend.generate(prompt, tools=tools, tool_choice=choice, messages=messages, request_options=options)
    assert seen == [projected]
    assert (tools, messages) == original
    # 投影可供只读验证方独立保存；不能经返回值修改下一次真实请求的 schema。
    projected["messages"].clear()
    assert (tools, messages) == original


@pytest.mark.parametrize("backend_type", [AnthropicCompatibleBackend, OpenAICompatibleBackend])
def test_project_generate_payload_does_not_claim_overridden_sender(backend_type):
    class AnotherProtocol(backend_type):
        def generate(self, prompt, **kwargs):
            raise AssertionError("preview must not send")

    backend = AnotherProtocol(_OPENAI_OPTIONS)
    assert backend.project_generate_payload("prompt") is None


def test_responses_backend_cannot_inherit_chat_payload_proof():
    from agent_py_agent.agent.backends.responses import OpenAIResponsesBackend

    backend = OpenAIResponsesBackend(_OPENAI_OPTIONS)
    assert backend.project_generate_payload("prompt") is None


@pytest.mark.parametrize("protocol", ["anthropic", "chat", "responses"])
@pytest.mark.parametrize("catalog", ["none", "empty", "provided"])
def test_none_choice_preserves_catalog_in_actual_protocol_payload(monkeypatch, protocol, catalog):
    from copy import deepcopy

    from agent_py_agent.agent.backends.responses import OpenAIResponsesBackend
    from agent_py_agent.agent.tooling.runtime_contracts import ToolChoice

    backend_type = {"anthropic": AnthropicCompatibleBackend, "chat": OpenAICompatibleBackend,
                    "responses": OpenAIResponsesBackend}[protocol]
    backend = backend_type(replace(_ANTHROPIC_OPTIONS if protocol == "anthropic" else _OPENAI_OPTIONS,
                                   stream_enabled=False))
    captured = []
    response = {"content": [{"type": "text", "text": "摘要"}], "stop_reason": "end_turn",
                "choices": [{"message": {"content": "摘要"}, "finish_reason": "stop"}],
                "status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": "摘要"}]}]}
    monkeypatch.setattr(backend, "request_json", lambda _path, payload, _headers: captured.append(deepcopy(payload)) or response)
    monkeypatch.setattr("socket.create_connection", lambda *_args, **_kwargs: pytest.fail("fixture must never open a socket"))
    schema = {"name": "read", "description": "只保留目录", "input_schema": {
        "type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"],
    }}
    tools = None if catalog == "none" else [] if catalog == "empty" else [deepcopy(schema)]

    result = backend.generate("只总结", tools=tools, tool_choice=ToolChoice.none("summary_only"))

    assert result.text == "摘要" and len(captured) == 1
    payload = captured[0]
    if catalog == "provided":
        assert payload["tool_choice"] == ({"type": "none"} if protocol == "anthropic" else "none")
        item = payload["tools"][0]
        original = item["function"] if protocol == "chat" else item
        assert original["name"] == schema["name"]
        assert original["input_schema" if protocol == "anthropic" else "parameters"] == schema["input_schema"]
        assert tools == [schema]
    else:
        assert "tools" not in payload
        assert payload.get("tool_choice") == ("none" if protocol == "chat" else None)
    assert "thinking" not in payload and "reasoning_effort" not in payload and "reasoning" not in payload
