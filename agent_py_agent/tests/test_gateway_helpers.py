"""网关助手测试 - gateway_helpers.py HTTP POST、SSE 流式请求。"""
from __future__ import annotations

import json
import urllib.error
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest


class IterableBytesIO(BytesIO):
    """BytesIO that iterates over lines."""
    def __iter__(self):
        return iter(self.readline, b"")


class TestPostJson:
    """post_json 同步 JSON 请求测试。"""

    def test_empty_api_key_raises(self):
        """验证空 api_key 抛出 ValueError。"""
        from agent_py_agent.agent.backends.gateway_helpers import post_json
        with pytest.raises(ValueError, match="api_key 为空"):
            post_json("https://api.example.com", "", "/v1/chat", {}, {}, timeout=30)

    @patch("urllib.request.urlopen")
    def test_success_response(self, mock_urlopen):
        """验证成功返回解析后的 JSON。"""
        data = json.dumps({"content": "test response"}).encode()
        mock_response = MagicMock()
        mock_response.read.return_value = data
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        from agent_py_agent.agent.backends.gateway_helpers import post_json
        result = post_json(
            "https://api.example.com",
            "test-key",
            "/v1/chat",
            {"model": "gpt-4"},
            {"Content-Type": "application/json"},
            timeout=30,
        )
        assert result == {"content": "test response"}

    @patch("urllib.request.urlopen")
    def test_http_error_handling(self, mock_urlopen):
        """验证 HTTP 错误被包装为 RuntimeError。"""
        from io import BytesIO
        body = BytesIO(b"invalid key")
        mock_urlopen.side_effect = urllib.error.HTTPError(
            "https://api.example.com", 401, "Unauthorized",
            {"Content-Type": "application/json"}, body
        )

        from agent_py_agent.agent.backends.gateway_helpers import post_json
        with pytest.raises(RuntimeError, match="HTTP 401"):
            post_json(
                "https://api.example.com",
                "bad-key",
                "/v1/chat",
                {},
                {},
                timeout=30,
            )

    @patch("urllib.request.urlopen")
    def test_url_error_is_wrapped_with_endpoint_hint(self, mock_urlopen):
        """验证 DNS/网络错误被包装成可读提示。"""
        mock_urlopen.side_effect = urllib.error.URLError("[Errno 11001] getaddrinfo failed")

        from agent_py_agent.agent.backends.gateway_helpers import post_json
        with pytest.raises(RuntimeError, match="网络请求失败.*api.example.com"):
            post_json(
                "https://api.example.com",
                "test-key",
                "/v1/chat",
                {},
                {},
                timeout=30,
            )


class TestPostStream:
    """post_stream 流式请求收集测试。"""

    def test_empty_api_key_raises(self):
        """验证空 api_key 抛出 ValueError。"""
        from agent_py_agent.agent.backends.gateway_helpers import post_stream
        with pytest.raises(ValueError, match="api_key 为空"):
            post_stream("https://api.example.com", "", "/v1/chat", {"stream": True}, {}, timeout=30)

    @patch("urllib.request.urlopen")
    def test_stream_collects_data_lines(self, mock_urlopen):
        """验证收集 SSE data 行。"""
        lines = [
            b"event: message",
            b'data: {"content": "line1"}',
            b'data: {"content": "line2"}',
            b"data: [DONE]",
        ]
        mock_response = MagicMock()
        mock_response.__iter__ = MagicMock(return_value=iter(lines))
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        from agent_py_agent.agent.backends.gateway_helpers import post_stream
        result = post_stream(
            "https://api.example.com",
            "test-key",
            "/v1/chat",
            {},
            {},
            timeout=30,
        )
        assert '{"content": "line1"}' in result
        assert '{"content": "line2"}' in result

    @patch("urllib.request.urlopen")
    def test_stream_skips_comments_and_events(self, mock_urlopen):
        """验证跳过注释行和 event 行。"""
        lines = [
            b": this is a comment",
            b"event: ping",
            b'data: {"content": "actual"}',
        ]
        mock_response = MagicMock()
        mock_response.__iter__ = MagicMock(return_value=iter(lines))
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        from agent_py_agent.agent.backends.gateway_helpers import post_stream
        result = post_stream(
            "https://api.example.com",
            "test-key",
            "/v1/chat",
            {},
            {},
            timeout=30,
        )
        assert len(result) == 1
        assert "actual" in result[0]

    @patch("urllib.request.urlopen")
    def test_stream_skips_empty_lines(self, mock_urlopen):
        """验证跳过空行。"""
        lines = [b"", b"   ", b'data: {"content": "text"}']
        mock_response = MagicMock()
        mock_response.__iter__ = MagicMock(return_value=iter(lines))
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        from agent_py_agent.agent.backends.gateway_helpers import post_stream
        result = post_stream(
            "https://api.example.com",
            "test-key",
            "/v1/chat",
            {},
            {},
            timeout=30,
        )
        assert len(result) == 1


class TestPostStreamIter:
    """post_stream_iter 流式迭代器测试。"""

    def test_empty_api_key_raises(self):
        """验证空 api_key 抛出 ValueError。"""
        from agent_py_agent.agent.backends.gateway_helpers import post_stream_iter
        with pytest.raises(ValueError, match="api_key 为空"):
            list(post_stream_iter("https://api.example.com", "", "/v1/chat", {"stream": True}, {}, timeout=30))

    @patch("urllib.request.urlopen")
    def test_iter_yields_data_lines(self, mock_urlopen):
        """验证生成器逐行产出。"""
        lines = [
            b'data: {"token": "hello"}',
            b'data: {"token": "world"}',
        ]
        mock_response = MagicMock()
        mock_response.__iter__ = MagicMock(return_value=iter(lines))
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        from agent_py_agent.agent.backends.gateway_helpers import post_stream_iter
        result = list(post_stream_iter(
            "https://api.example.com",
            "test-key",
            "/v1/chat",
            {},
            {},
            timeout=30,
        ))
        assert len(result) == 2
        assert '{"token": "hello"}' in result[0]

    @patch("urllib.request.urlopen")
    def test_iter_handles_http_error(self, mock_urlopen):
        """验证 HTTP 错误被包装为 RuntimeError。"""
        from io import BytesIO
        body = BytesIO(b"internal error")
        mock_urlopen.side_effect = urllib.error.HTTPError(
            "https://api.example.com", 500, "Server Error",
            {"Content-Type": "application/json"}, body
        )

        from agent_py_agent.agent.backends.gateway_helpers import post_stream_iter
        with pytest.raises(RuntimeError, match="HTTP 500"):
            list(post_stream_iter(
                "https://api.example.com",
                "test-key",
                "/v1/chat",
                {},
                {},
                timeout=30,
            ))

    @patch("urllib.request.urlopen")
    def test_iter_handles_url_error(self, mock_urlopen):
        """验证流式 DNS/网络错误也被包装。"""
        mock_urlopen.side_effect = urllib.error.URLError("[Errno 11001] getaddrinfo failed")

        from agent_py_agent.agent.backends.gateway_helpers import post_stream_iter
        with pytest.raises(RuntimeError, match="网络请求失败.*api.example.com"):
            list(post_stream_iter(
                "https://api.example.com",
                "test-key",
                "/v1/chat",
                {},
                {},
                timeout=30,
            ))

    @patch("urllib.request.urlopen")
    def test_iter_stops_on_empty_stream(self, mock_urlopen):
        """验证空流式响应正常结束。"""
        mock_response = MagicMock()
        mock_response.__iter__ = MagicMock(return_value=iter([]))
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        from agent_py_agent.agent.backends.gateway_helpers import post_stream_iter
        result = list(post_stream_iter(
            "https://api.example.com",
            "test-key",
            "/v1/chat",
            {},
            {},
            timeout=30,
        ))
        assert result == []
