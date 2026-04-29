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
        assert prompt.index("# User Task") < prompt.index("# Tool Transcript")
        assert "# Continue From Tool Transcript" in prompt
        assert "hello tool world" in prompt
        return ModelResponse(text="工具执行完成", backend=self.name)


class SubagentDelegationBackend(BaseBackend):
    """假的模型后端：模拟主代理从自然语言里真正创建子代理。"""

    name = "fake_subagent_delegation_backend"

    def __init__(self):
        self.calls = 0

    def generate(self, prompt: str) -> ModelResponse:
        self.calls += 1
        if self.calls == 1:
            assert "create_subagents [orchestration]" in prompt
            return ModelResponse(
                text=(
                    "[TOOL_CALL]\n"
                    "{"
                    '"tool":"create_subagents",'
                    '"goal":"隔离场景测试：实现 fixture 功能并产出证据",'
                    '"count":2,'
                    '"tool_preset":"coding",'
                    '"acceptance_checks":["必须有文件证据","必须说明测试结果"]'
                    "}\n"
                    "[/TOOL_CALL]"
                ),
                backend=self.name,
            )
        assert "subagent_workspace" in prompt
        assert "write_file" in prompt
        return ModelResponse(text="已创建子代理任务并等待调度。", backend=self.name)


class DuplicateSubagentDelegationBackend(BaseBackend):
    """假的模型后端：连续重复同一个派工工具调用。"""

    name = "fake_duplicate_subagent_delegation_backend"

    def __init__(self):
        self.calls = 0

    def generate(self, prompt: str) -> ModelResponse:
        self.calls += 1
        if self.calls <= 2:
            return ModelResponse(
                text=(
                    "[TOOL_CALL]\n"
                    "{"
                    '"tool":"create_subagents",'
                    '"goal":"重复派工防护测试",'
                    '"count":1,'
                    '"tool_preset":"read_only"'
                    "}\n"
                    "[/TOOL_CALL]"
                ),
                backend=self.name,
            )
        assert "阻止重复执行" in prompt
        return ModelResponse(text="重复派工已被拦截并收口。", backend=self.name)


class MaxToolRoundBackend(BaseBackend):
    """假的模型后端：验证工具轮数到顶时会再生成最终回答。"""

    name = "fake_max_tool_round_backend"

    def __init__(self):
        self.calls = 0

    def generate(self, prompt: str) -> ModelResponse:
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                text='[TOOL_CALL]\n{"tool":"read_file","path":"notes.txt"}\n[/TOOL_CALL]',
                backend=self.name,
            )
        assert "已达到最大工具轮数限制" in prompt
        return ModelResponse(text="工具轮数到顶后已正常收口。", backend=self.name)


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


def test_agent_can_delegate_to_subagents_from_tool_call():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        cfg = AgentConfig(
            enable_tools=True,
            memory_path="memory.jsonl",
            subagent_workspace="subs",
            max_subagents=3,
        )
        agent = SimpleAgent(cfg, workspace)
        agent.backend = SubagentDelegationBackend()

        result = agent.run("请创建两个子代理做隔离 coding 场景测试", save=False)
        tasks = agent.subagents.list_runs()
        board = agent.tools.execute_call({"tool": "subagent_board", "limit": 5})
        dry_dispatch = agent.tools.execute_call({"tool": "dispatch_subagents", "apply": False, "max_runners": 1})
        blocked_dispatch = agent.tools.execute_call(
            {"tool": "dispatch_subagents", "execute_runners": True, "apply": False}
        )

        assert result.response == "已创建子代理任务并等待调度。"
        assert result.tool_rounds == 1
        assert len(tasks) == 2
        assert all("write_file" in task.allowed_tools for task in tasks)
        assert board.ok
        assert tasks[0].id in board.output
        assert dry_dispatch.ok
        assert '"dry_run": true' in dry_dispatch.output
        assert not blocked_dispatch.ok
        assert "必须配合 apply=true" in blocked_dispatch.output


def test_repeated_orchestration_tool_call_is_not_executed_twice():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        cfg = AgentConfig(
            enable_tools=True,
            memory_path="memory.jsonl",
            subagent_workspace="subs",
            max_tool_rounds=4,
        )
        agent = SimpleAgent(cfg, workspace)
        agent.backend = DuplicateSubagentDelegationBackend()

        result = agent.run("请只创建一个子代理", save=False)
        tasks = agent.subagents.list_runs()

        assert result.response == "重复派工已被拦截并收口。"
        assert result.tool_rounds == 2
        assert len(tasks) == 1


def test_max_tool_rounds_generates_final_response():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        (workspace / "notes.txt").write_text("hello", encoding="utf-8")
        cfg = AgentConfig(
            enable_tools=True,
            memory_path="memory.jsonl",
            max_tool_rounds=0,
        )
        agent = SimpleAgent(cfg, workspace)
        agent.backend = MaxToolRoundBackend()

        result = agent.run("读取 notes", save=False)

        assert result.response == "工具轮数到顶后已正常收口。"
        assert result.tool_rounds == 0
        assert agent.backend.calls == 2


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


def test_tool_call_parser_accepts_subagent_call_alias():
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
    calls = registry.parse_tool_calls(
        '[SUBAGENT_CALL]\n{"tool":"read_file","path":"README.md"}\n[/TOOL_CALL]'
    )

    assert calls == [{"tool": "read_file", "path": "README.md"}]


def test_tool_call_parser_accepts_qwen_xmlish_read_call():
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
    calls = registry.parse_tool_calls(
        "<tool_call>\n"
        "<function=read>\n"
        "<parameter=file_path>\nREADME.md\n</parameter>\n"
        "</function>\n"
        "</tool_call>"
    )

    assert calls == [{"tool": "read_file", "path": "README.md"}]


def test_tool_call_parser_accepts_qwen_xmlish_write_call():
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
    calls = registry.parse_tool_calls(
        '<tool_call><function name="write">'
        '<parameter name="file_path">notes.txt</parameter>'
        '<parameter name="content">hello &amp; hi</parameter>'
        "</function></tool_call>"
    )

    assert calls == [{"tool": "write_file", "path": "notes.txt", "content": "hello & hi"}]


def test_tool_call_parser_reports_incomplete_qwen_xmlish_call():
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
    calls = registry.parse_tool_calls(
        "<tool_call><function=read><parameter=file_path>A.md</parameter></tool_call>\n"
        "<tool_call><function=read><parameter=file_path>B.md</parameter>"
    )

    assert calls[0] == {"tool": "read_file", "path": "A.md"}
    assert calls[1]["tool"] == "__parse_error__"
    assert "missing a closing </tool_call>" in calls[1]["error"]
    assert "B.md" in calls[1]["raw"]


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
