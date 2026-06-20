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


class TestWebFetchTool:
    """测试 WebFetchTool 抓取网页内容。"""

    @patch("agent_py_agent.agent.tooling.web_fetch_runtime._send_pinned")
    def test_web_fetch_markdown_mode_and_cache(self, mock_urlopen, tmp_path: Path):
        """web_fetch 应能把 HTML 转成可读 Markdown，并缓存同一 URL 的结果。"""
        from agent_py_agent.agent.tooling.web import WebFetchTool

        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.headers = {"Content-Type": "text/html; charset=utf-8"}
        mock_response.read.return_value = b"<html><body><h1>Title</h1><a href='/docs'>Docs</a><p>Hello</p></body></html>"
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = (MagicMock(), mock_response)

        tool = WebFetchTool(max_chars=10000, timeout=10, resolver=_public_resolver, cache_ttl_seconds=900)
        first = tool.execute({"url": "https://example.com/page", "format": "markdown"})
        second = tool.execute({"url": "https://example.com/page", "format": "markdown"})

        assert first.ok is True
        assert second.ok is True
        assert "# Title" in first.output
        assert "[Docs](/docs)" in first.output
        assert second.result_envelope["cache"]["hit"] is True
        mock_urlopen.assert_called_once()

    @patch("agent_py_agent.agent.tooling.web_fetch_runtime._send_pinned")
    def test_web_fetch_binary_response_is_saved_as_artifact(self, mock_urlopen, tmp_path: Path):
        """二进制网页结果不应灌进上下文，应保存 artifact 并返回引用。"""
        from agent_py_agent.agent.tooling.web import WebFetchTool

        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.headers = {"Content-Type": "application/pdf"}
        mock_response.read.return_value = b"%PDF-1.7 fake pdf bytes"
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = (MagicMock(), mock_response)

        tool = WebFetchTool(max_chars=10000, timeout=10, resolver=_public_resolver, artifact_root=tmp_path)
        result = tool.execute({"url": "https://example.com/file.pdf"})

        assert result.ok is True
        artifact_path = Path(result.result_envelope["artifact"]["path"])
        assert artifact_path.exists()
        assert artifact_path.read_bytes().startswith(b"%PDF")
        assert "artifact_ref=" in result.output

    @patch("agent_py_agent.agent.tooling.web_fetch_runtime._send_pinned")
    def test_web_fetch_blocks_private_dns_before_request(self, mock_urlopen, tmp_path: Path):
        """web_fetch 也必须走统一网络安全边界，不能只保护旧 web_fetch。"""
        from agent_py_agent.agent.tooling.web import WebFetchTool

        tool = WebFetchTool(max_chars=10000, timeout=10, resolver=_private_resolver, artifact_root=tmp_path)
        result = tool.execute({"url": "https://public.example.test/report"})

        assert result.ok is False
        assert result.error_code == "NETWORK_PRIVATE_IP_BLOCKED"
        mock_urlopen.assert_not_called()

    @patch("agent_py_agent.agent.tooling.web_fetch_runtime._send_pinned")
    def test_web_fetch_extract_mode_multiple_urls_saves_full_content_refs(self, mock_urlopen, tmp_path: Path):
        """web_fetch 的 extract 模式应能批量读取 URL，长正文保存为可恢复 artifact。"""
        from agent_py_agent.agent.tooling.web import WebFetchTool

        responses = []
        for title in ("One", "Two"):
            mock_response = MagicMock()
            mock_response.status = 200
            mock_response.headers = {"Content-Type": "text/html; charset=utf-8"}
            mock_response.read.return_value = f"<html><body><h1>{title}</h1><p>{'正文' * 200}</p></body></html>".encode()
            mock_response.__enter__ = MagicMock(return_value=mock_response)
            mock_response.__exit__ = MagicMock(return_value=False)
            responses.append(mock_response)
        mock_urlopen.side_effect = [(MagicMock(), _r) for _r in responses]

        tool = WebFetchTool(max_chars=120, timeout=10, resolver=_public_resolver, artifact_root=tmp_path)
        result = tool.execute({"urls": ["https://example.com/one", "https://example.com/two"], "mode": "extract"})

        assert result.ok is True
        payload = json.loads(result.output)
        assert len(payload["pages"]) == 2
        assert all(Path(page["artifact_ref"]).exists() for page in payload["pages"])
        assert payload["pages"][0]["title"] == "One"

    @patch("agent_py_agent.agent.tooling.web_fetch_runtime._send_pinned")
    def test_web_fetch_extract_mode_reports_blocked_urls_without_hiding_successful_pages(self, mock_urlopen, tmp_path: Path):
        """批量抽取时单个 URL 被安全边界拒绝，不应吞掉其他成功页面。"""
        from agent_py_agent.agent.tooling.web import WebFetchTool

        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.headers = {"Content-Type": "text/plain"}
        mock_response.read.return_value = b"public page"
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = (MagicMock(), mock_response)

        def resolver(host: str) -> tuple[str, ...]:
            return ("127.0.0.1",) if host.startswith("blocked") else ("8.8.8.8",)

        tool = WebFetchTool(max_chars=120, timeout=10, resolver=resolver, artifact_root=tmp_path)
        result = tool.execute({"urls": ["https://blocked.example.test", "https://ok.example.test"], "mode": "extract"})

        assert result.ok is True
        payload = json.loads(result.output)
        assert len(payload["pages"]) == 1
        assert payload["failures"][0]["error_code"] == "NETWORK_PRIVATE_IP_BLOCKED"

    @patch("agent_py_agent.agent.tooling.web_fetch_runtime._send_pinned")
    def test_web_fetch_accepts_api_post_without_separate_http_tool(self, mock_urlopen, tmp_path: Path):
        """web_fetch 应覆盖原独立 HTTP 工具 的请求体、请求头和变更提醒能力。"""
        from agent_py_agent.agent.tooling.web import WebFetchTool

        mock_response = MagicMock()
        mock_response.status = 201
        mock_response.headers = {"Content-Type": "application/json"}
        mock_response.read.return_value = b'{"id": 123}'
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = (MagicMock(), mock_response)

        tool = WebFetchTool(max_chars=10000, timeout=10, resolver=_public_resolver, artifact_root=tmp_path)
        result = tool.execute({
            "url": "https://api.example.com/items",
            "method": "POST",
            "headers": {"Content-Type": "application/json"},
            "body": '{"name": "test"}',
            "mode": "json",
        })

        assert result.ok is True
        assert "status=201" in result.output
        assert result.result_envelope["http_effect"] == "mutating"
        assert "idempotency_key" in result.result_envelope["advisories"]

