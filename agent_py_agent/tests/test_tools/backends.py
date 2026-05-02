"""LLM: fake backends, HTTP test server and helpers for tool system regression tests.

给人看的解释：
这个文件放所有"假的模型后端"、本地测试 HTTP 服务和工具注册表工厂。
测试用例里需要一个能模拟工具调用 / 子代理派工 / 最大轮数等行为的后端时，从这里导入即可。
"""

from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import threading

from agent_py_agent.agent.backend import BaseBackend, ModelResponse
from agent_py_agent.agent.tools import ToolRegistry


class ToolCallingBackend(BaseBackend):
    """LLM: fake model backend that first requests a tool call, then gives a final answer.

    新手说明:
    第一次调用时假装要读文件，第二次调用时给出最终回答。
    用来测试 SimpleAgent 的工具循环是否正确地把工具结果喂回模型。
    """

    name = "fake_tool_backend"

    def __init__(self):
        self.calls = 0

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
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
    """LLM: fake model backend that simulates a main agent creating sub-agents from natural language.

    新手说明:
    第一次调用时发出 create_subagents 工具调用，第二次调用时确认已创建。
    用来测试子代理派工流程和调度是否完整。
    """

    name = "fake_subagent_delegation_backend"

    def __init__(self):
        self.calls = 0

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
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
    """LLM: fake model backend that repeats the same orchestration tool call twice.

    新手说明:
    前两次调用都发相同的 create_subagents 调用，第三次调用时确认重复派工被拦截。
    用来测试工具去重机制是否生效。
    """

    name = "fake_duplicate_subagent_delegation_backend"

    def __init__(self):
        self.calls = 0

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
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
    """LLM: fake model backend that verifies a final response is generated when max tool rounds are hit.

    新手说明:
    第一次调用时请求工具调用，第二次调用时确认已收到最大轮数提示并给出最终回答。
    用来测试工具轮数到顶时的收口逻辑。
    """

    name = "fake_max_tool_round_backend"

    def __init__(self):
        self.calls = 0

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                text='[TOOL_CALL]\n{"tool":"read_file","path":"notes.txt"}\n[/TOOL_CALL]',
                backend=self.name,
            )
        assert "已达到最大工具轮数限制" in prompt
        return ModelResponse(text="工具轮数到顶后已正常收口。", backend=self.name)


class DemoHandler(BaseHTTPRequestHandler):
    """LLM: minimal HTTP handler for local integration tests of fetch and http_request tools.

    新手说明:
    GET /page 返回 "demo page"；POST /echo 把请求体原样回显成 JSON。
    其他路径返回 404。日志输出被静默。
    """

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
    """LLM: start a local HTTP server on a random port for tool integration tests.

    新手说明:
    启动一个后台线程跑 DemoHandler，返回 server 对象。
    用完之后调用 server.shutdown() 和 server.server_close() 即可。
    """

    server = HTTPServer(("127.0.0.1", 0), DemoHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def make_tool_registry(workspace: Path) -> ToolRegistry:
    """LLM: create a ToolRegistry with standard test parameters.

    新手说明:
    多个测试都需要创建配置相同的 ToolRegistry，这里统一参数避免重复。
    向量搜索默认关闭，其余参数取安全保守值。
    """

    return ToolRegistry(
        workspace,
        max_chars=12000,
        max_entries=100,
        max_matches=50,
        web_max_chars=12000,
        http_timeout=30,
        catalog_limit=20,
        retrieval_limit=3,
        vector_search_enabled=False,
    )
