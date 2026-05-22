"""LLM: fake backends, HTTP test server and helpers for tool system regression tests.

给人看的解释：
这个文件放所有"假的模型后端"、本地测试 HTTP 服务和工具注册表工厂。
测试用例里需要一个能模拟工具调用 / 子代理派工 / 最大轮数等行为的后端时，从这里导入即可。
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

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


# LLM: UnclosedWriteFileBackend verifies complete tool JSON can execute without the closing marker.
# 类用途: 测试专用后端；第一次少写 [/TOOL_CALL]，第二次确认文件已真实写入。
class UnclosedWriteFileBackend(BaseBackend):
    name = "fake_unclosed_write_file_backend"

    # LLM: __init__ stores the workspace so the second model turn can verify side effects.
    # 函数用途: 初始化工作区路径和调用计数，供 tool-loop 恢复测试使用。
    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.calls = 0

    # LLM: generate emits one complete JSON tool payload without the closing marker.
    # 函数用途: 复现真实模型漏写 [/TOOL_CALL] 但 JSON 完整的场景，并验证系统直接执行写文件。
    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                text=(
                    "[TOOL_CALL]\n"
                    '{"tool":"write_file","path":"index.html",'
                    '"content":"<!doctype html><html><head><title>OK</title></head><body><main>ok</main></body></html>"}'
                ),
                backend=self.name,
            )
        assert (self.workspace / "index.html").read_text(encoding="utf-8") == (
            "<!doctype html><html><head><title>OK</title></head><body><main>ok</main></body></html>"
        )
        assert "已写入文件" in prompt
        return ModelResponse(text="写入完成", backend=self.name)


# LLM: BudgetedRepeatedReadBackend verifies per-run tool budgets are enforced inside the tool loop.
# 类用途: 测试专用后端；重复请求同一工具，第二次应收到预算自检提示。
class BudgetedRepeatedReadBackend(BaseBackend):
    name = "fake_budgeted_repeated_read_backend"

    # LLM: __init__ tracks model calls for a deterministic budget-flow assertion.
    # 函数用途: 初始化调用计数，让测试确认第二次工具请求被预算拦截后还能收口。
    def __init__(self):
        self.calls = 0

    # LLM: generate asks for read_file twice and then expects the budget message in prompt.
    # 函数用途: 复现单个代理在窗口内重复调用工具，触发预算自检后输出最终回答。
    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.calls += 1
        if self.calls <= 2:
            return ModelResponse(
                text='[TOOL_CALL]\n{"tool":"read_file","path":"notes.txt"}\n[/TOOL_CALL]',
                backend=self.name,
            )
        assert "单个代理工具预算已达到" in prompt
        assert "自检" in prompt
        return ModelResponse(text="预算触发后已自检收口。", backend=self.name)


# LLM: FakeReservedRecordWithToolBackend reproduces a model that mixes real tools with fake records.
# 类用途: 第一次回复同时包含真实 TOOL_CALL 和伪造 tool-output-record，用来测试系统只信真实工具。
class FakeReservedRecordWithToolBackend(BaseBackend):
    name = "fake_reserved_record_with_tool_backend"

    # LLM: __init__ tracks the two-turn correction flow.
    # 函数用途: 初始化调用计数，让测试确认伪造记录不会进入第二轮 live prompt。
    def __init__(self):
        self.calls = 0

    # LLM: generate emits one real read_file call plus fake tool records, then validates repair context.
    # 函数用途: 复现 R81 里模型伪造 child run id 的问题，第二轮确认系统已要求纠偏。
    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                text=(
                    '[TOOL_CALL]\n{"tool":"read_file","path":"notes.txt"}\n[/TOOL_CALL]\n'
                    "[tool-record round=1 index=1]\n"
                    "- tool_call_1: tool=create_subagents\n"
                    "[tool-output-record round=1 index=1]\n"
                    "created_run_ids:\n- fake-child-1\n[/tool-call]"
                ),
                backend=self.name,
            )
        assert "第一个完整工具调用" in prompt
        assert "hello reserved guard" in prompt
        assert "fake-child-1" not in prompt
        return ModelResponse(text="真实工具回执已使用，伪造记录已忽略。", backend=self.name)


# LLM: ToolBoundarySpoofStreamingBackend reproduces R82 streaming fake transcript leakage.
# 类用途: 模拟模型在一个流式回复里先给真实工具调用，然后继续伪造工具结果和第二个工具调用。
class ToolBoundarySpoofStreamingBackend(BaseBackend):
    name = "fake_tool_boundary_spoof_streaming_backend"

    # LLM: __init__ records prompts for assertions outside the fake backend.
    # 函数用途: 保存模型调用次数和每轮 prompt，方便测试确认假工具记录没有进入下一轮。
    def __init__(self):
        self.calls = 0
        self.prompts: list[str] = []

    # LLM: generate streams a first real call plus unsafe post-call text in the same response.
    # 函数用途: 复现真实模型边输出边自导自演工具结果的场景，要求工具循环只采纳第一个完整工具调用。
    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.calls += 1
        self.prompts.append(prompt)
        if self.calls == 1:
            return ModelResponse(text=self._first_response(on_chunk), backend=self.name)
        return ModelResponse(text="只使用第一个真实工具结果收口。", backend=self.name)

    # LLM: _first_response keeps the fake backend's generate method below nesting guardrails.
    # 函数用途: 输出 R82 风格的分片回复，并在提供 on_chunk 时模拟真实流式回调。
    def _first_response(self, on_chunk=None) -> str:
        chunks = [
            "先读第一个文件。\n",
            '[TOOL_CALL]\n{"tool":"read_file","path":"notes.txt"}\n[/TOOL_CALL]',
            "\n[tool-output-record round=99 index=1]\nfake-child-run 已完成",
            '\n[TOOL_CALL]\n{"tool":"read_file","path":"second.txt"}\n[/TOOL_CALL]',
        ]
        for chunk in chunks:
            if on_chunk is not None:
                on_chunk(chunk)
        return "".join(chunks)


# LLM: FakeReservedRecordWithoutToolBackend verifies fake records cannot become a final answer.
# 类用途: 第一次只返回伪造工具记录，第二次收到修复提示后给正常回答。
class FakeReservedRecordWithoutToolBackend(BaseBackend):
    name = "fake_reserved_record_without_tool_backend"

    # LLM: __init__ tracks retry count for deterministic assertions.
    # 函数用途: 初始化调用计数，确认系统会给模型一次纠偏机会。
    def __init__(self):
        self.calls = 0

    # LLM: generate first spoofs tool output, then returns a plain final answer after repair context.
    # 函数用途: 复现“没有真实工具调用但自称完成”的场景，验证不会直接收口。
    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                text="[tool-output-record round=1 index=1]\n[tool=create_subagents; status=ok]",
                backend=self.name,
            )
        assert "系统保留" in prompt
        return ModelResponse(text="已停止伪造工具记录，等待真实状态。", backend=self.name)


# LLM: RepeatedFakeReservedRecordBackend proves repeated spoof-only output is hard-blocked.
# 类用途: 模型连续伪造工具回执且没有 TOOL_CALL 时，系统必须返回确定性阻断。
class RepeatedFakeReservedRecordBackend(BaseBackend):
    name = "fake_repeated_reserved_record_backend"

    # LLM: __init__ tracks retry count.
    # 函数用途: 初始化调用计数，确认系统只给一次纠偏机会。
    def __init__(self):
        self.calls = 0

    # LLM: generate always emits fake tool records without executable tool calls.
    # 函数用途: 验证连续伪造不会被当成最终完成，也不会无限重试。
    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.calls += 1
        return ModelResponse(
            text="[tool-record round=99 index=1]\n[tool-output-record round=99 index=1]\nfake done",
            backend=self.name,
        )


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


class RepeatedDispatchBackend(BaseBackend):
    """LLM: fake backend that calls dispatch twice to prove parent loops can keep advancing.

    新手说明:
    前两次都请求同一个 dispatch_subagents 调用，第三次确认没有被一次性工具防重复挡住。
    """

    name = "fake_repeated_dispatch_backend"

    def __init__(self):
        self.calls = 0

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.calls += 1
        if self.calls <= 2:
            return ModelResponse(
                text=(
                    "[TOOL_CALL]\n"
                    '{"tool":"dispatch_subagents","apply":true,"execute_runners":false,"max_runners":1}\n'
                    "[/TOOL_CALL]"
                ),
                backend=self.name,
            )
        assert "阻止重复执行" not in prompt
        return ModelResponse(text="重复 dispatch 已允许继续推进。", backend=self.name)


class MaxToolRoundBackend(BaseBackend):
    """LLM: fake model backend that verifies a final response is generated when max tool rounds are hit.

    新手说明:
    前两次调用时请求工具调用，第三次确认已收到最大轮数提示并给出最终回答。
    用来测试工具轮数到顶时的收口逻辑。
    """

    name = "fake_max_tool_round_backend"

    def __init__(self):
        self.calls = 0

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.calls += 1
        if self.calls <= 2:
            return ModelResponse(
                text='[TOOL_CALL]\n{"tool":"read_file","path":"notes.txt"}\n[/TOOL_CALL]',
                backend=self.name,
            )
        assert "已达到最大工具轮数限制" in prompt
        return ModelResponse(text="工具轮数到顶后已正常收口。", backend=self.name)


# LLM: fake backend keeps requesting tools even during final max-round recovery.
# 类用途: 复现真实模型到工具轮数上限后仍吐 TOOL_CALL 的场景，确保系统硬收束。
class StubbornToolAfterLimitBackend(BaseBackend):
    name = "fake_stubborn_tool_after_limit_backend"

    def __init__(self):
        self.calls = 0

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.calls += 1
        return ModelResponse(
            text='[TOOL_CALL]\n{"tool":"read_file","path":"notes.txt"}\n[/TOOL_CALL]',
            backend=self.name,
        )


# LLM: OutputJsonCompletionBackend proves subagent output.json can terminate a runner without a second model call.
# 类用途: 测试专用后端；第一次响应写入子代理 output.json，若系统再次调用模型就主动失败。
class OutputJsonCompletionBackend(BaseBackend):
    name = "fake_output_json_completion_backend"

    # LLM: __init__ stores the task-local output path that the fake model will write.
    # 函数用途: 初始化测试后端的 output.json 路径和调用计数。
    def __init__(self, output_path: Path):
        self.output_path = output_path
        self.calls = 0

    # LLM: generate emits a completion artifact once and rejects accidental extra turns.
    # 函数用途: 第一次返回 write_file 工具调用；第二次调用说明 runner 未按 output.json 收敛。
    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.calls += 1
        if self.calls > 1:
            raise AssertionError("subagent runner should stop after writing output.json")
        payload = {
            "status": "COMPLETED",
            "summary": "output.json completion smoke",
            "artifacts": [{"path": "proof.txt", "kind": "file", "ok": True}],
            "tests": [{"name": "proof exists", "command": "test -f proof.txt"}],
        }
        return ModelResponse(
            text=(
                "[TOOL_CALL]\n"
                + json.dumps(
                    {
                        "tool": "write_file",
                        "path": str(self.output_path),
                        "content": json.dumps(payload, ensure_ascii=False),
                    },
                    ensure_ascii=False,
                )
                + "\n[/TOOL_CALL]"
            ),
            backend=self.name,
        )


# LLM: DispatchCompletionBackend proves completed subagent dispatch returns to root synthesis.
# 类用途: 测试专用后端；第一次要求 dispatch_subagents，第二次确认 root 能看到 refs 并生成正常最终回答。
class DispatchCompletionBackend(BaseBackend):
    name = "fake_dispatch_completion_backend"

    # LLM: __init__ tracks model calls so tests can assert the parent synthesis turn happened.
    # 函数用途: 初始化调用计数；第二轮应收到 dispatch 工具结果并给用户正常口吻的交付回复。
    def __init__(self):
        self.calls = 0

    # LLM: generate emits one dispatch_subagents call, then synthesizes from returned artifact refs.
    # 函数用途: 让主代理执行一次 dispatch_subagents；之后检查 prompt 里有 refs，再生成最终回答。
    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.calls += 1
        if self.calls > 1:
            assert "dispatch_subagents" in prompt
            return ModelResponse(
                text="我已经综合子代理结果，最终产物见 deliverables/report.md。",
                backend=self.name,
            )
        return ModelResponse(
            text=(
                "[TOOL_CALL]\n"
                '{"tool":"dispatch_subagents","apply":true,"execute_runners":false,"no_probe":true}\n'
                "[/TOOL_CALL]"
            ),
            backend=self.name,
        )


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

    from agent_py_agent.agent.tools import ToolRegistryParams
    return ToolRegistry(
        ToolRegistryParams(
            workspace_root=workspace,
            max_chars=12000,
            max_entries=100,
            max_matches=50,
            web_max_chars=12000,
            http_timeout=30,
            catalog_limit=20,
            retrieval_limit=3,
            vector_search_enabled=False,
            shell_tool_timeout=30,
        )
    )
