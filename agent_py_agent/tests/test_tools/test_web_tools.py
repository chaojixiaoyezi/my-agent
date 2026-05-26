"""LLM: tests for fetch_url and http_request tool behavior.

给人看的解释：
这个文件专门测试网页和 HTTP 工具，避免文件系统工具测试继续膨胀。
"""

from agent_py_agent.agent.tools import FetchUrlTool, HttpRequestTool

from .backends import start_test_server


def test_fetch_url_and_http_request_tools(monkeypatch):
    """LLM: verify that FetchUrlTool and HttpRequestTool work against a local test server.

    新手说明:
    启动本地 HTTP 服务，分别测试 GET 抓取和 POST 回显。
    """
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    server = start_test_server()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        fetch_tool = FetchUrlTool(max_chars=2000, timeout=5, allowed_private_hosts=("127.0.0.1",))
        http_tool = HttpRequestTool(max_chars=2000, timeout=5, allowed_private_hosts=("127.0.0.1",))

        fetch_result = fetch_tool.execute({"url": base + "/page"})
        http_result = http_tool.execute(
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
    fetch_tool = FetchUrlTool(max_chars=2000, timeout=5)
    http_tool = HttpRequestTool(max_chars=2000, timeout=5)

    file_url = fetch_tool.execute({"url": "file:///etc/passwd"})
    userinfo_url = fetch_tool.execute({"url": "https://user:pass@example.com"})
    bad_header = http_tool.execute({"url": "https://example.com", "headers": {"X-Test": "ok\nbad"}})
    bad_method = http_tool.execute({"url": "https://example.com", "method": "GET\nPOST"})
    bad_body = http_tool.execute({"url": "https://example.com", "body": {"not": "text"}})

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
