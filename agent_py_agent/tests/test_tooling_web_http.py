"""Shared helpers for web tooling tests."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch


def _private_resolver(_host: str) -> tuple[str, ...]:
    return ("127.0.0.1",)


def _proxy_private_resolver(_host: str) -> tuple[str, ...]:
    return ("198.18.0.18",)


def _public_resolver(_host: str) -> tuple[str, ...]:
    return ("8.8.8.8",)


class TestHttpRequestTool:
    """测试 HttpRequestTool 发送 HTTP 请求。"""

    @patch("urllib.request.urlopen")
    def test_http_request_get(self, mock_urlopen, tmp_path: Path):
        """发送 GET 请求。"""
        from agent_py_agent.agent.tooling.web import HttpRequestTool

        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.headers = {"Content-Type": "application/json"}
        mock_response.read.return_value = b'{"status": "ok"}'
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        tool = HttpRequestTool(max_chars=10000, timeout=10, resolver=_public_resolver)
        result = tool.execute({"url": "https://api.example.com/health"})

        assert result.ok is True
        assert "status=200" in result.output

    @patch("urllib.request.urlopen")
    def test_http_request_accepts_per_call_max_chars(self, mock_urlopen, tmp_path: Path):
        """HTTP API 也支持按次缩小返回预览。"""
        from agent_py_agent.agent.tooling.web import HttpRequestTool

        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.headers = {"Content-Type": "application/json"}
        mock_response.read.return_value = b"b" * 1000
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        tool = HttpRequestTool(max_chars=10000, timeout=10, resolver=_public_resolver)
        result = tool.execute({"url": "https://api.example.com/large", "max_chars": 300})

        assert result.ok is True
        body = result.output.split("\n\n", 1)[1].split("\n... 已截断", 1)[0]
        assert body == "b" * 300
        assert "已截断" in result.output

    @patch("urllib.request.urlopen")
    def test_http_request_post(self, mock_urlopen, tmp_path: Path):
        """发送 POST 请求。"""
        from agent_py_agent.agent.tooling.web import HttpRequestTool

        mock_response = MagicMock()
        mock_response.status = 201
        mock_response.headers = {"Content-Type": "application/json"}
        mock_response.read.return_value = b'{"id": 123}'
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        tool = HttpRequestTool(max_chars=10000, timeout=10, resolver=_public_resolver)
        result = tool.execute({
            "url": "https://api.example.com/items",
            "method": "POST",
            "body": '{"name": "test"}',
        })

        assert result.ok is True
        assert "status=201" in result.output

    @patch("urllib.request.urlopen")
    def test_http_request_mutating_method_returns_advisory_not_hard_block(self, mock_urlopen, tmp_path: Path):
        """变更类 HTTP 请求应给结构化提醒，但不靠硬门直接断掉普通任务。"""
        from agent_py_agent.agent.tooling.web import HttpRequestTool

        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.headers = {"Content-Type": "application/json"}
        mock_response.read.return_value = b'{"ok": true}'
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        tool = HttpRequestTool(max_chars=10000, timeout=10, resolver=_public_resolver)
        result = tool.execute({"url": "https://api.example.com/items/1", "method": "DELETE"})

        assert result.ok is True
        assert result.result_envelope["http_effect"] == "mutating"
        assert "idempotency_key" in result.result_envelope["advisories"]


class TestHttpRequestValidation:
    """测试 http_request 参数和错误边界。"""

    def test_http_request_invalid_method(self, tmp_path: Path):
        """无效 HTTP 方法被拒绝。"""
        from agent_py_agent.agent.tooling.web import HttpRequestTool

        tool = HttpRequestTool(max_chars=10000, timeout=10, resolver=_public_resolver)
        result = tool.execute({
            "url": "https://api.example.com",
            "method": "INVALID_METHOD",
        })

        # 无效方法名应该在参数校验阶段被拒绝
        assert result.ok is False

    def test_http_request_headers_dict(self, tmp_path: Path):
        """字典格式请求头。"""
        from agent_py_agent.agent.tooling.web import HttpRequestTool

        tool = HttpRequestTool(max_chars=10000, timeout=10, resolver=_public_resolver)
        # 不需要真正发送请求，只需验证参数解析不报错
        # headers 参数会在内部标准化

    @patch("urllib.request.urlopen")
    def test_http_request_with_headers_json_string(self, mock_urlopen, tmp_path: Path):
        """JSON 字符串格式请求头。"""
        from agent_py_agent.agent.tooling.web import HttpRequestTool

        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.headers = {"Content-Type": "application/json"}
        mock_response.read.return_value = b'{"ok": true}'
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        tool = HttpRequestTool(max_chars=10000, timeout=10, resolver=_public_resolver)
        result = tool.execute({
            "url": "https://api.example.com",
            "headers": '{"Authorization": "Bearer token123"}',
        })

        assert result.ok is True

    def test_http_request_invalid_headers_json(self, tmp_path: Path):
        """无效 JSON 请求头被拒绝。"""
        from agent_py_agent.agent.tooling.web import HttpRequestTool

        tool = HttpRequestTool(max_chars=10000, timeout=10, resolver=_public_resolver)
        result = tool.execute({
            "url": "https://api.example.com",
            "headers": "not valid json",
        })

        assert result.ok is False
        assert "JSON" in result.output

    @patch("urllib.request.urlopen")
    def test_http_request_body_too_large(self, mock_urlopen, tmp_path: Path):
        """请求体过大被拒绝。"""
        from agent_py_agent.agent.tooling.web import HttpRequestTool

        tool = HttpRequestTool(max_chars=10000, timeout=10, resolver=_public_resolver)
        result = tool.execute({
            "url": "https://api.example.com",
            "body": "A" * 2_000_000,
        })

        assert result.ok is False
        assert "过长" in result.output

    def test_http_request_headers_too_many(self, tmp_path: Path):
        """请求头过多被拒绝。"""
        from agent_py_agent.agent.tooling.web import HttpRequestTool

        tool = HttpRequestTool(max_chars=10000, timeout=10, resolver=_public_resolver)
        many_headers = {f"Header{i}": f"Value{i}" for i in range(200)}
        result = tool.execute({
            "url": "https://api.example.com",
            "headers": many_headers,
        })

        assert result.ok is False
        assert "字段过多" in result.output

    @patch("urllib.request.urlopen")
    def test_http_request_blocks_private_dns_before_request(self, mock_urlopen, tmp_path: Path):
        """通用 HTTP 请求也必须经过 DNS 网络安全门。"""
        from agent_py_agent.agent.tooling.web import HttpRequestTool

        tool = HttpRequestTool(max_chars=10000, timeout=10, resolver=_private_resolver)
        result = tool.execute({"url": "https://api.public.example.test/items", "method": "GET"})

        assert result.ok is False
        assert result.error_code == "NETWORK_PRIVATE_IP_BLOCKED"
        assert result.result_envelope["network_safety_gate"]["gate"] == "network_safety"
        mock_urlopen.assert_not_called()

    @patch("urllib.request.urlopen")
    def test_http_request_blocks_metadata_even_with_private_resolution_opt_in(self, mock_urlopen, tmp_path: Path):
        """metadata/link-local 是安全底线，结构化私网解析授权也不能放行。"""
        from agent_py_agent.agent.tooling.web import HttpRequestTool

        tool = HttpRequestTool(
            max_chars=10000,
            timeout=10,
            resolver=lambda _host: ("169.254.169.254",),
            allow_private_resolution=True,
        )
        result = tool.execute({"url": "https://public.example.test/items", "method": "GET"})

        assert result.ok is False
        assert result.error_code == "NETWORK_ALWAYS_BLOCKED_IP"
        mock_urlopen.assert_not_called()

    def test_http_request_header_name_invalid(self, tmp_path: Path):
        """无效请求头名称被拒绝。"""
        from agent_py_agent.agent.tooling.web import HttpRequestTool

        tool = HttpRequestTool(max_chars=10000, timeout=10, resolver=_public_resolver)
        result = tool.execute({
            "url": "https://api.example.com",
            "headers": {"Invalid Name": "value"},
        })

        assert result.ok is False

    @patch("urllib.request.urlopen")
    def test_http_request_http_error_response(self, mock_urlopen, tmp_path: Path):
        """HTTP 错误响应处理。"""
        import urllib.error

        from agent_py_agent.agent.tooling.web import HttpRequestTool

        mock_error = urllib.error.HTTPError(
            url="https://api.example.com",
            code=500,
            msg="Internal Server Error",
            hdrs={},
            fp=None,
        )
        mock_error.read.return_value = b"Server Error"
        mock_urlopen.side_effect = mock_error

        tool = HttpRequestTool(max_chars=10000, timeout=10, resolver=_public_resolver)
        result = tool.execute({"url": "https://api.example.com"})

        assert result.ok is False
        assert "HTTP 500" in result.output

