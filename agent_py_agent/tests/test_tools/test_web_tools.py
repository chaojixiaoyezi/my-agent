"""LLM: tests for web_fetch URL and API behavior.

给人看的解释：
这个文件专门测试统一后的 web_fetch 网页和 HTTP/API 能力，避免文件系统工具测试继续膨胀。
"""

from agent_py_agent.agent.tooling.web import WebFetchTool

from .backends import start_test_server


def test_web_fetch_handles_page_and_api_requests(monkeypatch):
    """LLM: verify that WebFetchTool works for both page reads and API calls.

    新手说明:
    启动本地 HTTP 服务，分别测试 GET 抓取和 POST 回显。
    """
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    server = start_test_server()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        fetch_tool = WebFetchTool(max_chars=2000, timeout=5, allowed_private_hosts=("127.0.0.1",))

        fetch_result = fetch_tool.execute({"url": base + "/page"})
        http_result = fetch_tool.execute(
            {
                "url": base + "/echo",
                "method": "POST",
                "headers": {"Content-Type": "text/plain; charset=utf-8"},
                "body": "hello api",
            }
        )

        assert fetch_result.ok
        assert "demo page" in fetch_result.output
        assert http_result.ok
        assert "hello api" in http_result.output
    finally:
        server.shutdown()
        server.server_close()


def test_web_tools_reject_unsafe_urls_and_bad_request_parameters():
    """LLM: verify that web tools reject unsafe URLs and malformed request fields.

    新手说明:
    file://、带用户名密码的 URL、含换行的 header、非法 method、非字符串 body 都应被拒绝。
    """
    fetch_tool = WebFetchTool(max_chars=2000, timeout=5)

    file_url = fetch_tool.execute({"url": "file:///etc/passwd"})
    userinfo_url = fetch_tool.execute({"url": "https://user:pass@example.com"})
    bad_header = fetch_tool.execute({"url": "https://example.com", "headers": {"X-Test": "ok\nbad"}})
    bad_method = fetch_tool.execute({"url": "https://example.com", "method": "GET\nPOST"})
    bad_body = fetch_tool.execute({"url": "https://example.com", "body": {"not": "text"}})

    assert not file_url.ok
    assert "http 或 https" in file_url.output
    assert not userinfo_url.ok
    assert "用户名或密码" in userinfo_url.output
    assert not bad_header.ok
    assert "headers 值不能包含换行" in bad_header.output
    assert not bad_method.ok
    assert "method 必须是有效的 HTTP 方法名" in bad_method.output
    assert not bad_body.ok
    assert "body 参数必须是字符串或标量文本" in bad_body.output


import base64

from agent_py_agent.agent.tooling.web_search import (
    BingHtmlProvider,
    WebSearchProvider,
    WebSearchTool,
    _decode_bing_redirect,
    _parse_bing_results,
)


def _bing_ck(real_url: str) -> str:
    """构造 Bing /ck/a 跳转链接(真实 URL = a1 + base64url)。"""
    enc = "a1" + base64.urlsafe_b64encode(real_url.encode()).decode().rstrip("=")
    return f"https://www.bing.com/ck/a?!&amp;&amp;p=x&amp;u={enc}&amp;ntb=1"


def test_bing_parses_blocks_without_misalignment():
    """R12-R15 实锤钉子:按 b_algo 块解析,标题/真实URL/摘要逐块对应,绝不错位
    (github 产物按行号瞎对应→序号错位的同源病,块内解析根治)。"""
    html = (
        '<ol id="b_results">'
        f'<li class="b_algo"><h2><a href="{_bing_ck("https://arxiv.org/abs/2601.20552")}">'
        'OCR 2 <strong>Paper</strong></a></h2>'
        '<div class="b_caption"><p class="b_lineclamp2">Visual causal flow.</p></div></li>'
        f'<li class="b_algo"><h2><a href="{_bing_ck("https://github.com/deepseek-ai/DeepSeek-V3")}">'
        'V3 on GitHub</a></h2><div class="b_caption"><p>Weights and code.</p></div></li>'
        "</ol>"
    )
    rows = _parse_bing_results(html, 5)
    assert len(rows) == 2
    assert rows[0]["url"] == "https://arxiv.org/abs/2601.20552"
    assert rows[0]["title"] == "OCR 2 Paper"  # 内层标签清掉
    assert rows[0]["snippet"] == "Visual causal flow."
    assert rows[1]["url"] == "https://github.com/deepseek-ai/DeepSeek-V3"
    assert rows[1]["title"] == "V3 on GitHub"  # 第二块的标题没串到第一块的 URL


def test_bing_redirect_decode_variants():
    assert _decode_bing_redirect(_bing_ck("https://example.com/a")) == "https://example.com/a"
    assert _decode_bing_redirect("https://plain.example.com/x") == "https://plain.example.com/x"
    assert _decode_bing_redirect("https://www.bing.com/ck/a?u=broken") == ""  # 非 a1 前缀→空


def test_websearch_default_providers_bing_primary():
    """单点故障修复:默认多后端,Bing 首选 DuckDuckGo 兜底。"""
    tool = WebSearchTool(timeout=5)
    names = [type(p).__name__ for p in tool.providers]
    assert names[0] == "BingHtmlProvider"
    assert "DuckDuckGoHtmlProvider" in names


def test_websearch_falls_back_when_primary_returns_empty():
    """主后端空(反爬/被墙)时按序 fallback 到下一后端,不再一挂全挂。"""

    class _EmptyProvider(WebSearchProvider):
        name = "empty"

        def search(self, query, limit):
            return []

    class _GoodProvider(WebSearchProvider):
        name = "good"

        def search(self, query, limit):
            return [{"title": "T", "url": "https://example.com", "snippet": "S", "source": "good"}]

    tool = WebSearchTool(timeout=5, providers=[_EmptyProvider(), _GoodProvider()])
    result = tool.execute({"query": "anything", "limit": 3})
    assert result.ok is True
    import json

    payload = json.loads(result.output)
    assert payload["engine"] == "good"
    assert len(payload["results"]) == 1
