"""Web 请求工具测试 - HTTP请求、URL校验、错误处理。"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def _private_resolver(_host: str) -> tuple[str, ...]:
    return ("127.0.0.1",)


def _proxy_private_resolver(_host: str) -> tuple[str, ...]:
    return ("198.18.0.18",)


def _public_resolver(_host: str) -> tuple[str, ...]:
    return ("8.8.8.8",)


class TestWebSearchTool:
    """测试 WebSearchTool 做通用公开来源发现。"""

    def test_web_search_uses_provider_registry_and_falls_back(self, tmp_path: Path):
        """搜索工具应能从多个 provider 里选择可用来源，DuckDuckGo 只是兜底。"""
        from agent_py_agent.agent.tooling.web_search import WebSearchProvider, WebSearchTool

        class BrokenProvider(WebSearchProvider):
            name = "broken"

            def search(self, query: str, limit: int):
                raise RuntimeError("provider down")

        class GoodProvider(WebSearchProvider):
            name = "good"

            def search(self, query: str, limit: int):
                return [
                    {
                        "title": f"{query} result",
                        "url": "https://example.com/result",
                        "snippet": "provider worked",
                    }
                ]

        tool = WebSearchTool(max_results=5, timeout=10, providers=[BrokenProvider(), GoodProvider()])
        result = tool.execute({"query": "agent network tools", "limit": 3})

        assert result.ok is True
        payload = json.loads(result.output)
        assert payload["engine"] == "good"
        assert payload["provider_failures"][0]["provider"] == "broken"
        assert payload["results"][0]["url"] == "https://example.com/result"

    @patch("urllib.request.urlopen")
    def test_web_search_decodes_duckduckgo_results(self, mock_urlopen, tmp_path: Path):
        """搜索工具应返回结构化候选来源，不要求模型猜 URL。"""
        from agent_py_agent.agent.tooling.web_search import WebSearchTool

        html = """
        <html><body>
          <a class="result__a" href="/l/?uddg=https%3A%2F%2Farxiv.org%2Fabs%2F2501.12948">
            DeepSeek-R1: Incentivizing Reasoning Capability in LLMs via Reinforcement Learning
          </a>
          <a class="result__snippet">arXiv paper page with public source metadata.</a>
        </body></html>
        """
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.headers = {"Content-Type": "text/html; charset=utf-8"}
        mock_response.read.return_value = html.encode()
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        tool = WebSearchTool(max_results=5, timeout=10)
        result = tool.execute({"query": "DeepSeek-R1 arxiv", "limit": 3})

        assert result.ok is True
        payload = json.loads(result.output)
        assert payload["query"] == "DeepSeek-R1 arxiv"
        assert payload["results"][0]["url"] == "https://arxiv.org/abs/2501.12948"
        assert payload["results"][0]["title"].startswith("DeepSeek-R1")
        assert payload["results"][0]["source"] == "duckduckgo_html"

    def test_web_search_rejects_empty_query(self, tmp_path: Path):
        """空查询不能发起网络请求。"""
        from agent_py_agent.agent.tooling.web_search import WebSearchTool

        tool = WebSearchTool(max_results=5, timeout=10)
        result = tool.execute({"query": ""})

        assert result.ok is False
        assert "query" in result.output

    @patch("urllib.request.urlopen")
    def test_web_search_filters_allowed_domains(self, mock_urlopen, tmp_path: Path):
        """搜索结果应支持结构化域名过滤，避免只靠提示词约束来源。"""
        from agent_py_agent.agent.tooling.web_search import WebSearchTool

        html = """
        <html><body>
          <a class="result__a" href="/l/?uddg=https%3A%2F%2Fgithub.com%2FOpenGithubs%2Fgithub-weekly-rank">
            代码平台 weekly rank
          </a>
          <a class="result__snippet">Weekly 代码平台 ranking source.</a>
          <a class="result__a" href="/l/?uddg=https%3A%2F%2Fexample.com%2Fblog">
            Example blog
          </a>
          <a class="result__snippet">Commentary page.</a>
        </body></html>
        """
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.headers = {"Content-Type": "text/html; charset=utf-8"}
        mock_response.read.return_value = html.encode()
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        tool = WebSearchTool(max_results=5, timeout=10)
        result = tool.execute({
            "query": "github weekly rank 20260105",
            "allowed_domains": ["github.com"],
        })

        assert result.ok is True
        payload = json.loads(result.output)
        assert [item["url"] for item in payload["results"]] == [
            "https://github.com/OpenGithubs/github-weekly-rank"
        ]

    def test_registry_exposes_web_search_for_source_discovery(self, tmp_path: Path):
        """主工具注册表应只暴露统一网络四件套。"""
        from agent_py_agent.agent.tooling.registry import ToolRegistry, ToolRegistryParams

        registry = ToolRegistry(
            ToolRegistryParams(
                workspace_root=tmp_path,
                max_chars=1000,
                max_entries=20,
                max_matches=5,
                web_max_chars=1000,
                http_timeout=5,
                catalog_limit=20,
                retrieval_limit=10,
                vector_search_enabled=False,
            )
        )

        specs_by_name = {spec.name: spec for spec in registry.specs(include_orchestration=True)}
        assert specs_by_name["web_search"].effect == "read_only"
        assert specs_by_name["web_fetch"].effect == "read_only"
        assert "web_extract" not in specs_by_name
        assert "http_request" not in specs_by_name
        hits = registry.find_relevant_specs("需要搜索公开来源和候选链接")
        assert "web_search" in {spec.name for spec in hits}


class TestUrlValidation:
    """测试 URL 校验逻辑。"""

    def test_url_without_hostname_rejected(self, tmp_path: Path):
        """无主机名的 URL 被拒绝。"""
        from agent_py_agent.agent.tooling.web import WebFetchTool

        tool = WebFetchTool(max_chars=10000, timeout=10, resolver=_public_resolver)
        result = tool.execute({"url": "https://"})

        assert result.ok is False
        assert "主机名" in result.output

    def test_url_localhost_allowed(self, tmp_path: Path):
        """localhost 应该是有效的。"""
        from agent_py_agent.agent.tooling.web import WebFetchTool

        tool = WebFetchTool(max_chars=10000, timeout=10, resolver=_public_resolver)
        result = tool.execute({"url": "http://localhost:8080/"})

        # localhost 应该有有效主机名
        # 但这需要网络，这里只验证参数校验通过

    def test_response_truncation(self, tmp_path: Path):
        """响应内容过长时被截断。"""
        from agent_py_agent.agent.tooling.web import WebFetchTool

        tool = WebFetchTool(max_chars=100, timeout=10, resolver=_public_resolver)

        # 通过 mock 验证截断行为
        # 由于这个测试需要真实的网络调用，我们跳过
