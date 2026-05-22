"""LLM: tests for tool loop, delegation, max rounds, allowlists, and security grants.

给人看的解释：
这个文件放和"工具循环流程"相关的测试：工具调用闭环、子代理派工和去重、最大轮数收口、
子代理 output.json 收口、工具授权过滤和安全能力授权。
"""

import tempfile
from pathlib import Path

from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams
from agent_py_agent.agent.agent_core._tool_loop_service import ToolLoopService
from agent_py_agent.agent.agent_core.tool_round_execution import ToolCallExecuteParams
from agent_py_agent.agent.backend import ModelResponse
from agent_py_agent.agent.config import AgentConfig
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.tools import ToolExecutionResult

from .backends import (
    BudgetedRepeatedReadBackend,
    DuplicateSubagentDelegationBackend,
    FakeReservedRecordWithoutToolBackend,
    FakeReservedRecordWithToolBackend,
    MaxToolRoundBackend,
    RepeatedDispatchBackend,
    RepeatedFakeReservedRecordBackend,
    StubbornToolAfterLimitBackend,
    SubagentDelegationBackend,
    ToolBoundarySpoofStreamingBackend,
    ToolCallingBackend,
    UnclosedWriteFileBackend,
)


# LLM: _OneShotHarnessAgent gives ToolLoopService only the attributes needed for private one-shot tests.
# 函数用途: 避免为一次性调度去重单测启动完整 SimpleAgent，同时保持工具执行路径真实。
class _OneShotHarnessAgent:
    def __init__(self, tools):
        self.tools = tools


# LLM: _BlockedScheduleTools simulates a semantic schedule block with a successful tool envelope.
# 函数用途: 返回 ok=True 但 JSON 里 blocked=true 的真实 schedule_child_subagents 输出形状。
class _BlockedScheduleTools:
    def __init__(self):
        self.calls = 0

    def execute_call(self, payload, *, allowed_tools=None, granted_capabilities=None, write_boundary=None):
        self.calls += 1
        return ToolExecutionResult(
            "schedule_child_subagents",
            True,
            '{"blocked": true, "reason": "duplicate_leaf_target:app.js", "created_run_ids": []}',
        )


# LLM: _UnlimitedRoundsBackend proves max_tool_rounds=0 disables only the round cap, not normal tool execution.
# 类用途: 测试专用后端；前两轮都请求读取文件，第三轮自行收口，用来验证 0 表示不限制。
class _UnlimitedRoundsBackend:
    name = "fake_unlimited_rounds_backend"

    def __init__(self):
        self.calls = 0

    def generate(self, prompt: str, on_chunk=None):
        self.calls += 1
        if self.calls <= 2:
            return ToolCallingBackend().generate(prompt, on_chunk=on_chunk)
        assert "已达到最大工具轮数限制" not in prompt
        return ModelResponse(text="无限轮数配置已正常收口", backend=self.name)


class _RepeatedMissingReadBackend:
    name = "fake_repeated_missing_read_backend"

    def __init__(self):
        self.calls = 0

    def generate(self, prompt: str, on_chunk=None):
        self.calls += 1
        if self.calls <= 3:
            return ModelResponse(
                text='[TOOL_CALL]\n{"tool":"read_file","path":"missing.txt"}\n[/TOOL_CALL]',
                backend=self.name,
            )
        assert "TOOL_REPEATED_EXACT_FAILURE" in prompt
        assert "change_tool_arguments_or_strategy" in prompt
        return ModelResponse(text="检测到重复失败，已停止原样重试。", backend=self.name)


# LLM: _EmptyAfterToolBackend reproduces provider empty final text after a successful tool call.
# 类用途: 第一次请求工具，第二次模拟 MiniMax/Anthropic-compatible 空流式响应，验证工具结果不被异常吞掉。
class _EmptyAfterToolBackend:
    name = "fake_empty_after_tool_backend"

    def __init__(self):
        self.calls = 0

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                text='[TOOL_CALL]\n{"tool":"read_file","path":"notes.txt"}\n[/TOOL_CALL]',
                backend=self.name,
            )
        raise RuntimeError("Anthropic-compatible 流式响应没有文本内容")


# LLM: _EmptyThenFinalAfterToolBackend verifies blank provider text is recoverable without losing tool facts.
# 类用途: 第一次请求工具、第二次空响应、第三次根据恢复上下文正常继续收口。
class _EmptyThenFinalAfterToolBackend:
    name = "fake_empty_then_final_after_tool_backend"

    def __init__(self):
        self.calls = 0

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                text='[TOOL_CALL]\n{"tool":"read_file","path":"notes.txt"}\n[/TOOL_CALL]',
                backend=self.name,
            )
        if self.calls == 2:
            raise RuntimeError("Anthropic-compatible 流式响应没有文本内容")
        assert "上一轮模型接口返回了空文本" in prompt
        assert "hello empty repair" in prompt
        return ModelResponse(text="已根据工具结果继续完成。", backend=self.name)


# LLM: _LongAppendPromptWindowBackend reproduces a productive runner whose live tool transcript grows every round.
# 类用途: 测试专用后端；连续 append 同一个产物，确认系统会压缩旧工具上下文而不是让 prompt 无限变大。
class _LongAppendPromptWindowBackend:
    name = "fake_long_append_prompt_window_backend"

    def __init__(self, rounds: int = 45):
        self.calls = 0
        self.rounds = rounds
        self.max_prompt_chars = 0

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.calls += 1
        self.max_prompt_chars = max(self.max_prompt_chars, len(prompt))
        if self.calls <= self.rounds:
            content = f"row-{self.calls}: " + ("x" * 900)
            return ModelResponse(
                text=(
                    "[TOOL_CALL]\n"
                    f'{{"tool":"append_file","path":"data/weekly_data.json","content":"{content}\\n"}}\n'
                    "[/TOOL_CALL]"
                ),
                backend=self.name,
            )
        return ModelResponse(text="连续写入后已正常收口。", backend=self.name)


# LLM: _PredelegationSourceReadBackend verifies root tool-loop guard output, not just helper calls.
# 类用途: 第一轮要求读取 data 正文；第二轮检查模型只收到派工前阻断提示，没有收到文件正文。
class _PredelegationSourceReadBackend:
    name = "fake_predelegation_source_read_backend"

    def __init__(self):
        self.calls = 0

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                text='[TOOL_CALL]\n{"tool":"read_file","path":"data/company_profile.md"}\n[/TOOL_CALL]',
                backend=self.name,
            )
        assert "predelegation_source_read_blocked" in prompt
        assert "SECRET COMPANY BODY" not in prompt
        return ModelResponse(text="已改为把资料路径交给小傻妞。", backend=self.name)


def test_tool_loop_and_prompt_transcript():
    """LLM: verify that a tool call round feeds tool output back to the model for a final answer.

    新手说明:
    模拟一次读文件工具调用，确认工具结果出现在后续 prompt 中，并且最终回答正确。
    """
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


# LLM: root predelegation guard must run inside the real SimpleAgent tool loop.
# 函数用途: 防止只测 helper、不测主循环接入；派工任务里 root 读 data 正文会被挡成 refs-first 提示。
def test_tool_loop_blocks_predelegation_source_body_read():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        (workspace / "data").mkdir()
        (workspace / "data" / "company_profile.md").write_text(
            "SECRET COMPANY BODY",
            encoding="utf-8",
        )
        cfg = AgentConfig(
            enable_tools=True,
            enable_subagents=True,
            memory_path="memory.jsonl",
            subagent_workspace="data/subagents",
        )
        agent = SimpleAgent(cfg, workspace)
        agent.backend = _PredelegationSourceReadBackend()

        result = agent.run(
            "请组织多层小傻妞协作完成。",
            save=False,
            task_attributes={"subagent_delegation": True},
        )

        assert result.response == "已改为把资料路径交给小傻妞。"
        assert result.tool_rounds == 1
        assert "predelegation_source_read_blocked" in result.prompt
        assert "SECRET COMPANY BODY" not in result.prompt


# LLM: provider empty final response after tools should not crash the whole run.
# 函数用途: 覆盖真实 E2E 中工具都跑完、最终总结模型空响应导致 CLI 异常退出的问题。
def test_tool_loop_falls_back_when_final_model_response_is_empty_after_tool():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        (workspace / "notes.txt").write_text("hello empty model fallback", encoding="utf-8")
        cfg = AgentConfig(enable_tools=True, memory_path="memory.jsonl")
        agent = SimpleAgent(cfg, workspace)
        agent.backend = _EmptyAfterToolBackend()

        result = agent.run("读取 notes 后总结", save=False, allowed_tools=["read_file"])

        assert "模型接口最终总结返回空文本" in result.response
        assert result.executed_tools == ["read_file"]
        assert agent.backend.calls == 3


# LLM: provider blank text after tools should get one continuation attempt before deterministic fallback.
# 函数用途: 覆盖真实 MiniMax 空文本后继续生成的恢复路径，避免读完材料就直接退出。
def test_tool_loop_retries_once_when_final_model_response_is_empty_after_tool():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        (workspace / "notes.txt").write_text("hello empty repair", encoding="utf-8")
        cfg = AgentConfig(enable_tools=True, memory_path="memory.jsonl")
        agent = SimpleAgent(cfg, workspace)
        agent.backend = _EmptyThenFinalAfterToolBackend()

        result = agent.run("读取 notes 后继续总结", save=False, allowed_tools=["read_file"])

        assert result.response == "已根据工具结果继续完成。"
        assert result.executed_tools == ["read_file"]
        assert agent.backend.calls == 3


# LLM: max_tool_rounds=0 should mean unlimited, while the model can still stop itself.
# 函数用途: 验证主代理单次请求的工具轮数上限为 0 时不会立刻触发限制提示。
def test_max_tool_rounds_zero_allows_multiple_tool_rounds():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        (workspace / "notes.txt").write_text("hello unlimited", encoding="utf-8")
        cfg = AgentConfig(enable_tools=True, memory_path="memory.jsonl", max_tool_rounds=0)
        agent = SimpleAgent(cfg, workspace)
        agent.backend = _UnlimitedRoundsBackend()

        result = agent.run("重复读取 notes 后收口", save=False)

        assert result.response == "无限轮数配置已正常收口"
        assert result.tool_rounds == 2
        assert agent.backend.calls == 3
        assert "已达到最大工具轮数限制" not in result.prompt


# LLM: complete tool JSON should execute even when the closing marker is missing.
# 函数用途: 覆盖真实 runner 漏写 [/TOOL_CALL] 但 write_file JSON 完整时，不应白跑 parse recovery。
def test_tool_loop_executes_complete_unclosed_write_file_tool_call():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        cfg = AgentConfig(enable_tools=True, memory_path="memory.jsonl")
        agent = SimpleAgent(cfg, workspace)
        agent.backend = UnclosedWriteFileBackend(workspace)

        result = agent.run("写 index.html", save=False, allowed_tools=["write_file"])

        assert result.response == "写入完成"
        assert result.tool_rounds == 1
        assert agent.backend.calls == 2
        assert (workspace / "index.html").read_text(encoding="utf-8") == (
            "<!doctype html><html><head><title>OK</title></head><body><main>ok</main></body></html>"
        )


# LLM: per-agent budget should block repeated tool calls only for the active run id.
# 函数用途: 验证工具循环里同一 run_id 超过预算后，返回自检提示并停止继续执行工具。
def test_tool_loop_enforces_per_agent_tool_budget_for_run_id():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        (workspace / "notes.txt").write_text("budget note", encoding="utf-8")
        cfg = AgentConfig(
            enable_tools=True,
            memory_path="memory.jsonl",
            max_tool_rounds=4,
            tool_agent_budget_window_seconds=600,
            tool_agent_budget_max_calls=1,
        )
        agent = SimpleAgent(cfg, workspace)
        agent.backend = BudgetedRepeatedReadBackend()

        result = agent.run("重复读文件后自检", save=False, allowed_tools=["read_file"], run_id="run-budget")

        assert result.response == "预算触发后已自检收口。"
        assert result.tool_rounds == 2
        assert agent.backend.calls == 3
        assert result.executed_tools == ["read_file"]


def test_tool_loop_blocks_repeated_identical_tool_failures_before_reexecuting():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        cfg = AgentConfig(enable_tools=True, memory_path="memory.jsonl", max_tool_rounds=6)
        agent = SimpleAgent(cfg, workspace)
        agent.backend = _RepeatedMissingReadBackend()

        result = agent.run(
            "重复读不存在文件时应改变策略",
            save=False,
            allowed_tools=["read_file"],
            run_id="run-tool-guard",
            task_attributes={"tool_guard_exact_failure_block_after": 2},
        )

        assert result.response == "检测到重复失败，已停止原样重试。"
        assert result.tool_rounds == 3
        assert agent.backend.calls == 4
        assert result.executed_tools == []


# LLM: long-running writers should keep working without making every old tool record part of the live prompt.
# 函数用途: 覆盖真实 Task18 里单个数据 worker prompt 膨胀到数十万字符的问题。
def test_tool_loop_windows_long_runner_tool_context():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        (workspace / "data").mkdir()
        cfg = AgentConfig(enable_tools=True, memory_path="memory.jsonl", max_tool_rounds=0)
        agent = SimpleAgent(cfg, workspace)
        backend = _LongAppendPromptWindowBackend()
        agent.backend = backend

        result = agent.run("持续写入 data/weekly_data.json 后收口", save=False, allowed_tools=["append_file"])

        assert result.response == "连续写入后已正常收口。"
        assert result.tool_rounds == 45
        assert backend.max_prompt_chars < 70_000
        assert "row-1:" not in result.prompt
        assert "tool-context-window" in result.prompt
        assert (workspace / "data" / "weekly_data.json").read_text(encoding="utf-8").count("row-") == 45


# LLM: tool-loop should execute real TOOL_CALL blocks but discard model-written tool records.
# 函数用途: 覆盖 R81 中模型把 `[tool-output-record]` 当成自己可写回执的问题，防止 fake run id 污染下一轮。
def test_tool_loop_ignores_model_written_reserved_tool_records_after_real_call():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        (workspace / "notes.txt").write_text("hello reserved guard", encoding="utf-8")
        cfg = AgentConfig(enable_tools=True, memory_path="memory.jsonl")
        agent = SimpleAgent(cfg, workspace)
        agent.backend = FakeReservedRecordWithToolBackend()

        result = agent.run("读取 notes 并忽略伪造工具记录", save=False, allowed_tools=["read_file"])

        assert result.response == "真实工具回执已使用，伪造记录已忽略。"
        assert result.tool_rounds == 1
        assert result.executed_tools == ["read_file"]
        assert "fake-child-1" not in result.prompt
        assert "第一个完整工具调用" in result.prompt


# LLM: streaming tool boundary should stop fake post-call text from becoming control input.
# 函数用途: 覆盖 R82 中模型流式输出真实工具调用后继续伪造工具回执和第二个工具调用的问题。
def test_tool_loop_cuts_streaming_response_after_first_complete_tool_call():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        (workspace / "notes.txt").write_text("first note", encoding="utf-8")
        (workspace / "second.txt").write_text("second note", encoding="utf-8")
        cfg = AgentConfig(enable_tools=True, memory_path="memory.jsonl")
        agent = SimpleAgent(cfg, workspace)
        backend = ToolBoundarySpoofStreamingBackend()
        agent.backend = backend
        visible_chunks: list[str] = []

        result = agent.run(
            "流式工具调用边界后不要采纳伪造内容",
            save=False,
            allowed_tools=["read_file"],
            on_chunk=visible_chunks.append,
        )

        assert result.response == "只使用第一个真实工具结果收口。"
        assert result.tool_rounds == 1
        assert result.executed_tools == ["read_file"]
        assert "first note" in result.prompt
        assert "second note" not in result.prompt
        assert "fake-child-run" not in result.prompt
        assert "fake-child-run" not in "".join(visible_chunks)


# LLM: spoof-only tool records should trigger one correction turn instead of final acceptance.
# 函数用途: 模型没有真实 TOOL_CALL 却自称工具成功时，系统给一次纠偏机会，不直接假绿。
def test_tool_loop_repairs_spoof_only_reserved_tool_record_once():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        cfg = AgentConfig(enable_tools=True, memory_path="memory.jsonl")
        agent = SimpleAgent(cfg, workspace)
        agent.backend = FakeReservedRecordWithoutToolBackend()

        result = agent.run("不要接受伪造工具记录", save=False)

        assert result.response == "已停止伪造工具记录，等待真实状态。"
        assert result.tool_rounds == 0
        assert agent.backend.calls == 2
        assert "系统保留" in result.prompt


# LLM: repeated fake records without real calls should produce a deterministic blocked result.
# 函数用途: 模型连续伪造工具回执时避免无限循环，并明确告诉上层当前结果不可信。
def test_tool_loop_blocks_repeated_spoof_only_reserved_tool_records():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        cfg = AgentConfig(enable_tools=True, memory_path="memory.jsonl")
        agent = SimpleAgent(cfg, workspace)
        agent.backend = RepeatedFakeReservedRecordBackend()

        result = agent.run("连续伪造工具记录应被阻断", save=False)

        assert "系统已阻止本轮结果" in result.response
        assert "不能把这次回复视为完成" in result.response
        assert result.tool_rounds == 0
        assert agent.backend.calls == 2


def test_agent_can_delegate_to_subagents_from_tool_call():
    """LLM: verify that a create_subagents tool call creates tasks and dispatch control works.

    新手说明:
    测试子代理创建、任务板查看、干跑调度和执行调度拦截。
    """
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
        blocked_dispatch = agent.tools.execute_call({"tool": "dispatch_subagents", "execute_runners": True, "apply": False})

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


def test_create_subagents_rejects_external_write_target_before_task_creation():
    """LLM: write-capable subagents should fail early for absolute paths outside workspace."""
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        external_dir = workspace.parent / "external-target"
        cfg = AgentConfig(
            enable_tools=True,
            memory_path="memory.jsonl",
            subagent_workspace="subs",
            max_subagents=3,
        )
        agent = SimpleAgent(cfg, workspace)

        result = agent.tools.execute_call(
            {
                "tool": "create_subagents",
                "goal": "创建一个 txt 文件",
                "allowed_tools": ["read_file", "write_file"],
                "extra_write_roots": [str(external_dir)],
            }
        )

        assert not result.ok
        assert "工作区外" in result.output
        assert agent.subagents.list_runs() == []


def test_repeated_orchestration_tool_call_is_not_executed_twice():
    """LLM: verify that identical consecutive orchestration tool calls are deduplicated.

    新手说明:
    连续两次 create_subagents 只应执行一次，第二次被拦截。
    """
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


# LLM: blocked hierarchy schedule attempts must remain retryable after the parent narrows scope.
# 函数用途: 复现 R38 中 schedule_child_subagents 返回 blocked=true 后，被一次性调用去重挡住修正重试的问题。
def test_blocked_schedule_result_does_not_consume_one_shot_key():
    params = ToolLoopExecuteParams(
        user_prompt="",
        memories=[],
        runtime_injections=[],
        prompt_files=[],
        tool_catalog_section="",
        tool_recommendations_section="",
        tool_context=[],
        effective_on_chunk=None,
        allowed_tools=None,
        granted_capabilities=None,
        write_boundary=None,
        task_attributes=None,
        request_id="",
        run_id="",
        task_id="",
        one_shot_tool_calls=set(),
        executed_tools=[],
        archive_tool_calls=[],
    )
    agent = _OneShotHarnessAgent(_BlockedScheduleTools())
    service = ToolLoopService(agent)
    payload = {"tool": "schedule_child_subagents", "apply": True, "children": [{"goal": "cart"}]}

    first = service._execute_one_tool_call(ToolCallExecuteParams(params, 1, 1, payload))
    second = service._execute_one_tool_call(ToolCallExecuteParams(params, 2, 1, payload))

    assert first.ok is True
    assert second.ok is True
    assert agent.tools.calls == 2
    assert "阻止重复执行" not in second.output


def test_repeated_dispatch_is_allowed_for_parent_progress_loops():
    """LLM: dispatch_subagents may need repeated identical calls when rate limits leave pending children."""
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        cfg = AgentConfig(
            enable_tools=True,
            memory_path="memory.jsonl",
            subagent_workspace="subs",
            max_tool_rounds=4,
        )
        agent = SimpleAgent(cfg, workspace)
        agent.backend = RepeatedDispatchBackend()

        result = agent.run("继续推进父节点调度", save=False)

        assert result.response == "重复 dispatch 已允许继续推进。"
        assert result.tool_rounds == 2
        assert "阻止重复执行" not in result.prompt


def test_max_tool_rounds_generates_final_response():
    """LLM: verify that hitting max_tool_rounds still produces a final model response.

    新手说明:
    把 max_tool_rounds 设为 1，模型应该收到轮数限制提示并给出回答。
    """
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        (workspace / "notes.txt").write_text("hello", encoding="utf-8")
        cfg = AgentConfig(
            enable_tools=True,
            memory_path="memory.jsonl",
            max_tool_rounds=1,
        )
        agent = SimpleAgent(cfg, workspace)
        agent.backend = MaxToolRoundBackend()

        result = agent.run("读取 notes", save=False)

        assert result.response == "工具轮数到顶后已正常收口。"
        assert result.tool_rounds == 1
        assert agent.backend.calls == 3


# LLM: tool loop must not return a fresh TOOL_CALL as the final answer after max rounds.
# 函数用途: 模拟模型不听收口提示仍继续要工具，验证系统返回确定性停止说明而不是继续误导上层。
def test_max_tool_rounds_hard_stops_when_model_still_requests_tools():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        cfg = AgentConfig(enable_tools=True, memory_path="memory.jsonl", max_tool_rounds=1)
        agent = SimpleAgent(cfg, workspace)
        agent.backend = StubbornToolAfterLimitBackend()

        result = agent.run("读取 notes", save=False)

        assert "已达到最大工具轮数限制" in result.response
        assert "后续工具请求不会被执行" in result.response
        assert "[TOOL_CALL]" not in result.response
        assert result.tool_rounds == 1
        assert agent.backend.calls == 3
