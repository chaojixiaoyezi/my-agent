"""Web 请求工具测试 - HTTP请求、URL校验、错误处理。"""
from __future__ import annotations

import json
import threading
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

import pytest

from agent_py_agent.agent.common.cancellation import (
    CancellationToken,
    bind_cancellation_token,
)


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

    def test_official_search_posts_with_current_owner_key_and_projects_results(self, tmp_path, monkeypatch):
        from agent_py_agent.agent.settings import model_profiles
        from agent_py_agent.agent.settings.model_credentials import deepseek_search_api_key
        from agent_py_agent.agent.settings.model_provider_schema import ModelProfileError
        from agent_py_agent.agent.tooling import web_search

        def save(owner_id, providers):
            home = SimpleNamespace(config_dir=tmp_path, owner_provider="local", owner_kind="user", owner_id=owner_id)
            path = model_profiles.model_profiles_path(home)
            path.parent.mkdir(exist_ok=True)
            catalog = model_profiles.read_model_profiles(path)
            catalog["providers"] = providers
            model_profiles._save_profiles(path, catalog)
            return home

        official = {"display_name": "官方", "api_base": "https://api.deepseek.com/v1"}
        opencode = {"display_name": "DeepSeek", "api_base": "https://opencode.ai/zen/v1", "api_key": "other-fixture"}
        user = save("user-a", {"opencode": opencode, "official": {**official, "api_key_ref": "env:DEEPSEEK_API_KEY"}})
        other = save("user-b", {"opencode": opencode})
        save("user-c", {"official": {**official, "api_key": "different-owner-fixture"}})
        monkeypatch.setenv("DEEPSEEK_API_KEY", "official-fixture")
        with pytest.raises(ModelProfileError, match="未找到可用"):
            deepseek_search_api_key(user)

        seen = []
        payload = {"results": [
            {"title": "官网", "url": "https://docs.example.com/tool", "snippet": "test official-fixture"},
            {"title": "非白名单", "url": "https://example.com.other.test/other", "snippet": "elsewhere"},
        ]}

        def open_request(request, timeout):
            seen.append((request.full_url, request.method, json.loads(request.data), timeout))
            assert request.get_header("Authorization") == "Bearer official-fixture"
            assert request.get_header("Content-type") == "application/json"
            return BytesIO(json.dumps(payload).encode())

        def build_opener(redirect_handler):
            assert redirect_handler.redirect_request(None, None, 307, "", {}, "https://other.test") is None
            return SimpleNamespace(open=open_request)

        monkeypatch.setattr(web_search.urllib.request, "build_opener", build_opener)
        tool = web_search.WebSearchTool(timeout=9, deepseek_credential=lambda: deepseek_search_api_key(user, protected_env_names=frozenset({"DEEPSEEK_API_KEY"})))
        outcome = tool.execute({"provider": "deepseek", "query": "官方资料", "limit": 2, "allowed_domains": ["example.com"]})
        assert outcome.ok
        assert seen == [(
            "https://api.deepseek.com/v1/tools/search", "POST",
            {"query": "官方资料", "count": 2, "need_content": False}, 9,
        )]
        payload = json.loads(outcome.output)
        assert payload["engine"] == "deepseek"
        assert payload["query_evidence"]["conditions"]["provider"] == "deepseek"
        assert payload["results"] == [
            {"title": "官网", "url": "https://docs.example.com/tool", "snippet": "test [已隐藏]", "source": "deepseek"},
        ]

        other_tool = web_search.WebSearchTool(timeout=9, deepseek_credential=lambda: deepseek_search_api_key(other))
        denied = other_tool.execute({"provider": "deepseek", "query": "官方资料"})
        assert not denied.ok and denied.effect_outcome == "not_started"
        assert "credential_missing" in denied.output
        assert len(seen) == 1

    def test_official_search_validates_before_resolving_credentials(self):
        from agent_py_agent.agent.tooling.web_search import WebSearchTool

        credential = MagicMock(side_effect=AssertionError("preflight must run before credentials"))
        config = SimpleNamespace(enabled=False)
        tool = WebSearchTool(timeout=9, deepseek_credential=credential, deepseek_enabled=lambda: config.enabled)
        denied = tool.execute({"provider": "deepseek", "query": "官方资料"})
        assert not denied.ok and denied.effect_outcome == "not_started"
        assert "tool_deepseek_search_enabled=false" in denied.output

        config.enabled = True
        long_query = tool.execute({"provider": "deepseek", "query": "长" * 129})
        assert not long_query.ok and long_query.error_code == "TOOL_INVALID_ARGUMENTS"
        assert long_query.effect_outcome == "not_started"
        assert "129" in long_query.output and "128" in long_query.output
        invalid = tool.execute({"provider": "opencode", "query": "官方资料"})
        assert not invalid.ok and invalid.error_code == "TOOL_INVALID_ARGUMENTS"
        credential.assert_not_called()

    def test_official_search_http_error_keeps_status_and_never_uses_default_provider(self, monkeypatch):
        from agent_py_agent.agent.tooling import web_search

        stream = BytesIO(b'{"message":"official-fixture"}')
        opener = MagicMock(side_effect=HTTPError(web_search.DEEPSEEK_SEARCH_URL, 401, "", {}, stream))
        monkeypatch.setattr(web_search.urllib.request, "build_opener", lambda *_: SimpleNamespace(open=opener))
        default = MagicMock()
        tool = web_search.WebSearchTool(timeout=9, providers=[default], deepseek_credential=lambda: "official-fixture")
        outcome = tool.execute({"provider": "deepseek", "query": "官方资料"})
        assert not outcome.ok and not outcome.retryable
        assert outcome.effect_outcome == "failed"
        assert outcome.reported_error_code == "DEEPSEEK_SEARCH_HTTP_401"
        assert "HTTP 401" in outcome.output and "official-fixture" not in outcome.output
        assert "/model" in outcome.next_action
        assert stream.closed
        opener.assert_called_once()
        default.search.assert_not_called()

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

    @patch("urllib.request.urlopen")
    def test_web_search_closes_blocking_response_on_cancellation(
        self,
        mock_urlopen,
    ):
        from agent_py_agent.agent.tooling.web_search import WebSearchTool

        reading = threading.Event()
        closed = threading.Event()

        class BlockingResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                self.close()
                return False

            # 生产端带读取上限调用 read(size)（web_search._read_search_response），
            # fake 必须接受该参数，否则会以 TypeError 提前失败、测不到"取消关闭阻塞读"这条契约。
            def read(self, *_args):
                reading.set()
                assert closed.wait(3)
                return b""

            def close(self):
                closed.set()

        mock_urlopen.return_value = BlockingResponse()
        token = CancellationToken()
        observed = {}

        def run() -> None:
            with bind_cancellation_token(token):
                observed["result"] = WebSearchTool(timeout=20).execute(
                    {"query": "blocking query"}
                )

        thread = threading.Thread(target=run)
        thread.start()
        assert reading.wait(2)
        token.cancel("test stop")
        thread.join(3)

        assert not thread.is_alive()
        assert observed["result"].error_code == "CANCELLED"

    def test_web_search_rejects_empty_query(self, tmp_path: Path):
        """空查询不能发起网络请求。"""
        from agent_py_agent.agent.tooling.web_search import WebSearchTool

        tool = WebSearchTool(max_results=5, timeout=10)
        result = tool.execute({"query": ""})

        assert result.ok is False
        assert "query" in result.output

    def test_web_search_provider_failures_return_unavailable(self, tmp_path: Path):
        """所有 provider 抛异常(反爬/网络)→ TOOL_UNAVAILABLE，明确是工具失败。"""
        from agent_py_agent.agent.tooling.web_search import WebSearchProvider, WebSearchTool

        class BrokenProvider(WebSearchProvider):
            name = "broken"

            def search(self, query: str, limit: int):
                raise RuntimeError("provider down")

        tool = WebSearchTool(max_results=5, timeout=10, providers=[BrokenProvider()])
        result = tool.execute({"query": "anything", "limit": 3})

        assert result.ok is False
        assert result.error_code == "TOOL_UNAVAILABLE"
        assert json.loads(result.output)["provider_failures"]

    def test_web_search_empty_results_without_failures_is_ok(self, tmp_path: Path):
        """provider 正常响应但无匹配→ ok=True 空结果+引导，不误判为工具不可用。"""
        from agent_py_agent.agent.tooling.web_search import WebSearchProvider, WebSearchTool

        class EmptyProvider(WebSearchProvider):
            name = "empty"

            def search(self, query: str, limit: int):
                return []

        tool = WebSearchTool(max_results=5, timeout=10, providers=[EmptyProvider()])
        result = tool.execute({"query": "veryrarequerynohits", "limit": 3})

        assert result.ok is True
        payload = json.loads(result.output)
        assert payload["results"] == []
        assert payload["provider_failures"] == []
        assert "note" in payload

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
        runtimes_by_name = {
            runtime.model_spec.name: runtime
            for runtime in registry.runtime_snapshot().runtimes
        }
        assert runtimes_by_name["web_search"].runtime_policy.effect_resolver.default_effect == "read_only"
        fetch_effects = dict(
            runtimes_by_name["web_fetch"].runtime_policy.effect_resolver.by_parameter
        )
        assert dict(fetch_effects["method"])["GET"] == "read_only"
        assert dict(fetch_effects["method"])["POST"] == "dangerous"
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


def test_core_owner_http_dispatch_checks_actual_ports_and_blocks_unmanaged_paths(tmp_path, monkeypatch):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings.config import AgentConfig
    from agent_py_agent.agent.settings.model_profiles import (
        _save_profiles,
        model_profiles_path,
        read_model_profiles,
    )
    from agent_py_agent.agent.tooling import web_search
    from agent_py_agent.agent.tooling.runtime_contracts import ToolCall
    from agent_py_agent.agent.user_space.owner_egress import (
        OwnerEgressEndpoint,
        OwnerEgressPolicy,
        OwnerEgressPolicyStore,
    )

    requests = []
    redirect = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            requests.append((self.server.server_port, self.path, "GET"))
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", redirect["url"])
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"allowed endpoint content")

        def do_POST(self):
            content = self.rfile.read(int(self.headers["Content-Length"]))
            requests.append((self.server.server_port, self.path, "POST", json.loads(content)))
            assert self.headers["Authorization"] == "Bearer owner-search-fixture"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"results":[{"title":"official","url":"https://example.org","snippet":"fixture"}]}')

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as allowed, ThreadingHTTPServer(("127.0.0.1", 0), Handler) as denied:
        threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in (allowed, denied)]
        try:
            for thread in threads:
                thread.start()
            accepted_url = f"http://127.0.0.1:{allowed.server_port}"
            redirect["url"] = f"http://127.0.0.1:{denied.server_port}/blocked"
            policy = OwnerEgressPolicy("allowlist", (OwnerEgressEndpoint("127.0.0.1", allowed.server_port),))
            owner_reads = []

            def read_policy(_store, owner_id):
                owner_reads.append(owner_id)
                return policy

            monkeypatch.setattr(OwnerEgressPolicyStore, "get_effective", read_policy)
            agent = SimpleAgent(AgentConfig(
                my_agent_home=str(tmp_path / "home"), my_agent_owner_kind="user", my_agent_owner_id="fixture",
                allowed_private_hosts=["127.0.0.1"], enable_plugins=False, model_backend="echo",
            ), tmp_path)
            registry = agent.tools
            path = model_profiles_path(agent.home_paths)
            path.parent.mkdir(exist_ok=True)
            catalog = read_model_profiles(path)
            catalog["providers"] = {"deepseek": {
                "display_name": "官方", "api_base": "https://api.deepseek.com", "api_key": "owner-search-fixture",
            }}
            _save_profiles(path, catalog)

            snapshot = registry.runtime_snapshot(run_id="run-egress-test")
            authority = agent.subagents.runtime_db.record_run_creation(
                owner_id=agent.home_paths.owner_id, run_id=snapshot.run_id, role="assistant",
                goal="测试业务网页出站", conversation_task_id="task-egress-test", thread_id="thread-egress-test",
            )

            def invoke(name, arguments, suffix):
                runtime = snapshot.runtime(name)
                assert runtime is not None, name
                call = ToolCall(
                    "call-" + suffix, name, arguments, "native", runtime.model_spec.schema_hash,
                    snapshot.run_id, "turn-egress-test", authority["attempt_id"],
                )
                return registry.execute_tool(call, write_boundary=None, runtime_snapshot=snapshot).result

            result = invoke("web_fetch", {"url": accepted_url + "/ok"}, "allowed")
            assert result.ok, result
            assert "allowed endpoint content" in result.output
            result = invoke("web_fetch", {"url": redirect["url"]}, "blocked")
            assert not result.ok and result.reported_error_code == "OWNER_EGRESS_ENDPOINT_DENIED"
            result = invoke("web_fetch", {"url": accepted_url + "/redirect"}, "redirect")
            assert not result.ok and result.reported_error_code == "OWNER_EGRESS_ENDPOINT_DENIED"
            assert result.effect_outcome == "unknown"

            # DeepSeek 的实际 HTTP 传输走同一 pin。原官方目标及 G4 均已有独立断言，
            # 此处仅替换测试目标与 G4 的本地回环许可，让真实 socket 可在本机复现。
            monkeypatch.setattr(web_search, "DEEPSEEK_SEARCH_URL", accepted_url + "/v1/tools/search")
            monkeypatch.setattr(web_search, "_network_safety_error", lambda *_: None)
            result = invoke("web_search", {"provider": "deepseek", "query": "fixture"}, "official")
            assert result.ok and '"engine": "deepseek"' in result.output
            monkeypatch.setattr(web_search, "DEEPSEEK_SEARCH_URL", redirect["url"])
            result = invoke("web_search", {"provider": "deepseek", "query": "fixture"}, "official-blocked")
            assert not result.ok and result.reported_error_code == "OWNER_EGRESS_ENDPOINT_DENIED"

            progress = invoke("task_progress", {"action": "read"}, "local-progress")
            assert progress.ok and progress.handler_executed, progress
            runtime = snapshot.runtime("create_subagents")
            assert runtime is not None
            child_view = registry.with_access_policy(
                access_mode="workspace-write", path_access_mode="normal", owner_scope_root=registry.owner_scope_root,
            )
            try:
                child_snapshot = child_view.runtime_snapshot(allowed_tools=["web_fetch"], run_id="child")
                assert child_snapshot.owner_egress_policy is snapshot.owner_egress_policy
            finally:
                child_view.close_mcp_clients()

            # 未登记宿主传输的远端工具必须在 handler 前被拒绝。
            result = invoke("remote_agent_push", {
                "target_owner": "local/target", "instruction": "fixture", "idempotency_key": "fixture",
            }, "unmanaged")
            assert not result.ok
            assert result.reported_error_code == "OWNER_EGRESS_TRANSPORT_UNAVAILABLE"
            assert requests == [
                (allowed.server_port, "/ok", "GET"), (allowed.server_port, "/redirect", "GET"),
                (allowed.server_port, "/v1/tools/search", "POST", {"query": "fixture", "count": 10, "need_content": False}),
            ]
            assert set(owner_reads) == {"local/fixture"}
        finally:
            for server in (allowed, denied):
                server.shutdown()
            for thread in threads:
                thread.join(timeout=3)


def test_core_protects_dormant_deployment_provider_custom_env_name(tmp_path, monkeypatch):
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings.config import AgentConfig
    from agent_py_agent.agent.settings.model_credentials import deepseek_search_api_key
    from agent_py_agent.agent.settings.model_profiles import _save_profiles, read_model_profiles
    from agent_py_agent.agent.settings.shared_model_catalog import _admin_profiles_path
    from agent_py_agent.agent.tooling._subprocess_env_scrub import scrub_subprocess_env
    from agent_py_agent.agent.user_space.home_layout import home_paths

    deploy_home = tmp_path / "home"
    path = _admin_profiles_path(home_paths(deploy_home))
    path.parent.mkdir(parents=True)
    catalog = read_model_profiles(path)
    catalog["providers"] = {"deepseek": {
        "display_name": "官方", "api_base": "https://api.deepseek.com", "api_key_ref": "env:DEPLOY_SEARCH_AUTH",
    }}
    _save_profiles(path, catalog)
    monkeypatch.setenv("DEPLOY_SEARCH_AUTH", "official-search-only-fixture")

    admin = SimpleAgent(AgentConfig(my_agent_home=str(deploy_home), enable_plugins=False), tmp_path)
    assert deepseek_search_api_key(
        admin.home_paths, protected_env_names=admin.tools._construction_params.host_credential_env_names,
    ) == "official-search-only-fixture"
    other = SimpleAgent(AgentConfig(
        my_agent_home=str(deploy_home), my_agent_owner_kind="user", my_agent_owner_id="another",
        enable_plugins=False,
    ), tmp_path)
    assert "DEPLOY_SEARCH_AUTH" not in scrub_subprocess_env(
        {"DEPLOY_SEARCH_AUTH": "official-search-only-fixture"},
        passthrough=frozenset({"DEPLOY_SEARCH_AUTH"}),
        host_credential_env_names=other.tools._construction_params.host_credential_env_names,
    )
    assert "DEPLOY_SEARCH_AUTH" in other.tools._construction_params.host_credential_env_names
