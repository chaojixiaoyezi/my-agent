"""工具系统回归测试。"""

from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import tempfile
import threading

from agent_py_agent.agent.backend import BaseBackend, ModelResponse
from agent_py_agent.agent.config import AgentConfig
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.tools import (
    AppendFileTool,
    FetchUrlTool,
    HttpRequestTool,
    ReplaceInFileTool,
    ToolRegistry,
    WriteFileTool,
)


class ToolCallingBackend(BaseBackend):
    """假的模型后端：先要求调用工具，再给最终答案。"""

    name = "fake_tool_backend"

    def __init__(self):
        self.calls = 0

    def generate(self, prompt: str) -> ModelResponse:
        self.calls += 1
        if self.calls == 1:
            assert "# Tool Catalog" in prompt
            assert "# Recommended Tools" in prompt
            return ModelResponse(
                text='[TOOL_CALL]\n{"tool": "read_file", "path": "notes.txt"}\n[/TOOL_CALL]',
                backend=self.name,
            )
        assert "hello tool world" in prompt
        return ModelResponse(text="工具执行完成", backend=self.name)


class DemoHandler(BaseHTTPRequestHandler):
    """本地测试 HTTP 服务。"""

    def do_GET(self):
        if self.path == "/page":
            body = "demo page"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        if self.path == "/echo":
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length).decode("utf-8")
            payload = {"received": raw}
            body = json.dumps(payload, ensure_ascii=False)
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, format, *args):
        return


def start_test_server():
    """启动一个本地 HTTP 服务供工具测试使用。"""

    server = HTTPServer(("127.0.0.1", 0), DemoHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def test_tool_loop_and_prompt_transcript():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        (workspace / "notes.txt").write_text("hello tool world", encoding="utf-8")
        cfg = AgentConfig(enable_tools=True, memory_path="memory.jsonl")
        agent = SimpleAgent(cfg, workspace)
        agent.backend = ToolCallingBackend()
        result = agent.run("读取 notes.txt 并总结", save=False)
        assert result.response == "工具执行完成"
        assert result.tool_rounds == 1
        assert "hello tool world" in result.prompt


def test_tool_catalog_and_recommended_sections():
    registry = ToolRegistry(
        Path.cwd(),
        max_chars=6000,
        max_entries=200,
        max_matches=50,
        web_max_chars=12000,
        http_timeout=30,
        catalog_limit=20,
        retrieval_limit=3,
        vector_search_enabled=False,
    )

    catalog = registry.render_catalog_section()
    recommended = registry.render_recommended_tools_section("帮我测试一个 REST API 接口并查看返回")

    assert "# Tool Catalog" in catalog
    assert "http_request [api]" in catalog
    assert "适用场景" in catalog
    assert "## http_request" in recommended
    assert "推荐理由" in recommended


def test_tool_allowlist_limits_prompt_and_execution():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        cfg = AgentConfig(enable_tools=True, memory_path="memory.jsonl")
        agent = SimpleAgent(cfg, workspace)
        result = agent.run("读取 notes.txt", save=False, allowed_tools=["read_file"])
        blocked = agent.tools.execute_call(
            {"tool": "write_file", "path": "x.txt", "content": "x"},
            allowed_tools=["read_file"],
        )

        assert "read_file [filesystem]" in result.prompt
        assert "write_file [filesystem]" not in result.prompt
        assert not blocked.ok
        assert "未授权" in blocked.output


def test_write_and_append_file_tools():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        write_tool = WriteFileTool(workspace)
        append_tool = AppendFileTool(workspace)

        write_result = write_tool.execute({"path": "src/demo.py", "content": "print('a')\n"})
        append_result = append_tool.execute({"path": "src/demo.py", "content": "print('b')\n"})

        assert write_result.ok
        assert append_result.ok
        assert (workspace / "src" / "demo.py").read_text(encoding="utf-8") == "print('a')\nprint('b')\n"


def test_replace_in_file_tool():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        target = workspace / "src" / "demo.py"
        target.parent.mkdir(parents=True)
        target.write_text("def hello():\n    return 'old'\n", encoding="utf-8")

        tool = ReplaceInFileTool(workspace)
        result = tool.execute(
            {
                "path": "src/demo.py",
                "old": "return 'old'",
                "new": "return 'new'",
            }
        )

        assert result.ok
        assert "替换 1 处" in result.output
        assert target.read_text(encoding="utf-8") == "def hello():\n    return 'new'\n"


def test_fetch_url_and_http_request_tools():
    server = start_test_server()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        fetch_tool = FetchUrlTool(max_chars=2000, timeout=5)
        http_tool = HttpRequestTool(max_chars=2000, timeout=5)

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
