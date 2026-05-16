from __future__ import annotations

import json
from dataclasses import replace
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest

from agent_py_agent.agent.backends.base import (
    AnthropicCompatibleBackend,
    BackendOptions,
    BaseBackend,
    EchoBackend,
    HttpBackend,
    ModelResponse,
    OpenAICompatibleBackend,
    get_backend,
)
from agent_py_agent.agent.backends.errors import ProviderTimeoutError

_DEFAULT_OPTIONS = BackendOptions(
    api_base="https://api.example.com",
    api_key="key",
    model_name="test",
    request_timeout=60,
    max_tokens=1024,
    temperature=0.2,
    stream_enabled=True,
)


def _options(**overrides) -> BackendOptions:
    return replace(_DEFAULT_OPTIONS, **overrides)


class TestModelResponse:
    def test_model_response_basic(self):
        resp = ModelResponse(text="hello", backend="echo")
        assert resp.text == "hello"
        assert resp.backend == "echo"

    def test_model_response_mutable(self):
        resp = ModelResponse(text="hi", backend="test")
        resp.text = "updated"
        assert resp.text == "updated"


class TestBaseBackend:
    def test_base_backend_not_implemented(self):
        backend = BaseBackend()
        with pytest.raises(NotImplementedError):
            backend.generate("prompt")


class TestEchoBackend:
    def test_echo_backend_name(self):
        assert EchoBackend().name == "echo"

    def test_echo_generate_empty_prompt(self):
        backend = EchoBackend()
        resp = backend.generate("")
        assert resp.backend == "echo"
        assert "空任务" in resp.text

    def test_echo_generate_no_user_task(self):
        backend = EchoBackend()
        resp = backend.generate("just some text without user task")
        assert resp.backend == "echo"
        assert "echo" in resp.text.lower()

    def test_echo_generate_with_user_task(self):
        backend = EchoBackend()
        prompt = "# User Task\n分析这个文件\n# Other Section"
        resp = backend.generate(prompt)
        assert "分析这个文件" in resp.text

    def test_echo_truncates_long_task(self):
        backend = EchoBackend()
        long_task = "x" * 500
        prompt = f"# User Task\n{long_task}\n# Other"
        resp = backend.generate(prompt)
        assert len(resp.text) < len(long_task) + 100

    def test_echo_on_chunk_not_called(self):
        backend = EchoBackend()
        on_chunk = MagicMock()
        resp = backend.generate("test prompt", on_chunk=on_chunk)
        on_chunk.assert_not_called()


class TestHttpBackendInit:
    def test_http_backend_strips_api_base_trailing_slash(self):
        backend = HttpBackend(_options(api_base="https://api.example.com/"))
        assert backend.api_base == "https://api.example.com"

    def test_http_backend_converts_timeout_to_int(self):
        backend = HttpBackend(_options(request_timeout="30"))
        assert backend.request_timeout == 30

    def test_http_backend_converts_max_tokens_to_int(self):
        backend = HttpBackend(_options(max_tokens="512"))
        assert backend.max_tokens == 512

    def test_http_backend_converts_temperature_to_float(self):
        backend = HttpBackend(_options(temperature="0.7"))
        assert backend.temperature == 0.7


class TestHttpBackendRequestJson:
    def test_request_json_missing_api_key(self):
        backend = HttpBackend(_options(api_key=""))
        with pytest.raises(ValueError, match="api_key 为空"):
            backend.request_json("/path", {}, {})

    @patch("urllib.request.urlopen")
    def test_request_json_success(self, mock_urlopen):
        mock_response = MagicMock()
        mock_response.read.return_value = b'{"result": "ok"}'
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        backend = HttpBackend(_options(api_key="test-key"))
        result = backend.request_json("/path", {"key": "value"}, {"Header": "val"})
        assert result == {"result": "ok"}

    @patch("urllib.request.urlopen")
    def test_request_json_http_error(self, mock_urlopen):
        import urllib.error

        mock_response = MagicMock()
        mock_response.read.return_value = b'{"error": "bad"}'

        exc = urllib.error.HTTPError(
            url="https://api.example.com/path",
            code=400,
            msg="Bad Request",
            hdrs={},
            fp=mock_response,
        )
        mock_urlopen.side_effect = exc

        backend = HttpBackend(_options(api_key="test-key"))
        with pytest.raises(RuntimeError, match="HTTP 400"):
            backend.request_json("/path", {}, {})

    # LLM: provider socket timeouts must be typed so CLI and runner recovery can classify them.
    # 函数用途: 模拟模型 HTTP 请求卡到 timeout，确认不再只是普通 RuntimeError。
    @patch("urllib.request.urlopen")
    def test_request_json_timeout_raises_provider_timeout(self, mock_urlopen):
        mock_urlopen.side_effect = TimeoutError("timed out")

        backend = HttpBackend(_options(api_key="test-key", request_timeout=17))

        with pytest.raises(ProviderTimeoutError, match="17s"):
            backend.request_json("/path", {}, {})


class TestHttpBackendRequestStream:
    def test_request_stream_missing_api_key(self):
        backend = HttpBackend(_options(api_key=""))
        with pytest.raises(ValueError, match="api_key 为空"):
            backend.request_stream("/path", {}, {})

    # LLM: streaming provider timeouts need the same typed boundary as non-streaming calls.
    # 函数用途: 流式接口超时时也抛 ProviderTimeoutError，避免 runner/CLI 判断分叉。
    @patch("urllib.request.urlopen")
    def test_request_stream_timeout_raises_provider_timeout(self, mock_urlopen):
        mock_urlopen.side_effect = TimeoutError("timed out")
        backend = HttpBackend(_options(api_key="test-key", request_timeout=19))

        with pytest.raises(ProviderTimeoutError, match="19s"):
            backend.request_stream("/path", {}, {})


class TestOpenAICompatibleBackend:
    def test_openai_backend_name(self):
        assert OpenAICompatibleBackend(_options()).name == "openai_compatible"

    @patch.object(OpenAICompatibleBackend, "request_stream")
    def test_generate_stream_collects_content(self, mock_request_stream):
        # request_stream already strips "data: " prefix and filters [DONE]
        mock_request_stream.return_value = [
            '{"choices": [{"delta": {"content": "hello"}}]}',
            '[DONE]',
        ]

        backend = OpenAICompatibleBackend(_options(api_key="test-key", model_name="gpt-4"))
        resp = backend.generate("test prompt", on_chunk=None)
        assert resp.text == "hello"
        assert resp.backend == "openai_compatible"

    def test_generate_with_streaming_disabled(self):
        """When stream_enabled=False, uses request_json directly."""
        backend = OpenAICompatibleBackend(_options(api_key="test-key", model_name="gpt-4", stream_enabled=False))
        with patch.object(backend, "request_json") as mock_request_json:
            mock_request_json.return_value = {
                "choices": [{"message": {"content": "direct response"}}]
            }
            resp = backend.generate("test prompt", on_chunk=None)
            assert resp.text == "direct response"

    @patch("urllib.request.urlopen")
    def test_generate_response_missing_content(self, mock_urlopen):
        mock_response = MagicMock()
        mock_response.read.return_value = b'{"choices": [{"message": {}}]}'
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        backend = OpenAICompatibleBackend(_options(api_key="test-key", model_name="gpt-4", stream_enabled=False))
        with pytest.raises(RuntimeError, match="无法解析"):
            backend.generate("test")

    @patch("urllib.request.urlopen")
    def test_generate_sets_stream_payload(self, mock_urlopen):
        mock_response = MagicMock()
        mock_response.read.return_value = b'{"choices": [{"message": {"content": "hi"}}]}'
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        backend = OpenAICompatibleBackend(_options(api_key="test-key", model_name="gpt-4"))
        backend.generate("test")
        mock_urlopen.assert_called()

    def test_generate_stream_calls_on_chunk_during_iteration(self):
        backend = OpenAICompatibleBackend(_options(api_key="test-key", model_name="gpt-4"))
        events: list[str] = []

        def request_stream_iter(path, payload, headers):
            events.append("yield-first")
            yield json.dumps({"choices": [{"delta": {"content": "hel"}}]})
            events.append("after-first")
            yield json.dumps({"choices": [{"delta": {"content": "lo"}}]})
            events.append("after-second")
            yield "[DONE]"

        def on_chunk(content: str) -> None:
            events.append(f"chunk-{content}")

        backend.request_stream_iter = request_stream_iter
        resp = backend.generate("test prompt", on_chunk=on_chunk)

        assert resp.text == "hello"
        assert events == [
            "yield-first",
            "chunk-hel",
            "after-first",
            "chunk-lo",
            "after-second",
        ]


class TestAnthropicCompatibleBackend:
    def test_anthropic_backend_name(self):
        assert AnthropicCompatibleBackend(_options()).name == "anthropic_compatible"

    def test_anthropic_custom_version(self):
        backend = AnthropicCompatibleBackend(_options(), anthropic_version="2024-01-01")
        assert backend.anthropic_version == "2024-01-01"

    @patch("urllib.request.urlopen")
    def test_generate_without_stream(self, mock_urlopen):
        mock_response = MagicMock()
        mock_response.read.return_value = b'{"content": [{"type": "text", "text": "hello"}]}'
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        backend = AnthropicCompatibleBackend(_options(api_key="test-key", model_name="claude-3", stream_enabled=False))
        resp = backend.generate("test prompt", on_chunk=None)
        assert resp.text == "hello"

    @patch("urllib.request.urlopen")
    def test_generate_with_completion_fallback(self, mock_urlopen):
        mock_response = MagicMock()
        mock_response.read.return_value = b'{"completion": "fallback text", "content": []}'
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        backend = AnthropicCompatibleBackend(_options(api_key="test-key", model_name="claude-3", stream_enabled=False))
        resp = backend.generate("test")
        assert resp.text == "fallback text"

    @patch("urllib.request.urlopen")
    def test_generate_retries_once_on_thinking_without_text(self, mock_urlopen):
        first = MagicMock()
        first.read.return_value = b'{"content": [{"thinking": "I should continue"}]}'
        first.__enter__ = MagicMock(return_value=first)
        first.__exit__ = MagicMock(return_value=False)
        second = MagicMock()
        second.read.return_value = b'{"content": [{"type": "text", "text": "after retry"}]}'
        second.__enter__ = MagicMock(return_value=second)
        second.__exit__ = MagicMock(return_value=False)
        mock_urlopen.side_effect = [first, second]

        backend = AnthropicCompatibleBackend(_options(api_key="test-key", model_name="claude-3", stream_enabled=False))
        resp = backend.generate("test")

        assert resp.text == "after retry"
        assert mock_urlopen.call_count == 2

    @patch("urllib.request.urlopen")
    def test_generate_no_text_raises(self, mock_urlopen):
        mock_response = MagicMock()
        mock_response.read.return_value = b'{"content": []}'
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        backend = AnthropicCompatibleBackend(_options(api_key="test-key", model_name="claude-3", stream_enabled=False))
        with pytest.raises(RuntimeError, match="没有文本内容"):
            backend.generate("test")

    def test_generate_stream_calls_on_chunk_during_iteration(self):
        backend = AnthropicCompatibleBackend(_options(api_key="test-key", model_name="claude-3"))
        events: list[str] = []

        def request_stream_iter(path, payload, headers):
            events.append("yield-first")
            yield json.dumps({"type": "content_block_delta", "delta": {"text": "hel"}})
            events.append("after-first")
            yield json.dumps({"type": "content_block_delta", "delta": {"text": "lo"}})
            events.append("after-second")
            yield json.dumps({"type": "message_stop"})

        def on_chunk(content: str) -> None:
            events.append(f"chunk-{content}")

        backend.request_stream_iter = request_stream_iter
        resp = backend.generate("test prompt", on_chunk=on_chunk)

        assert resp.text == "hello"
        assert events == [
            "yield-first",
            "chunk-hel",
            "after-first",
            "chunk-lo",
            "after-second",
        ]

    def test_generate_stream_retries_once_on_empty_text(self):
        backend = AnthropicCompatibleBackend(_options(api_key="test-key", model_name="claude-3"))
        calls: list[int] = []

        def request_stream(path, payload, headers):
            del path, payload, headers
            calls.append(len(calls) + 1)
            if len(calls) == 1:
                return [json.dumps({"type": "message_stop"})]
            return [
                json.dumps({"type": "content_block_delta", "delta": {"text": "after "}}),
                json.dumps({"type": "content_block_delta", "delta": {"text": "retry"}}),
                json.dumps({"type": "message_stop"}),
            ]

        backend.request_stream = request_stream
        resp = backend.generate("test prompt", on_chunk=None)

        assert resp.text == "after retry"
        assert calls == [1, 2]


class TestGetBackend:
    def test_get_backend_echo(self):
        backend = get_backend("echo")
        assert isinstance(backend, EchoBackend)
        assert backend.name == "echo"

    def test_get_backend_openai_compatible_requires_config(self):
        with pytest.raises(ValueError, match="需要传入 config"):
            get_backend("openai_compatible")

    def test_get_backend_anthropic_compatible_requires_config(self):
        with pytest.raises(ValueError, match="需要传入 config"):
            get_backend("anthropic_compatible")

    def test_get_backend_unknown(self):
        with pytest.raises(ValueError, match="未知模型后端"):
            get_backend("unknown_backend", config=MagicMock())

    def test_get_backend_openai_with_config(self):
        config = MagicMock()
        config.api_base = "https://api.example.com"
        config.api_key = "key"
        config.model_name = "gpt-4"
        config.request_timeout = 60
        config.max_tokens = 1024
        config.temperature = "0.7"
        config.stream_enabled = True
        config.anthropic_version = "2023-06-01"

        backend = get_backend("openai_compatible", config)
        assert isinstance(backend, OpenAICompatibleBackend)

    def test_get_backend_anthropic_with_config(self):
        config = MagicMock()
        config.api_base = "https://api.example.com"
        config.api_key = "key"
        config.model_name = "claude-3"
        config.request_timeout = 60
        config.max_tokens = 1024
        config.temperature = "0.7"
        config.stream_enabled = True
        config.anthropic_version = "2023-06-01"

        backend = get_backend("anthropic_compatible", config)
        assert isinstance(backend, AnthropicCompatibleBackend)
