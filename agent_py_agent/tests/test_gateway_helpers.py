"""网关助手测试 - gateway_helpers.py HTTP POST、SSE 流式请求。"""
from __future__ import annotations

import json
import urllib.error
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest

from agent_py_agent.agent.backends.gateway_helpers import GatewayRequest


class IterableBytesIO(BytesIO):
    """BytesIO that iterates over lines."""
    def __iter__(self):
        return iter(self.readline, b"")


def _request(api_key: str = "test-key", payload: dict | None = None) -> GatewayRequest:
    return GatewayRequest(
        api_base="https://api.example.com",
        api_key=api_key,
        path="/v1/chat",
        payload=payload or {},
        headers={"Content-Type": "application/json"},
        timeout=30,
    )


class TestPostJson:
    """post_json 同步 JSON 请求测试。"""

    def test_empty_api_key_raises(self):
        """验证空 api_key 抛出 ValueError。"""
        from agent_py_agent.agent.backends.gateway_helpers import post_json
        with pytest.raises(ValueError, match="api_key 为空"):
            post_json(_request(api_key=""))

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
        result = post_json(_request(payload={"model": "gpt-4"}))
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
            post_json(_request(api_key="bad-key"))

    @patch("agent_py_agent.agent.backends.gateway_helpers.time.sleep")
    @patch("urllib.request.urlopen")
    def test_retryable_http_error_retries_before_wrapping(self, mock_urlopen, mock_sleep):
        """验证模型服务临时过载时会短暂重试，而不是一次 529 直接打断长任务。"""
        from io import BytesIO

        first = urllib.error.HTTPError(
            "https://api.example.com",
            529,
            "Overloaded",
            {"Content-Type": "application/json"},
            BytesIO(b'{"error": "overloaded"}'),
        )
        second = MagicMock()
        second.read.return_value = json.dumps({"content": "after retry"}).encode()
        second.__enter__ = MagicMock(return_value=second)
        second.__exit__ = MagicMock(return_value=False)
        mock_urlopen.side_effect = [first, second]

        from agent_py_agent.agent.backends.gateway_helpers import post_json

        assert post_json(_request()) == {"content": "after retry"}
        assert mock_urlopen.call_count == 2
        mock_sleep.assert_called_once()

    @patch("agent_py_agent.agent.backends.gateway_helpers.time.sleep")
    @patch("urllib.request.urlopen")
    def test_retryable_network_disconnect_retries_before_success(self, mock_urlopen, mock_sleep):
        """验证模型接口偶发断线会先短暂重试，避免真实 E2E 因一次 EOF 误判子代理失败。"""
        transient = urllib.error.URLError("Remote end closed connection without response")
        second = MagicMock()
        second.read.return_value = json.dumps({"content": "after reconnect"}).encode()
        second.__enter__ = MagicMock(return_value=second)
        second.__exit__ = MagicMock(return_value=False)
        mock_urlopen.side_effect = [transient, second]

        from agent_py_agent.agent.backends.gateway_helpers import post_json

        assert post_json(_request()) == {"content": "after reconnect"}
        assert mock_urlopen.call_count == 2
        mock_sleep.assert_called_once()

    @patch("agent_py_agent.agent.backends.gateway_helpers.time.sleep")
    @patch("urllib.request.urlopen")
    def test_proxy_tunnel_503_retries_before_success(self, mock_urlopen, mock_sleep):
        """验证代理隧道层 503 也按临时 provider 网络问题重试。"""
        transient = urllib.error.URLError("Tunnel connection failed: 503 Service Unavailable")
        second = MagicMock()
        second.read.return_value = json.dumps({"content": "after proxy retry"}).encode()
        second.__enter__ = MagicMock(return_value=second)
        second.__exit__ = MagicMock(return_value=False)
        mock_urlopen.side_effect = [transient, second]

        from agent_py_agent.agent.backends.gateway_helpers import post_json

        assert post_json(_request()) == {"content": "after proxy retry"}
        assert mock_urlopen.call_count == 2
        mock_sleep.assert_called_once()

    @patch("agent_py_agent.agent.backends.gateway_helpers.time.sleep")
    @patch("urllib.request.urlopen")
    def test_retryable_network_disconnect_exhaustion_is_transient_error(self, mock_urlopen, mock_sleep):
        """验证多次断线后仍归类为 provider 临时错误，方便父级重试/接管而不是当业务失败。"""
        from agent_py_agent.agent.backends.errors import ProviderTransientError
        from agent_py_agent.agent.backends.gateway_helpers import post_json

        mock_urlopen.side_effect = [
            urllib.error.URLError("Remote end closed connection without response")
            for _ in range(4)
        ]

        with pytest.raises(ProviderTransientError, match="临时断开|连接被重置"):
            post_json(_request())
        assert mock_urlopen.call_count == 4
        assert mock_sleep.call_count == 3

    @patch("agent_py_agent.agent.backends.gateway_helpers.time.sleep")
    @patch("urllib.request.urlopen")
    def test_retryable_http_exhaustion_is_transient_error(self, mock_urlopen, mock_sleep):
        """验证 429/529 重试耗尽后仍是 provider 临时错误，避免真实 run 只暴露 HTTP traceback。"""
        from agent_py_agent.agent.backends.errors import ProviderTransientError
        from agent_py_agent.agent.backends.gateway_helpers import post_json

        def _rate_limit_error() -> urllib.error.HTTPError:
            return urllib.error.HTTPError(
                "https://api.example.com",
                429,
                "Too Many Requests",
                {"Content-Type": "application/json"},
                BytesIO(b'{"error":{"type":"rate_limit_error","message":"plan limited"}}'),
            )

        mock_urlopen.side_effect = [_rate_limit_error() for _ in range(4)]

        with pytest.raises(ProviderTransientError, match="HTTP 429.*plan limited"):
            post_json(_request())
        assert mock_urlopen.call_count == 4
        assert mock_sleep.call_count == 3

    @patch("agent_py_agent.agent.backends.gateway_helpers.time.sleep")
    @patch("urllib.request.urlopen")
    def test_timeout_does_not_retry_as_transient_disconnect(self, mock_urlopen, mock_sleep):
        """验证超时仍走 provider_timeout，不和断线重试混在一起。"""
        from agent_py_agent.agent.backends.errors import ProviderTimeoutError
        from agent_py_agent.agent.backends.gateway_helpers import post_json

        mock_urlopen.side_effect = TimeoutError("timed out")

        with pytest.raises(ProviderTimeoutError, match="请求超时"):
            post_json(_request())
        assert mock_urlopen.call_count == 1
        mock_sleep.assert_not_called()

    @patch("urllib.request.urlopen")
    def test_url_error_is_wrapped_with_endpoint_hint(self, mock_urlopen):
        """验证 DNS/网络错误被包装成可读提示。"""
        mock_urlopen.side_effect = urllib.error.URLError("[Errno 11001] getaddrinfo failed")

        from agent_py_agent.agent.backends.gateway_helpers import post_json
        with pytest.raises(RuntimeError, match="网络请求失败.*api.example.com"):
            post_json(_request())


class TestPostStream:
    """post_stream 流式请求收集测试。"""

    def test_empty_api_key_raises(self):
        """验证空 api_key 抛出 ValueError。"""
        from agent_py_agent.agent.backends.gateway_helpers import post_stream
        with pytest.raises(ValueError, match="api_key 为空"):
            post_stream(_request(api_key="", payload={"stream": True}))

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
        result = post_stream(_request())
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
        result = post_stream(_request())
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
        result = post_stream(_request())
        assert len(result) == 1

    @patch("agent_py_agent.agent.backends.gateway_helpers.time.monotonic")
    @patch("urllib.request.urlopen")
    def test_stream_enforces_total_timeout_on_heartbeat_lines(self, mock_urlopen, mock_monotonic):
        """流式服务持续发心跳但不结束时，也会按 request_timeout 总时长退出。"""
        from agent_py_agent.agent.backends.errors import ProviderTimeoutError
        from agent_py_agent.agent.backends.gateway_helpers import post_stream

        mock_monotonic.side_effect = [0, 31]
        mock_response = MagicMock()
        mock_response.__iter__ = MagicMock(return_value=iter([b": ping"]))
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        with pytest.raises(ProviderTimeoutError, match="流式响应超时"):
            post_stream(_request())


class TestPostStreamIter:
    """post_stream_iter 流式迭代器测试。"""

    def test_empty_api_key_raises(self):
        """验证空 api_key 抛出 ValueError。"""
        from agent_py_agent.agent.backends.gateway_helpers import post_stream_iter
        with pytest.raises(ValueError, match="api_key 为空"):
            list(post_stream_iter(_request(api_key="", payload={"stream": True})))

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
        result = list(post_stream_iter(_request()))
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
            list(post_stream_iter(_request()))

    @patch("urllib.request.urlopen")
    def test_iter_handles_url_error(self, mock_urlopen):
        """验证流式 DNS/网络错误也被包装。"""
        mock_urlopen.side_effect = urllib.error.URLError("[Errno 11001] getaddrinfo failed")

        from agent_py_agent.agent.backends.gateway_helpers import post_stream_iter
        with pytest.raises(RuntimeError, match="网络请求失败.*api.example.com"):
            list(post_stream_iter(_request()))

    @patch("urllib.request.urlopen")
    def test_iter_stops_on_empty_stream(self, mock_urlopen):
        """验证空流式响应正常结束。"""
        mock_response = MagicMock()
        mock_response.__iter__ = MagicMock(return_value=iter([]))
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        from agent_py_agent.agent.backends.gateway_helpers import post_stream_iter
        result = list(post_stream_iter(_request()))
        assert result == []
