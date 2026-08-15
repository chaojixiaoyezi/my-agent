"""浏览器自动化工具测试 —— 单入口 browser(action=navigate/snapshot/click/type/close)+ 会话管理器。

分层覆盖,全部 CI 友好(不依赖外网):
  - 会话管理器层(BrowserSessionManager):用 data: URL 起真 headless Chromium,
    验证 navigate/snapshot/click/type/close 工作、a11y 快照含 ref、ref 过期优雅报错。
  - 工具层(不需要浏览器):SSRF 挡内网(导航前就拦,不起浏览器)、缺参数报错、
    精确 schema、注册进 registry。
  - 工具层端到端(需要浏览器):用本地 http.server + MY_AGENT_ALLOW_PRIVATE_URLS 放行,
    走完整 navigate→snapshot→type→click→close。

浏览器二进制处理:playwright 没装 / Chromium 没下载时,需要真浏览器的用例整体 skip
(而非 fail),并打印原因。不需要浏览器的用例(SSRF/参数校验/schema/注册)始终跑。
"""
from __future__ import annotations

import http.server
import json
import socketserver
import threading

import pytest

from agent_py_agent.agent.tooling.browser_session import (
    BrowserSessionManager,
    BrowserUnavailableError,
)
from agent_py_agent.agent.tooling.browser_tools import (
    BrowserTool,
    browser_tools,
)

# ---------------------------------------------------------------------------
# 浏览器可用性探测:决定需要真浏览器的用例是 skip 还是跑。
# ---------------------------------------------------------------------------


def test_readiness_check_does_not_start_browser_or_create_session():
    """LLM: rendering a tool snapshot must never allocate browser resources."""
    manager = BrowserSessionManager()

    manager.readiness_error()

    assert manager._browser is None
    assert manager._playwright is None
    assert manager._sessions == {}


def _browser_available() -> tuple[bool, str]:
    """探测能否真起 headless Chromium。返回 (可用, 原因)。"""
    try:
        from playwright.sync_api import sync_playwright  # type: ignore[import-not-found]
    except ImportError:
        return False, "playwright 未安装(pip install playwright)"
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            browser.close()
    except Exception as exc:  # 二进制没下载/启动失败
        return False, f"Chromium 二进制不可用(python -m playwright install chromium):{exc}"
    return True, ""


_BROWSER_OK, _BROWSER_SKIP_REASON = _browser_available()
_requires_browser = pytest.mark.skipif(not _BROWSER_OK, reason=_BROWSER_SKIP_REASON)


_LOGIN_HTML = (
    "data:text/html,"
    "<html><head><title>Login Page</title></head><body>"
    "<h1>Welcome</h1>"
    '<a href="https://example.com/about">About us</a>'
    '<input type="text" aria-label="Search box">'
    '<button id="go">Go</button>'
    "</body></html>"
)


# ---------------------------------------------------------------------------
# 会话管理器层(需要真浏览器)
# ---------------------------------------------------------------------------


@pytest.fixture
def manager():
    """每个用例一个独立 manager,teardown 关浏览器(不泄漏进程)。"""
    mgr = BrowserSessionManager()
    try:
        yield mgr
    finally:
        mgr.shutdown()


@_requires_browser
class TestSessionManager:
    def test_navigate_returns_title_and_snapshot(self, manager: BrowserSessionManager):
        result = manager.navigate("s1", _LOGIN_HTML)
        assert result["title"] == "Login Page"
        assert "Welcome" in result["snapshot"]
        # a11y 快照应含可交互元素的 ref(无需视觉就能定位)
        assert "[ref=e" in result["snapshot"]
        assert result["element_count"] >= 2

    def test_snapshot_after_navigate(self, manager: BrowserSessionManager):
        manager.navigate("s1", _LOGIN_HTML)
        snap = manager.snapshot("s1")
        assert "Search box" in snap["snapshot"]
        assert snap["url"].startswith("data:text/html")

    def test_snapshot_without_navigate_raises(self, manager: BrowserSessionManager):
        with pytest.raises(ValueError):
            manager.snapshot("never")

    def test_type_fills_input(self, manager: BrowserSessionManager):
        manager.navigate("s1", _LOGIN_HTML)
        input_ref = _first_ref(manager, "s1", tag="input")
        manager.type_text("s1", input_ref, "hello world")
        page = manager._sessions["s1"].page
        value = page.input_value(f"[data-agent-ref='{input_ref}']")
        assert value == "hello world"

    def test_click_does_not_hang_on_navigation(self, manager: BrowserSessionManager):
        # 点表单按钮会触发导航;click 用 no_wait_after,不能卡住。
        manager.navigate("s1", _LOGIN_HTML)
        button_ref = _first_ref(manager, "s1", tag="button")
        result = manager.click("s1", button_ref)
        assert result["clicked"] == button_ref

    def test_stale_ref_raises_value_error(self, manager: BrowserSessionManager):
        manager.navigate("s1", _LOGIN_HTML)
        with pytest.raises(ValueError, match="不存在"):
            manager.click("s1", "e999")

    def test_css_selector_passthrough(self, manager: BrowserSessionManager):
        manager.navigate("s1", _LOGIN_HTML)
        # 非 ref 形式当作 CSS selector 透传。
        result = manager.click("s1", "#go")
        assert result["clicked"] == "#go"

    def test_sessions_are_isolated(self, manager: BrowserSessionManager):
        manager.navigate("a", _LOGIN_HTML)
        manager.navigate("b", "data:text/html,<title>Other</title><h1>B</h1>")
        assert manager.snapshot("a")["title"] == "Login Page"
        assert manager.snapshot("b")["title"] == "Other"
        assert set(manager.active_session_ids()) == {"a", "b"}

    def test_close_is_idempotent(self, manager: BrowserSessionManager):
        manager.navigate("s1", _LOGIN_HTML)
        assert manager.close("s1")["status"] == "closed"
        assert manager.close("s1")["status"] == "already_closed"

    def test_lazy_start_no_browser_until_first_use(self):
        # 仅构造 manager 不应启动浏览器(惰性)。
        mgr = BrowserSessionManager()
        assert mgr._browser is None
        assert mgr.active_session_ids() == []
        # 首次导航才起浏览器。
        mgr.navigate("s1", _LOGIN_HTML)
        assert mgr._browser is not None
        mgr.shutdown()
        assert mgr._browser is None


# ---------------------------------------------------------------------------
# 工具层:SSRF / 参数校验 / schema / 注册(不需要浏览器,始终跑)
# ---------------------------------------------------------------------------


class TestSSRFProtection:
    """导航前复用 network_safety gate 挡内网,且在起浏览器之前就拦下。"""

    @pytest.mark.parametrize(
        "url, expected_code",
        [
            ("http://localhost:8080/", "NETWORK_PRIVATE_HOST_BLOCKED"),
            ("http://127.0.0.1/", "NETWORK_PRIVATE_HOST_BLOCKED"),
            ("http://192.168.1.1/", "NETWORK_PRIVATE_HOST_BLOCKED"),
            ("http://169.254.169.254/latest/meta-data/", "NETWORK_ALWAYS_BLOCKED_IP"),
            ("http://metadata.google.internal/", "NETWORK_ALWAYS_BLOCKED_HOST"),
        ],
    )
    def test_private_urls_blocked(self, url: str, expected_code: str, monkeypatch):
        # 确保 env 放行没开(默认拒私网)。
        monkeypatch.delenv("MY_AGENT_ALLOW_PRIVATE_URLS", raising=False)
        result = BrowserTool().execute({"action": "navigate", "url": url})
        assert result.ok is False
        assert result.error_code == expected_code

    def test_ssrf_blocks_before_browser_launch(self, monkeypatch):
        """SSRF 拦截发生在起浏览器之前:用一个一旦被调用就报错的假 manager 验证。"""
        monkeypatch.delenv("MY_AGENT_ALLOW_PRIVATE_URLS", raising=False)

        class _ExplodingManager:
            def navigate(self, *a, **k):
                raise AssertionError("SSRF 应在调用 manager.navigate 之前就拦下")

        tool = BrowserTool(manager=_ExplodingManager())
        result = tool.execute({"action": "navigate", "url": "http://127.0.0.1:9999/"})
        assert result.ok is False
        assert result.error_code.startswith("NETWORK_")

    def test_invalid_url_scheme_rejected(self):
        result = BrowserTool().execute({"action": "navigate", "url": "ftp://example.com/"})
        assert result.ok is False
        assert result.error_code == "TOOL_INVALID_ARGUMENTS"


class TestArgumentValidation:
    """缺/空参数优雅报 TOOL_INVALID_ARGUMENTS,不崩。"""

    def test_unknown_action(self):
        result = BrowserTool().execute({"action": "teleport"})
        assert result.ok is False
        assert result.error_code == "TOOL_INVALID_ARGUMENTS"

    def test_navigate_missing_url(self):
        result = BrowserTool().execute({"action": "navigate"})
        assert result.ok is False
        assert result.error_code == "TOOL_INVALID_ARGUMENTS"

    def test_click_missing_ref(self):
        result = BrowserTool().execute({"action": "click"})
        assert result.ok is False
        assert result.error_code == "TOOL_INVALID_ARGUMENTS"

    def test_type_missing_ref(self):
        result = BrowserTool().execute({"action": "type", "text": "hi"})
        assert result.ok is False
        assert result.error_code == "TOOL_INVALID_ARGUMENTS"

    def test_type_missing_text(self):
        result = BrowserTool().execute({"action": "type", "ref": "e1"})
        assert result.ok is False
        assert result.error_code == "TOOL_INVALID_ARGUMENTS"


class TestBrowserUnavailable:
    """playwright/Chromium 不可用时返回 TOOL_UNAVAILABLE(带指引),不崩。"""

    def test_unavailable_maps_to_tool_unavailable(self):
        class _UnavailableManager:
            def navigate(self, *a, **k):
                raise BrowserUnavailableError("浏览器没装,请 pip install playwright")

        tool = BrowserTool(manager=_UnavailableManager())
        # 用合法公网 URL 过 SSRF,再在 manager 层抛不可用。
        result = tool.execute({"action": "navigate", "url": "https://example.com/"})
        assert result.ok is False
        assert result.error_code == "TOOL_UNAVAILABLE"
        body = json.loads(result.output)
        assert "playwright" in body["message"]

    def test_action_error_maps_to_network_failed(self):
        # 用可解析的公网域名(过 SSRF gate),manager 层模拟导航失败 → NETWORK_REQUEST_FAILED。
        class _BrokenManager:
            def navigate(self, *a, **k):
                raise RuntimeError("net::ERR_CONNECTION_REFUSED")

        result = BrowserTool(manager=_BrokenManager()).execute({"action": "navigate", "url": "https://example.com/"})
        assert result.ok is False
        assert result.error_code == "NETWORK_REQUEST_FAILED"

    def test_timeout_maps_to_tool_timeout(self):
        class _SlowManager:
            def navigate(self, *a, **k):
                raise RuntimeError("Page.goto: Timeout 30000ms exceeded")

        result = BrowserTool(manager=_SlowManager()).execute({"action": "navigate", "url": "https://example.com/"})
        assert result.ok is False
        assert result.error_code == "TOOL_TIMEOUT"


class TestSessionLifecycleRobustness:
    """会话生命周期健壮性(mock playwright 层,不需要真浏览器)。"""

    def test_session_creation_failure_closes_context_no_leak(self):
        """page 建失败时已建的 context 被关闭——不泄露浏览器资源(否则反复失败累积泄露 context)。"""
        from unittest.mock import MagicMock

        mgr = BrowserSessionManager()
        fake_context = MagicMock()
        fake_context.new_page.side_effect = RuntimeError("page creation failed")
        mgr._browser = MagicMock()  # 绕过真实启动(_ensure_browser_locked 见 _browser 非 None 即返回)
        mgr._browser.new_context.return_value = fake_context

        with pytest.raises(RuntimeError):
            mgr._get_or_create_session_locked("s1")

        fake_context.close.assert_called_once()  # ⭐ context 被关,不泄露
        assert "s1" not in mgr._sessions  # 半建会话不留痕,下次重试干净

    def test_successful_session_does_not_close_context(self):
        """正常路径不误关 context(回归保护:别让修复把成功路径也关了)。"""
        from unittest.mock import MagicMock

        mgr = BrowserSessionManager()
        fake_context = MagicMock()
        mgr._browser = MagicMock()
        mgr._browser.new_context.return_value = fake_context

        session = mgr._get_or_create_session_locked("s1")
        fake_context.close.assert_not_called()  # 成功路径不关
        assert mgr._sessions.get("s1") is session  # 会话被缓存,复用


class TestToolSpecs:
    def test_single_browser_tool_with_action_enum(self):
        tool = BrowserTool()
        spec = tool.model_spec
        assert spec.name == "browser"
        assert spec.category == "web"
        # 条件必填(url/ref/text 按 action)在运行时校验,工具级只必填 action
        assert spec.input_schema["required"] == ["action"]
        assert spec.input_schema["properties"]["action"]["enum"] == [
            "navigate", "snapshot", "click", "type", "close",
        ]
        # 整组取最严:浏览器交互有状态有副作用 → mutating + 幂等(自动派生 key)
        assert tool.runtime_policy.effect_resolver.default_effect == "mutating"
        assert tool.runtime_policy.idempotency_policy.scope == "operation"

    def test_all_tools_factory(self):
        names = {t.model_spec.name for t in browser_tools()}
        assert names == {"browser"}  # 5 个动作合成 1 个入口

    def test_registered_in_registry(self, tmp_path):
        from agent_py_agent.agent.tooling.registry import ToolRegistry, ToolRegistryParams

        params = ToolRegistryParams(
            workspace_root=tmp_path,
            max_chars=1000,
            max_entries=50,
            max_matches=50,
            web_max_chars=1000,
            http_timeout=10,
            catalog_limit=200,
            retrieval_limit=10,
            vector_search_enabled=False,
        )
        registry = ToolRegistry(params)
        names = set(registry.tools)
        assert "browser" in names
        snapshot = registry.runtime_snapshot()
        if registry.tools["browser"].availability().available:
            assert "browser" in snapshot.available_tool_names
        else:
            assert "browser" not in snapshot.available_tool_names
            assert any(name == "browser" for name, _code, _reason in snapshot.unavailable_tools)
        # 旧的逐动作工具名已不再单独注册
        assert "browser_navigate" not in names


# ---------------------------------------------------------------------------
# 工具层端到端(需要浏览器):本地 http.server + 私网放行
# ---------------------------------------------------------------------------


@pytest.fixture
def local_server():
    """起一个本地 http.server 服务一个简单 HTML 页面。返回 base url。"""
    html = (
        b"<html><head><title>Local Test</title></head><body>"
        b"<h1>Hi from server</h1>"
        b'<input type="text" aria-label="Query">'
        b"<button>Submit</button>"
        b"</body></html>"
    )

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(html)

        def log_message(self, *args):  # 静音
            pass

    server = socketserver.TCPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}/"
    finally:
        server.shutdown()
        server.server_close()


@_requires_browser
class TestToolEndToEnd:
    """完整工具链:navigate→snapshot→type→click→close,经 registry-facing 工具 API。"""

    def test_full_flow_via_local_server(self, local_server: str, monkeypatch):
        # 放行私网(本地测试页),否则 SSRF 会拦 127.0.0.1。
        monkeypatch.setenv("MY_AGENT_ALLOW_PRIVATE_URLS", "1")
        session = "e2e"
        browser = BrowserTool()
        try:
            nav = browser.execute({"action": "navigate", "url": local_server, "session_id": session})
            assert nav.ok, nav.output
            nav_body = json.loads(nav.output)
            assert nav_body["title"] == "Local Test"
            assert "Hi from server" in nav_body["snapshot"]
            assert nav_body["element_count"] >= 2

            snap = browser.execute({"action": "snapshot", "session_id": session})
            assert snap.ok
            assert "Query" in json.loads(snap.output)["snapshot"]

            typed = browser.execute({"action": "type", "session_id": session, "ref": "e1", "text": "playwright"})
            assert typed.ok
            assert json.loads(typed.output)["typed"] == "playwright"

            clicked = browser.execute({"action": "click", "session_id": session, "ref": "e2"})
            assert clicked.ok
            assert json.loads(clicked.output)["clicked"] == "e2"

            bad = browser.execute({"action": "click", "session_id": session, "ref": "e404"})
            assert bad.ok is False
            assert bad.error_code == "TOOL_INVALID_ARGUMENTS"
        finally:
            browser.execute({"action": "close", "session_id": session})
            from agent_py_agent.agent.tooling.browser_session import browser_session_manager

            browser_session_manager.shutdown()


# ---------------------------------------------------------------------------
# 测试辅助
# ---------------------------------------------------------------------------


def _first_ref(manager: BrowserSessionManager, session_id: str, *, tag: str) -> str:
    """从当前快照的 ref_map 里取第一个指定 tag 的元素 ref。"""
    session = manager._sessions[session_id]
    for ref, meta in session.ref_map.items():
        if meta.get("tag") == tag:
            return ref
    raise AssertionError(f"快照里没有 tag={tag} 的元素")
