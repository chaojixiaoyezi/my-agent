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


class TestFetchUrlTool:
    """测试 FetchUrlTool 抓取网页内容。"""

    @patch("urllib.request.urlopen")
    def test_web_fetch_markdown_mode_and_cache(self, mock_urlopen, tmp_path: Path):
        """web_fetch 应能把 HTML 转成可读 Markdown，并缓存同一 URL 的结果。"""
        from agent_py_agent.agent.tooling.web import WebFetchTool

        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.headers = {"Content-Type": "text/html; charset=utf-8"}
        mock_response.read.return_value = b"<html><body><h1>Title</h1><a href='/docs'>Docs</a><p>Hello</p></body></html>"
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        tool = WebFetchTool(max_chars=10000, timeout=10, resolver=_public_resolver, cache_ttl_seconds=900)
        first = tool.execute({"url": "https://example.com/page", "format": "markdown"})
        second = tool.execute({"url": "https://example.com/page", "format": "markdown"})

        assert first.ok is True
        assert second.ok is True
        assert "# Title" in first.output
        assert "[Docs](/docs)" in first.output
        assert second.result_envelope["cache"]["hit"] is True
        mock_urlopen.assert_called_once()

    @patch("urllib.request.urlopen")
    def test_web_fetch_binary_response_is_saved_as_artifact(self, mock_urlopen, tmp_path: Path):
        """二进制网页结果不应灌进上下文，应保存 artifact 并返回引用。"""
        from agent_py_agent.agent.tooling.web import WebFetchTool

        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.headers = {"Content-Type": "application/pdf"}
        mock_response.read.return_value = b"%PDF-1.7 fake pdf bytes"
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        tool = WebFetchTool(max_chars=10000, timeout=10, resolver=_public_resolver, artifact_root=tmp_path)
        result = tool.execute({"url": "https://example.com/file.pdf"})

        assert result.ok is True
        artifact_path = Path(result.result_envelope["artifact"]["path"])
        assert artifact_path.exists()
        assert artifact_path.read_bytes().startswith(b"%PDF")
        assert "artifact_ref=" in result.output

    @patch("urllib.request.urlopen")
    def test_web_fetch_blocks_private_dns_before_request(self, mock_urlopen, tmp_path: Path):
        """web_fetch 也必须走统一网络安全边界，不能只保护旧 fetch_url。"""
        from agent_py_agent.agent.tooling.web import WebFetchTool

        tool = WebFetchTool(max_chars=10000, timeout=10, resolver=_private_resolver, artifact_root=tmp_path)
        result = tool.execute({"url": "https://public.example.test/report"})

        assert result.ok is False
        assert result.error_code == "NETWORK_PRIVATE_IP_BLOCKED"
        mock_urlopen.assert_not_called()

    @patch("urllib.request.urlopen")
    def test_web_extract_multiple_urls_saves_full_content_refs(self, mock_urlopen, tmp_path: Path):
        """web_extract 应能批量抽取 URL，长正文保存为可恢复 artifact。"""
        from agent_py_agent.agent.tooling.web import WebExtractTool

        responses = []
        for title in ("One", "Two"):
            mock_response = MagicMock()
            mock_response.status = 200
            mock_response.headers = {"Content-Type": "text/html; charset=utf-8"}
            mock_response.read.return_value = f"<html><body><h1>{title}</h1><p>{'正文' * 200}</p></body></html>".encode()
            mock_response.__enter__ = MagicMock(return_value=mock_response)
            mock_response.__exit__ = MagicMock(return_value=False)
            responses.append(mock_response)
        mock_urlopen.side_effect = responses

        tool = WebExtractTool(max_chars=120, timeout=10, resolver=_public_resolver, artifact_root=tmp_path)
        result = tool.execute({"urls": ["https://example.com/one", "https://example.com/two"]})

        assert result.ok is True
        payload = json.loads(result.output)
        assert len(payload["pages"]) == 2
        assert all(Path(page["artifact_ref"]).exists() for page in payload["pages"])
        assert payload["pages"][0]["title"] == "One"

    @patch("urllib.request.urlopen")
    def test_web_extract_reports_blocked_urls_without_hiding_successful_pages(self, mock_urlopen, tmp_path: Path):
        """批量抽取时单个 URL 被安全边界拒绝，不应吞掉其他成功页面。"""
        from agent_py_agent.agent.tooling.web import WebExtractTool

        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.headers = {"Content-Type": "text/plain"}
        mock_response.read.return_value = b"public page"
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        def resolver(host: str) -> tuple[str, ...]:
            return ("127.0.0.1",) if host.startswith("blocked") else ("8.8.8.8",)

        tool = WebExtractTool(max_chars=120, timeout=10, resolver=resolver, artifact_root=tmp_path)
        result = tool.execute({"urls": ["https://blocked.example.test", "https://ok.example.test"]})

        assert result.ok is True
        payload = json.loads(result.output)
        assert len(payload["pages"]) == 1
        assert payload["failures"][0]["error_code"] == "NETWORK_PRIVATE_IP_BLOCKED"


class TestFetchUrlCompatibilityTool:
    """测试旧 fetch_url 兼容入口。"""

    @patch("urllib.request.urlopen")
    def test_fetch_url_basic(self, mock_urlopen, tmp_path: Path):
        """基本 URL 抓取功能。"""
        from agent_py_agent.agent.tooling.web import FetchUrlTool

        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.headers = {"Content-Type": "text/html"}
        mock_response.read.return_value = b"<html>Hello</html>"
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        tool = FetchUrlTool(max_chars=10000, timeout=10, resolver=_public_resolver)
        result = tool.execute({"url": "https://example.com"})

        assert result.ok is True
        assert "status=200" in result.output

    @patch("urllib.request.urlopen")
    def test_fetch_url_accepts_per_call_max_chars(self, mock_urlopen, tmp_path: Path):
        """批量研究时可按次缩小网页预览，避免大页面反复撑大上下文。"""
        from agent_py_agent.agent.tooling.web import FetchUrlTool

        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.headers = {"Content-Type": "text/plain"}
        mock_response.read.return_value = b"a" * 1000
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        tool = FetchUrlTool(max_chars=10000, timeout=10, resolver=_public_resolver)
        result = tool.execute({"url": "https://example.com", "max_chars": 300})

        assert result.ok is True
        body = result.output.split("\n\n", 1)[1].split("\n... 已截断", 1)[0]
        assert body == "a" * 300
        assert "已截断" in result.output

    @patch("urllib.request.urlopen")
    def test_fetch_url_html_preview_prioritizes_visible_body_text(self, mock_urlopen, tmp_path: Path):
        """长 HTML 的 head/script 不应挤掉 body 里的可读正文。"""
        from agent_py_agent.agent.tooling.web import FetchUrlTool

        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.headers = {"Content-Type": "text/html; charset=utf-8"}
        noisy_head = "<script>" + ("var ignored = 1;" * 300) + "</script>"
        visible_body = "<body><main><a href='/owner/project'>owner/project</a><p>1,234 stars this week</p></main></body>"
        mock_response.read.return_value = f"<html><head>{noisy_head}</head>{visible_body}</html>".encode()
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        tool = FetchUrlTool(max_chars=400, timeout=10, resolver=_public_resolver)
        result = tool.execute({"url": "https://example.com/trending"})

        assert result.ok is True
        assert "visible_text_preview:" in result.output
        assert "owner/project" in result.output
        assert "1,234 stars this week" in result.output

    @patch("urllib.request.urlopen")
    def test_fetch_url_with_http_error(self, mock_urlopen, tmp_path: Path):
        """HTTP 错误响应处理。"""
        import urllib.error

        from agent_py_agent.agent.tooling.web import FetchUrlTool

        mock_error = urllib.error.HTTPError(
            url="https://example.com",
            code=404,
            msg="Not Found",
            hdrs={},
            fp=None,
        )
        mock_error.read.return_value = b"Not Found"
        mock_urlopen.side_effect = mock_error

        tool = FetchUrlTool(max_chars=10000, timeout=10, resolver=_public_resolver)
        result = tool.execute({"url": "https://example.com"})

        assert result.ok is False
        assert "HTTP 404" in result.output

    @patch("urllib.request.urlopen")
    def test_fetch_url_timeout(self, mock_urlopen, tmp_path: Path):
        """请求超时处理。"""
        import urllib.error

        from agent_py_agent.agent.tooling.web import FetchUrlTool

        mock_urlopen.side_effect = urllib.error.URLError("Timeout")

        tool = FetchUrlTool(max_chars=10000, timeout=10, resolver=_public_resolver)
        result = tool.execute({"url": "https://example.com"})

        assert result.ok is False
        assert "请求失败" in result.output

    def test_fetch_url_invalid_url_scheme(self, tmp_path: Path):
        """非 HTTP/HTTPS URL 被拒绝。"""
        from agent_py_agent.agent.tooling.web import FetchUrlTool

        tool = FetchUrlTool(max_chars=10000, timeout=10, resolver=_public_resolver)
        result = tool.execute({"url": "ftp://example.com/file"})

        assert result.ok is False
        assert "http 或 https" in result.output

    def test_fetch_url_missing_url(self, tmp_path: Path):
        """缺少 URL 参数。"""
        from agent_py_agent.agent.tooling.web import FetchUrlTool

        tool = FetchUrlTool(max_chars=10000, timeout=10, resolver=_public_resolver)
        result = tool.execute({})

        assert result.ok is False

    def test_fetch_url_empty_url(self, tmp_path: Path):
        """空 URL 被拒绝。"""
        from agent_py_agent.agent.tooling.web import FetchUrlTool

        tool = FetchUrlTool(max_chars=10000, timeout=10, resolver=_public_resolver)
        result = tool.execute({"url": ""})

        assert result.ok is False

    def test_fetch_url_url_with_control_chars(self, tmp_path: Path):
        """包含控制字符的 URL 被拒绝。"""
        from agent_py_agent.agent.tooling.web import FetchUrlTool

        tool = FetchUrlTool(max_chars=10000, timeout=10, resolver=_public_resolver)
        result = tool.execute({"url": "https://example.com\n/foo"})

        assert result.ok is False
        assert "控制字符" in result.output

    def test_fetch_url_url_with_credentials_blocked(self, tmp_path: Path):
        """带用户信息的 URL 被拒绝。"""
        from agent_py_agent.agent.tooling.web import FetchUrlTool

        tool = FetchUrlTool(max_chars=10000, timeout=10, resolver=_public_resolver)
        result = tool.execute({"url": "https://user:pass@example.com"})

        assert result.ok is False
        assert "用户名或密码" in result.output

    def test_fetch_url_url_too_long(self, tmp_path: Path):
        """超长 URL 被拒绝。"""
        from agent_py_agent.agent.tooling.web import FetchUrlTool

        tool = FetchUrlTool(max_chars=10000, timeout=10, resolver=_public_resolver)
        long_url = "https://example.com/" + "a" * 5000
        result = tool.execute({"url": long_url})

        assert result.ok is False
        assert "过长" in result.output

    @patch("urllib.request.urlopen")
    def test_fetch_url_blocks_private_dns_before_request(self, mock_urlopen, tmp_path: Path):
        """DNS 解析到私网地址时，网络入口门必须先拦截，不能发起请求。"""
        from agent_py_agent.agent.tooling.web import FetchUrlTool

        tool = FetchUrlTool(max_chars=10000, timeout=10, resolver=_private_resolver)
        result = tool.execute({"url": "https://public.example.test/report"})

        assert result.ok is False
        assert result.error_code == "NETWORK_PRIVATE_IP_BLOCKED"
        assert result.result_envelope["network_safety_gate"]["gate"] == "network_safety"
        mock_urlopen.assert_not_called()

    @patch("urllib.request.urlopen")
    def test_fetch_url_structured_private_resolution_opt_in(self, mock_urlopen, tmp_path: Path):
        """代理/VPN 环境只能通过结构化开关放行私网解析，不读 prompt 文案。"""
        from agent_py_agent.agent.tooling.web import FetchUrlTool

        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.headers = {"Content-Type": "text/plain"}
        mock_response.read.return_value = b"ok"
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        tool = FetchUrlTool(
            max_chars=10000,
            timeout=10,
            resolver=_proxy_private_resolver,
            allow_private_resolution=True,
        )
        result = tool.execute({"url": "https://public.example.test/report"})

        assert result.ok is True
        assert "ok" in result.output
        mock_urlopen.assert_called_once()

