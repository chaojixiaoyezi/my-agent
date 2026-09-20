"""网关助手测试 - gateway_helpers.py HTTP POST、SSE 流式请求。"""

from __future__ import annotations

import http.client
import json
import socket
import threading
import time
import urllib.error
import urllib.request
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


def test_gateway_transport_splits_short_connect_and_long_read_timeouts():
    from agent_py_agent.agent.backends import gateway_helpers

    request = GatewayRequest(
        api_base="https://api.example.com",
        api_key="key",
        path="/v1/chat",
        payload={},
        headers={},
        timeout=600,
        connect_timeout=10,
    )
    opener = MagicMock()
    with patch("urllib.request.build_opener", return_value=opener) as build_opener:
        gateway_helpers._gateway_urlopen(gateway_helpers._urllib_request(request), request)

    proxy_handler, http_handler, https_handler = build_opener.call_args.args
    assert isinstance(proxy_handler, urllib.request.ProxyHandler)
    assert http_handler.transport_options.connect_timeout == 10
    assert https_handler.transport_options.connect_timeout == 10
    assert http_handler.transport_options.read_timeout == 600
    assert https_handler.transport_options.read_timeout == 600
    opener.open.assert_called_once()
    assert opener.open.call_args.kwargs["timeout"] == 600


def test_gateway_transport_keeps_external_provider_proxy_configuration():
    from agent_py_agent.agent.backends import gateway_helpers

    request = GatewayRequest(
        api_base="https://api.example.com",
        api_key="key",
        path="/v1/chat",
        payload={},
        headers={},
        timeout=30,
    )
    opener = MagicMock()
    with (
        patch(
            "urllib.request.getproxies",
            return_value={"https": "http://127.0.0.1:7890"},
        ),
        patch("urllib.request.build_opener", return_value=opener) as build_opener,
    ):
        gateway_helpers._gateway_urlopen(
            gateway_helpers._urllib_request(request),
            request,
        )

    proxy_handler = build_opener.call_args.args[0]
    assert proxy_handler.proxies == {"https": "http://127.0.0.1:7890"}


@pytest.mark.parametrize(
    "host",
    ["127.0.0.1", "127.99.4.3", "[::1]", "[::ffff:127.0.0.1]", "localhost", "model.localhost"],
)
def test_gateway_transport_bypasses_proxy_for_all_explicit_loopback_hosts(host):
    from agent_py_agent.agent.backends import gateway_helpers

    request = GatewayRequest(
        api_base=f"http://{host}:8899",
        api_key="key",
        path="/v1/chat",
        payload={},
        headers={},
        timeout=30,
    )
    opener = MagicMock()
    with (
        patch(
            "urllib.request.getproxies",
            return_value={"http": "http://127.0.0.1:7890"},
        ),
        patch("urllib.request.build_opener", return_value=opener) as build_opener,
    ):
        gateway_helpers._gateway_urlopen(
            gateway_helpers._urllib_request(request),
            request,
        )

    proxy_handler = build_opener.call_args.args[0]
    assert proxy_handler.proxies == {}


def test_http_connection_switches_socket_to_read_timeout_after_connect():
    from agent_py_agent.agent.backends import gateway_helpers

    connection = gateway_helpers._SplitTimeoutHTTPConnection(
        "api.example.com",
        transport_options=gateway_helpers._SplitTimeoutOptions(7, 600, None),
    )
    connection.sock = MagicMock()
    with patch.object(http.client.HTTPConnection, "connect"):
        connection.connect()

    assert connection.timeout == 7
    connection.sock.settimeout.assert_called_once_with(600)


class TestPostJson:
    """post_json 同步 JSON 请求测试。"""

    def test_empty_api_key_raises(self):
        """验证空 api_key 抛出 ValueError。"""
        from agent_py_agent.agent.backends.gateway_helpers import post_json

        with pytest.raises(ValueError, match="api_key 为空"):
            post_json(_request(api_key=""))

    @patch("agent_py_agent.agent.backends.gateway_helpers._gateway_urlopen")
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

    @patch("agent_py_agent.agent.backends.gateway_helpers._gateway_urlopen")
    def test_http_error_handling(self, mock_urlopen):
        """验证 HTTP 错误被包装为 RuntimeError。"""
        from io import BytesIO

        body = BytesIO(b"invalid key")
        mock_urlopen.side_effect = urllib.error.HTTPError(
            "https://api.example.com",
            401,
            "Unauthorized",
            {"Content-Type": "application/json"},
            body,
        )

        from agent_py_agent.agent.backends.errors import ProviderRequestRejectedError
        from agent_py_agent.agent.backends.gateway_helpers import post_json

        with pytest.raises(ProviderRequestRejectedError, match="HTTP 401") as exc_info:
            post_json(_request(api_key="bad-key"))
        assert exc_info.value.status_code == 401
        assert exc_info.value.error_code == "PROVIDER_REQUEST_REJECTED"

    @patch("agent_py_agent.agent.backends.gateway_helpers._provider_retry_wait")
    @patch("agent_py_agent.agent.backends.gateway_helpers._gateway_urlopen")
    def test_unlabeled_400_spends_one_bounded_retry(self, mock_urlopen, mock_sleep):
        """真机证据(2026-09-11 四路复刻)：body 既无 message/type/code 的空洞 400 是供应商瞬时拒绝，
        同一 payload 重放即成功；此前直接终结子代理导致大量白干。现改为有界重试一次。"""
        from io import BytesIO

        silent = urllib.error.HTTPError(
            "https://api.example.com",
            400,
            "Bad Request",
            {"Content-Type": "application/json"},
            BytesIO(b'{"object":"error","model":"deepseek-v4-flash"}'),
        )
        second = MagicMock()
        second.read.return_value = json.dumps({"content": "after retry"}).encode()
        second.__enter__ = MagicMock(return_value=second)
        second.__exit__ = MagicMock(return_value=False)
        mock_urlopen.side_effect = [silent, second]

        from agent_py_agent.agent.backends.gateway_helpers import post_json

        assert post_json(_request()) == {"content": "after retry"}
        assert mock_urlopen.call_count == 2, "空洞 400 只重试一次"
        mock_sleep.assert_not_called()

    @patch("agent_py_agent.agent.backends.gateway_helpers._provider_retry_wait")
    @patch("agent_py_agent.agent.backends.gateway_helpers._gateway_urlopen")
    def test_unlabeled_400_stays_bounded_and_typed(self, mock_urlopen, mock_sleep):
        """连续空洞 400 仍然是有界的：最多多花一次请求，随后抛 typed 拒绝，不掩盖错误。"""
        from io import BytesIO

        def _silent():
            return urllib.error.HTTPError(
                "https://api.example.com",
                400,
                "Bad Request",
                {"Content-Type": "application/json"},
                BytesIO(b'{"object":"error","model":"deepseek-v4-flash"}'),
            )

        mock_urlopen.side_effect = [_silent(), _silent(), _silent()]

        from agent_py_agent.agent.backends.errors import ProviderRequestRejectedError
        from agent_py_agent.agent.backends.gateway_helpers import post_json

        with pytest.raises(ProviderRequestRejectedError) as error:
            post_json(_request())
        assert error.value.status_code == 400
        assert mock_urlopen.call_count == 2
        mock_sleep.assert_not_called()

    @pytest.mark.parametrize("body", [b'', b'not-json', b'[]'])
    @patch("agent_py_agent.agent.backends.gateway_helpers._provider_retry_wait")
    @patch("agent_py_agent.agent.backends.gateway_helpers._gateway_urlopen")
    def test_unexplained_400_retains_request_rejection(self, mock_urlopen, mock_sleep, body):
        """空正文与畸形 JSON 不是"供应商未给原因"，仍保留 HTTP 400 且不重试。"""
        from io import BytesIO

        silent = urllib.error.HTTPError(
            "https://api.example.com",
            400,
            "Bad Request",
            {"Content-Type": "application/json"},
            BytesIO(body),
        )
        mock_urlopen.side_effect = [silent, silent, silent, silent]

        from agent_py_agent.agent.backends.errors import ProviderRequestRejectedError
        from agent_py_agent.agent.backends.gateway_helpers import post_json

        with pytest.raises(ProviderRequestRejectedError) as error:
            post_json(_request())
        assert error.value.status_code == 400
        assert mock_urlopen.call_count == 1
        mock_sleep.assert_not_called()

    @patch("agent_py_agent.agent.backends.gateway_helpers._gateway_urlopen")
    def test_explicit_400_stays_permanent(self, mock_urlopen):
        """带 error/message 说明的 400 是真正的请求错误,保持永久失败不重试。"""
        from io import BytesIO

        explicit = urllib.error.HTTPError(
            "https://api.example.com",
            400,
            "Bad Request",
            {"Content-Type": "application/json"},
            BytesIO(b'{"error":{"message":"model not found"}}'),
        )
        mock_urlopen.side_effect = explicit

        from agent_py_agent.agent.backends.errors import ProviderRequestRejectedError
        from agent_py_agent.agent.backends.gateway_helpers import post_json

        with pytest.raises(ProviderRequestRejectedError, match="HTTP 400"):
            post_json(_request())
        assert mock_urlopen.call_count == 1

    @patch("agent_py_agent.agent.backends.gateway_helpers._gateway_urlopen")
    def test_http_context_window_error_is_typed_provider_error(self, mock_urlopen):
        """验证上下文窗口错误在 provider HTTP 边界结构化，核心层不用猜异常文本。"""
        from agent_py_agent.agent.backends.errors import ProviderContextWindowError
        from agent_py_agent.agent.backends.gateway_helpers import post_json

        body = BytesIO(b'{"error":{"message":"prompt too long for context window"}}')
        mock_urlopen.side_effect = urllib.error.HTTPError(
            "https://api.example.com",
            400,
            "Bad Request",
            {"Content-Type": "application/json"},
            body,
        )

        with pytest.raises(ProviderContextWindowError) as exc_info:
            post_json(_request())
        assert exc_info.value.error_code == "MODEL_CONTEXT_WINDOW_EXCEEDED"

    @patch("agent_py_agent.agent.backends.gateway_helpers._gateway_urlopen")
    def test_litellm_available_context_size_error_is_typed_provider_error(self, mock_urlopen):
        """LiteLLM 的 available context size 措辞也必须进入统一 compact/resume 主链。"""
        from agent_py_agent.agent.backends.errors import ProviderContextWindowError
        from agent_py_agent.agent.backends.gateway_helpers import post_json

        body = BytesIO(
            b'{"error":{"message":"request (135099 tokens) exceeds the available '
            b'context size (125184 tokens), try increasing it"}}'
        )
        mock_urlopen.side_effect = urllib.error.HTTPError(
            "https://api.example.com",
            400,
            "Bad Request",
            {"Content-Type": "application/json"},
            body,
        )

        with pytest.raises(ProviderContextWindowError) as exc_info:
            post_json(_request())
        assert exc_info.value.error_code == "MODEL_CONTEXT_WINDOW_EXCEEDED"
        assert exc_info.value.details["status_code"] == 400

    @patch("agent_py_agent.agent.backends.gateway_helpers._provider_retry_wait")
    @patch("agent_py_agent.agent.backends.gateway_helpers._gateway_urlopen")
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

        from agent_py_agent.agent.backends.gateway_helpers import (
            post_json,
            provider_attempt_observer,
        )

        events: list[dict[str, object]] = []
        with provider_attempt_observer(events.append):
            assert post_json(_request()) == {"content": "after retry"}
        assert mock_urlopen.call_count == 2
        mock_sleep.assert_called_once()
        assert [event["status"] for event in events] == [
            "started",
            "failed",
            "started",
            "response_opened",
        ]
        assert events[1]["http_status"] == 529
        assert events[1]["retry_scheduled"] is True
        assert events[0]["attempt_id"] == events[1]["attempt_id"]
        assert events[2]["attempt_id"] == events[3]["attempt_id"]
        assert events[0]["attempt_id"] != events[2]["attempt_id"]

    @patch("agent_py_agent.agent.backends.gateway_helpers._provider_retry_wait")
    @patch("agent_py_agent.agent.backends.gateway_helpers._gateway_urlopen")
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

    @patch("agent_py_agent.agent.backends.gateway_helpers._provider_retry_wait")
    @patch("agent_py_agent.agent.backends.gateway_helpers._gateway_urlopen")
    def test_connection_refused_errno_retries_with_structured_schedule(
        self,
        mock_urlopen,
        mock_sleep,
    ):
        """系统级 ECONNREFUSED 先按 2/5/15 秒传输退避，并向 observer 公布精确进度。"""
        import errno

        refused = urllib.error.URLError(
            ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused")
        )
        second = MagicMock()
        second.read.return_value = json.dumps({"content": "after reconnect"}).encode()
        second.__enter__ = MagicMock(return_value=second)
        second.__exit__ = MagicMock(return_value=False)
        mock_urlopen.side_effect = [refused, second]

        from agent_py_agent.agent.backends.gateway_helpers import (
            post_json,
            provider_attempt_observer,
        )

        events: list[dict[str, object]] = []
        with provider_attempt_observer(events.append):
            assert post_json(_request()) == {"content": "after reconnect"}

        failed = next(event for event in events if event["status"] == "failed")
        assert failed["retry_scheduled"] is True
        assert failed["retry_attempt"] == 1
        assert failed["retry_total"] == 3
        assert failed["retry_wait_seconds"] == 2.0
        mock_sleep.assert_called_once_with(2.0)

    @patch("agent_py_agent.agent.backends.gateway_helpers._provider_retry_wait")
    @patch("agent_py_agent.agent.backends.gateway_helpers._gateway_urlopen")
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

    @patch("agent_py_agent.agent.backends.gateway_helpers._provider_retry_wait")
    @patch("agent_py_agent.agent.backends.gateway_helpers._gateway_urlopen")
    def test_retryable_network_disconnect_exhaustion_is_transient_error(
        self, mock_urlopen, mock_sleep
    ):
        """验证多次断线后仍归类为 provider 临时错误，方便父级重试/接管而不是当业务失败。"""
        from agent_py_agent.agent.backends.errors import ProviderTransientError
        from agent_py_agent.agent.backends.gateway_helpers import post_json

        mock_urlopen.side_effect = [
            urllib.error.URLError("Remote end closed connection without response") for _ in range(4)
        ]

        with pytest.raises(ProviderTransientError, match="临时断开|连接被重置"):
            post_json(_request())
        assert mock_urlopen.call_count == 4
        assert mock_sleep.call_count == 3

    @patch("agent_py_agent.agent.backends.gateway_helpers._provider_retry_wait")
    @patch("agent_py_agent.agent.backends.gateway_helpers._gateway_urlopen")
    def test_hard_quota_429_fails_fast_without_transport_retry(self, mock_urlopen, mock_wait):
        """Provider 明确说套餐额度耗尽时，同一 key 原地重试不会恢复。"""
        from agent_py_agent.agent.backends.errors import ProviderQuotaExhaustedError
        from agent_py_agent.agent.backends.gateway_helpers import post_json

        body = BytesIO(
            '{"type":"error","error":{"type":"rate_limit_error",'
            '"message":"已达到 Token Plan 用量上限：请升级套餐或购买积分。 (2056)"}}'.encode()
        )
        mock_urlopen.side_effect = urllib.error.HTTPError(
            "https://api.example.com",
            429,
            "Too Many Requests",
            {"Content-Type": "application/json"},
            body,
        )

        with pytest.raises(ProviderQuotaExhaustedError) as exc_info:
            post_json(_request())
        assert exc_info.value.error_code == "PROVIDER_QUOTA_EXHAUSTED"
        assert exc_info.value.details["status_code"] == 429
        assert mock_urlopen.call_count == 1
        mock_wait.assert_not_called()

    @patch("agent_py_agent.agent.backends.gateway_helpers._provider_retry_wait")
    @patch("agent_py_agent.agent.backends.gateway_helpers._gateway_urlopen")
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

    @patch("agent_py_agent.agent.backends.gateway_helpers._provider_retry_wait")
    @patch("agent_py_agent.agent.backends.gateway_helpers._gateway_urlopen")
    def test_timeout_does_not_retry_as_transient_disconnect(self, mock_urlopen, mock_sleep):
        """验证超时仍走 provider_timeout，不和断线重试混在一起。"""
        from agent_py_agent.agent.backends.errors import ProviderTimeoutError
        from agent_py_agent.agent.backends.gateway_helpers import post_json

        mock_urlopen.side_effect = TimeoutError("timed out")

        with pytest.raises(ProviderTimeoutError, match="请求超时"):
            post_json(_request())
        assert mock_urlopen.call_count == 1
        mock_sleep.assert_not_called()

    @patch("agent_py_agent.agent.backends.gateway_helpers._provider_retry_wait")
    @patch("agent_py_agent.agent.backends.gateway_helpers._gateway_urlopen")
    def test_dns_error_retries_before_returning_transient_failure(
        self,
        mock_urlopen,
        mock_wait,
    ):
        """DNS 瞬断像 会话运行时 ConnectionFailed 一样先有界退避，耗尽后才结束当前请求。"""
        import socket

        mock_urlopen.side_effect = urllib.error.URLError(
            socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution")
        )

        from agent_py_agent.agent.backends.errors import ProviderTransientError
        from agent_py_agent.agent.backends.gateway_helpers import post_json

        with pytest.raises(ProviderTransientError, match="网络请求失败.*api.example.com"):
            post_json(_request())
        assert mock_urlopen.call_count == 4
        assert mock_wait.call_count == 3


class TestPostStream:
    """post_stream 流式请求收集测试。"""

    def test_empty_api_key_raises(self):
        """验证空 api_key 抛出 ValueError。"""
        from agent_py_agent.agent.backends.gateway_helpers import post_stream

        with pytest.raises(ValueError, match="api_key 为空"):
            post_stream(_request(api_key="", payload={"stream": True}))

    @patch("agent_py_agent.agent.backends.gateway_helpers._gateway_urlopen")
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

    @patch("agent_py_agent.agent.backends.gateway_helpers._gateway_urlopen")
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

    @patch("agent_py_agent.agent.backends.gateway_helpers._gateway_urlopen")
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
    @patch("agent_py_agent.agent.backends.gateway_helpers._gateway_urlopen")
    def test_stream_heartbeat_comments_do_not_reset_idle_timeout(
        self, mock_urlopen, mock_monotonic
    ):
        """只有真实 SSE data 才算模型进展，注释心跳不能无限续命。"""
        from agent_py_agent.agent.backends.errors import ProviderTimeoutError
        from agent_py_agent.agent.backends.gateway_helpers import post_stream

        mock_monotonic.side_effect = [0, 0, 31]
        mock_response = MagicMock()
        mock_response.__iter__ = MagicMock(return_value=iter([b": ping"]))
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        with pytest.raises(ProviderTimeoutError) as err:
            post_stream(_request())
        assert err.value.stage == "first_event"

    @patch("agent_py_agent.agent.backends.gateway_helpers.time.monotonic")
    @patch("agent_py_agent.agent.backends.gateway_helpers._gateway_urlopen")
    def test_stream_data_resets_idle_timeout_without_total_wall_limit(
        self, mock_urlopen, mock_monotonic
    ):
        # 会话运行时 idle 语义：第 1 行在 20s 到达后把 deadline 延到 50s；
        # 第 2 行在整次请求已过 45s 时仍正常接收，不存在固定 30s 总墙钟。
        mock_monotonic.side_effect = [0, 0, 20, 20, 20, 45, 45, 45]
        mock_response = MagicMock()
        mock_response.__iter__ = MagicMock(
            return_value=iter([b'data: {"content": "a"}', b'data: {"content": "b"}'])
        )
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        from agent_py_agent.agent.backends.gateway_helpers import post_stream

        assert len(post_stream(_request())) == 2


class TestPostStreamIter:
    """post_stream_iter 流式迭代器测试。"""

    def test_empty_api_key_raises(self):
        """验证空 api_key 抛出 ValueError。"""
        from agent_py_agent.agent.backends.gateway_helpers import post_stream_iter

        with pytest.raises(ValueError, match="api_key 为空"):
            list(post_stream_iter(_request(api_key="", payload={"stream": True})))

    @patch("agent_py_agent.agent.backends.gateway_helpers._gateway_urlopen")
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

    @patch("agent_py_agent.agent.backends.gateway_helpers._gateway_urlopen")
    def test_iter_handles_http_error(self, mock_urlopen):
        """验证 provider 5xx HTTP 错误被包装为可读的临时错误，而不是裸 traceback。"""
        from io import BytesIO

        from agent_py_agent.agent.backends.errors import ProviderTransientError

        body = BytesIO(
            b'{"type":"error","error":{"type":"api_error","message":"input new_sensitive"}}'
        )
        mock_urlopen.side_effect = urllib.error.HTTPError(
            "https://api.example.com",
            500,
            "Server Error",
            {"Content-Type": "application/json"},
            body,
        )

        from agent_py_agent.agent.backends.gateway_helpers import post_stream_iter

        with pytest.raises(ProviderTransientError, match="HTTP 500.*input new_sensitive"):
            list(post_stream_iter(_request()))

    @patch("agent_py_agent.agent.backends.gateway_helpers._provider_retry_wait")
    @patch("agent_py_agent.agent.backends.gateway_helpers._gateway_urlopen")
    def test_iter_retries_dns_error_before_transient_failure(self, mock_urlopen, mock_wait):
        """流式入口与普通请求共用 DNS 有界退避，不因一次解析抖动杀死长代理。"""
        import socket

        mock_urlopen.side_effect = urllib.error.URLError(
            socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution")
        )

        from agent_py_agent.agent.backends.errors import ProviderTransientError
        from agent_py_agent.agent.backends.gateway_helpers import post_stream_iter

        with pytest.raises(ProviderTransientError, match="网络请求失败.*api.example.com"):
            list(post_stream_iter(_request()))
        assert mock_urlopen.call_count == 4
        assert mock_wait.call_count == 3

    @patch("agent_py_agent.agent.backends.gateway_helpers._gateway_urlopen")
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


def test_stream_watchdog_aborts_hanging_stream():
    """看门狗硬超时兜底:provider 半行 trickle(发字节但不完成整行)时 readline 永久阻塞,
    idle deadline 检查在逐行循环内永远执行不到 → request_timeout 形同虚设、子代理冻结。
    看门狗在完整空闲区间后强制关 socket，返回 typed idle timeout 而非普通网络错误。"""
    import threading
    import time as _t

    from agent_py_agent.agent.backends.errors import ProviderTimeoutError
    from agent_py_agent.agent.backends.gateway_helpers import post_stream

    closed = threading.Event()

    class HangingResponse:
        def __iter__(self):
            # 模拟半行 trickle 冻结:阻塞直到被 close(看门狗到点)
            if not closed.wait(timeout=8):
                raise AssertionError("看门狗未在超时内关闭卡住的流")
            raise OSError("stream socket closed by watchdog")

        def close(self):
            closed.set()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            self.close()
            return False

    req = GatewayRequest(
        api_base="https://api.example.com",
        api_key="k",
        path="/v1/chat",
        payload={},
        headers={"Content-Type": "application/json"},
        timeout=1,
    )
    with patch(
        "agent_py_agent.agent.backends.gateway_helpers._gateway_urlopen",
        return_value=HangingResponse(),
    ):
        start = _t.monotonic()
        with pytest.raises(ProviderTimeoutError) as err:
            list(post_stream(req))
        assert err.value.stage == "first_event"
        elapsed = _t.monotonic() - start
    assert closed.is_set(), "看门狗应关闭卡住的流"
    assert elapsed < 5, f"看门狗应在 ~timeout(1s) 内解除冻结,实际 {elapsed:.1f}s"


def test_stream_watchdog_shuts_down_stdlib_socket_when_response_close_cannot_cancel_read():
    """A real buffered socket read must end at the idle deadline even when close() cannot wake it."""
    from agent_py_agent.agent.backends.errors import ProviderTimeoutError
    from agent_py_agent.agent.backends.gateway_helpers import post_stream

    client, peer = socket.socketpair()
    peer_release = threading.Timer(3.0, peer.close)
    close_called = threading.Event()

    class BufferedSocketResponse:
        def __init__(self) -> None:
            self.fp = client.makefile("rb")

        def __iter__(self):
            return iter(self.fp)

        def close(self) -> None:
            # Model the CPython cross-thread case seen with urllib: the public
            # close path is entered, but it does not cancel the active read.
            close_called.set()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    request = GatewayRequest(
        api_base="https://api.example.com",
        api_key="k",
        path="/v1/chat",
        payload={},
        headers={"Content-Type": "application/json"},
        timeout=1,
    )
    peer_release.start()
    try:
        started = time.monotonic()
        with patch(
            "agent_py_agent.agent.backends.gateway_helpers._gateway_urlopen",
            return_value=BufferedSocketResponse(),
        ):
            with pytest.raises(ProviderTimeoutError) as err:
                list(post_stream(request))
            assert err.value.stage == "first_event"
        elapsed = time.monotonic() - started
    finally:
        peer_release.cancel()
        peer.close()
        client.close()

    assert close_called.is_set()
    assert elapsed < 2.5, f"socket shutdown 应在 1 秒 idle 门附近解除阻塞，实际 {elapsed:.1f}s"


def test_stream_watchdog_maps_reader_teardown_attribute_error_to_typed_timeout():
    """The reader-side close race is timeout cleanup, not a provider/programmer failure."""
    from agent_py_agent.agent.backends.errors import ProviderTimeoutError
    from agent_py_agent.agent.backends.gateway_helpers import post_stream

    closed = threading.Event()

    class ReaderCloseRaceResponse:
        def __iter__(self):
            if not closed.wait(timeout=5):
                raise AssertionError("看门狗没有关闭卡住的读取器")
            raise AttributeError("'NoneType' object has no attribute 'close'")

        def close(self):
            closed.set()

    request = GatewayRequest(
        api_base="https://api.example.com",
        api_key="k",
        path="/v1/chat",
        payload={},
        headers={"Content-Type": "application/json"},
        timeout=1,
    )

    with patch(
        "agent_py_agent.agent.backends.gateway_helpers._gateway_urlopen",
        return_value=ReaderCloseRaceResponse(),
    ):
        with pytest.raises(ProviderTimeoutError) as err:
            list(post_stream(request))
        assert err.value.stage == "first_event"

    assert closed.is_set()


def test_stream_reader_attribute_error_without_teardown_remains_visible():
    """Do not hide a real implementation error behind the timeout taxonomy."""
    from agent_py_agent.agent.backends.gateway_helpers import post_stream

    class BrokenResponse:
        def __iter__(self):
            raise AttributeError("real parser bug")
            yield b""  # pragma: no cover - preserve iterator shape.

        def close(self):
            return None

    with patch(
        "agent_py_agent.agent.backends.gateway_helpers._gateway_urlopen",
        return_value=BrokenResponse(),
    ):
        with pytest.raises(AttributeError, match="real parser bug"):
            list(post_stream(_request()))


def test_user_interrupt_aborts_hanging_provider_stream_without_waiting_for_timeout():
    from agent_py_agent.agent.backends.gateway_helpers import post_stream
    from agent_py_agent.agent.concurrency.interrupt import interrupt_by_name, register_interruptible

    closed = threading.Event()
    ready = threading.Event()
    outcome: dict[str, object] = {}

    class HangingResponse:
        def __iter__(self):
            ready.set()
            if not closed.wait(timeout=5):
                raise AssertionError("停止信号没有关闭模型流")
            raise OSError("stream socket closed by user interrupt")

        def close(self):
            closed.set()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()
            return False

    req = GatewayRequest(
        api_base="https://api.example.com",
        api_key="k",
        path="/v1/chat",
        payload={},
        headers={"Content-Type": "application/json"},
        timeout=600,
    )

    def worker():
        try:
            with register_interruptible("provider-stream-stop"):
                with patch(
                    "agent_py_agent.agent.backends.gateway_helpers._gateway_urlopen",
                    return_value=HangingResponse(),
                ):
                    post_stream(req)
        except BaseException as exc:
            outcome["error"] = exc

    thread = threading.Thread(target=worker)
    thread.start()
    assert ready.wait(timeout=2)
    assert interrupt_by_name("provider-stream-stop") is True
    thread.join(timeout=2)

    assert closed.is_set()
    assert not thread.is_alive()
    assert isinstance(outcome.get("error"), InterruptedError)


def test_user_interrupt_aborts_response_header_wait_before_http_response_exists():
    from agent_py_agent.agent.backends.gateway_helpers import post_stream
    from agent_py_agent.agent.concurrency.interrupt import interrupt_by_name, register_interruptible

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    server.settimeout(5)
    port = server.getsockname()[1]
    request_received = threading.Event()
    server_done = threading.Event()
    outcome: dict[str, object] = {}

    def hold_response_headers() -> None:
        connection: socket.socket | None = None
        try:
            connection, _address = server.accept()
            connection.settimeout(5)
            received = b""
            while b"\r\n\r\n" not in received:
                chunk = connection.recv(4096)
                if not chunk:
                    return
                received += chunk
            request_received.set()
            while connection.recv(4096):
                pass
        finally:
            if connection is not None:
                connection.close()
            server.close()
            server_done.set()

    def provider_worker() -> None:
        try:
            with register_interruptible("provider-header-stop"):
                post_stream(
                    GatewayRequest(
                        api_base=f"http://127.0.0.1:{port}",
                        api_key="key",
                        path="/v1/chat",
                        payload={},
                        headers={"Content-Type": "application/json"},
                        timeout=600,
                        connect_timeout=5,
                    )
                )
        except BaseException as exc:
            outcome["error"] = exc

    server_thread = threading.Thread(target=hold_response_headers, daemon=True)
    worker_thread = threading.Thread(target=provider_worker)
    server_thread.start()
    worker_thread.start()
    assert request_received.wait(timeout=2)

    started = time.monotonic()
    assert interrupt_by_name("provider-header-stop") is True
    worker_thread.join(timeout=2)
    elapsed = time.monotonic() - started

    assert not worker_thread.is_alive()
    assert server_done.wait(timeout=2)
    assert elapsed < 1.5
    assert isinstance(outcome.get("error"), InterruptedError)


@pytest.mark.parametrize("operation", ["post_json", "get_json", "post_stream"])
@pytest.mark.parametrize("error_type", [http.client.ResponseNotReady, AttributeError])
@pytest.mark.parametrize("cancelled", [True, False])
def test_header_open_error_uses_current_interrupt_without_retry(operation, error_type, cancelled):
    from agent_py_agent.agent.backends import gateway_helpers
    from agent_py_agent.agent.concurrency.interrupt import interrupt_by_name, register_interruptible

    error = error_type("open failed")
    opener = MagicMock()

    # LLM: 替身只制造 open 与中断的确定性先后关系；三种公共请求入口必须共用同一取消合同。
    # 函数用途: 在响应尚未创建时触发停止，再抛连接清理异常，不发送真实网络请求。
    def open_with_error(*_args, **_kwargs):
        if cancelled:
            assert interrupt_by_name("header-error-boundary")
        raise error

    opener.open.side_effect = open_with_error
    expected = InterruptedError if cancelled else error_type
    with register_interruptible("header-error-boundary"):
        with patch("urllib.request.build_opener", return_value=opener):
            with pytest.raises(expected) as caught:
                getattr(gateway_helpers, operation)(_request())
    assert (caught.value.__cause__ if cancelled else caught.value) is error
    opener.open.assert_called_once()


def test_user_interrupt_serializes_response_close_with_reader_cleanup():
    """Stopping a blocked stream must not race urllib's owning-thread close path."""
    from agent_py_agent.agent.backends.gateway_helpers import post_stream
    from agent_py_agent.agent.concurrency.interrupt import interrupt_by_name, register_interruptible

    ready = threading.Event()
    release_read = threading.Event()
    close_started = threading.Event()
    outcome: dict[str, object] = {}

    class CloseRaceResponse:
        def __init__(self) -> None:
            self._closing = False
            self.close_calls = 0

        def __iter__(self):
            ready.set()
            if not release_read.wait(timeout=5):
                raise AssertionError("停止信号没有解除模型流读取")
            # CPython's buffered reader can surface this cleanup race after
            # another thread closes HTTPResponse.fp.  Structured interrupt
            # state, not the incidental exception class, owns the outcome.
            raise AttributeError("'NoneType' object has no attribute 'close'")

        def close(self):
            self.close_calls += 1
            if self._closing:
                raise AttributeError("'NoneType' object has no attribute 'close'")
            self._closing = True
            close_started.set()
            release_read.set()
            # Give the reader thread time to enter its normal cleanup path.
            time.sleep(0.05)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()
            return False

    response = CloseRaceResponse()
    request = GatewayRequest(
        api_base="https://api.example.com",
        api_key="k",
        path="/v1/chat",
        payload={},
        headers={"Content-Type": "application/json"},
        timeout=600,
    )

    def worker():
        try:
            with register_interruptible("provider-close-race"):
                with patch(
                    "agent_py_agent.agent.backends.gateway_helpers._gateway_urlopen",
                    return_value=response,
                ):
                    post_stream(request)
        except BaseException as exc:
            outcome["error"] = exc

    thread = threading.Thread(target=worker)
    thread.start()
    assert ready.wait(timeout=2)
    assert interrupt_by_name("provider-close-race") is True
    assert close_started.wait(timeout=2)
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert response.close_calls == 1
    assert isinstance(outcome.get("error"), InterruptedError)
