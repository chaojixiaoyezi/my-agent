"""LLM: tests for tool loop, delegation, max rounds, allowlists, and security grants.

给人看的解释：
这个文件放和"工具循环流程"相关的测试：工具调用闭环、子代理派工和去重、最大轮数收口、
子代理 output.json 收口、工具授权过滤和安全能力授权。
"""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams
from agent_py_agent.agent.agent_core._tool_loop_service import ToolLoopService
from agent_py_agent.agent.agent_core.tool_loop.round_execution import (
    ToolCallExecuteParams,
    ToolCallRecordParams,
    ToolRoundExecutionRequest,
    execute_tool_round,
)
from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.backends.errors import ProviderResponseError
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.tooling import ToolExecutionResult

from .backends import (
    BudgetedRepeatedReadBackend,
    DelayedSecondToolStreamingBackend,
    DuplicateSubagentDelegationBackend,
    FakeProtectedMarkerWithoutToolBackend,
    FakeProtectedMarkerWithToolBackend,
    MaxToolRoundBackend,
    RepeatedDispatchBackend,
    RepeatedFakeProtectedMarkerBackend,
    StubbornToolAfterLimitBackend,
    SubagentDelegationBackend,
    ToolBoundarySpoofStreamingBackend,
    ToolCallingBackend,
    UnclosedWriteFileBackend,
)


def _assert_verified_response(
    result,
    model_text: str,
    expected_operations: dict[tuple[str, str], int],
) -> None:
    """Keep the model-authored reply while checking the machine-authored operation proof."""

    assert result.response.startswith(f"{model_text}\n\n操作核验（以程序记录为准）：")
    verification = result.operation_verification
    assert verification["schema"] == "operation_verification.v1"
    actual: dict[tuple[str, str], int] = {}
    for operation in verification["operations"]:
        key = (operation["tool"], operation["verification_status"])
        actual[key] = actual.get(key, 0) + 1
    assert actual == expected_operations


class _OneShotHarnessAgent:
    def __init__(self, tools):
        self.tools = tools


class _BlockedScheduleTools:
    def __init__(self):
        self.calls = 0

    def execute_call(
        self,
        payload,
        *,
        allowed_tools=None,
        write_boundary=None,
        runtime_snapshot=None,
    ):
        _ = (payload, allowed_tools, write_boundary, runtime_snapshot)
        self.calls += 1
        return ToolExecutionResult(
            "schedule_child_subagents",
            True,
            '{"blocked": true, "reason": "duplicate_leaf_target:app.js", "created_run_ids": []}',
        )


class _SuccessfulCreateTools:
    def __init__(self):
        self.calls = 0

    def execute_call(
        self,
        payload,
        *,
        allowed_tools=None,
        write_boundary=None,
        runtime_snapshot=None,
    ):
        _ = (payload, allowed_tools, write_boundary, runtime_snapshot)
        self.calls += 1
        return ToolExecutionResult("create_subagents", True, '{"created": 1}')


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


class _GatewayNaturalDispatchReplyBackend:
    name = "fake_gateway_natural_dispatch_reply"

    def __init__(self):
        self.calls = 0

    def generate(self, prompt: str, on_chunk=None):
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                text=(
                    "[TOOL_CALL]\n"
                    '{"tool":"create_subagents","goal":"分别整理两部分",'
                    '"items":[{"goal":"整理第一部分"},{"goal":"整理第二部分"}],'
                    '"defer_start":true,"tool_preset":"read_only"}'
                    "\n[/TOOL_CALL]"
                ),
                backend=self.name,
            )
        if self.calls == 2:
            assert "[natural-user-reply]" not in prompt
            assert "create_subagents" in prompt
            return ModelResponse(
                text=(
                    "[TOOL_CALL]\n"
                    '{"tool":"wait","seconds":120,"reason":"稍后继续整理并汇总两个部分"}'
                    "\n[/TOOL_CALL]"
                ),
                backend=self.name,
            )
        assert "[natural-user-reply]" in prompt
        assert '"wait_registered": true' in prompt
        assert '"task_continues_without_more_user_input": true' in prompt
        assert '"current_user_request": "请把两个部分分别整理后汇总"' in prompt
        assert '"delegated_work": {' in prompt
        assert '"status_counts": {' in prompt
        assert '"total": 2' in prompt
        assert '"active"' not in prompt
        assert '"finished"' not in prompt
        assert '"issues"' not in prompt
        assert "# Tool Catalog" not in prompt
        return ModelResponse(text="我先把两部分拆开整理，汇总好后一起给你。", backend=self.name)


class _RepeatedMissingReadBackend:
    name = "fake_repeated_missing_read_backend"

    def __init__(self):
        self.calls = 0

    def generate(self, prompt: str, on_chunk=None):
        self.calls += 1
        if self.calls <= 4:
            if self.calls == 4:
                assert "tool-loop-guardrail-hint" in prompt
            return ModelResponse(
                text='[TOOL_CALL]\n{"tool":"read_file","path":"missing.txt"}\n[/TOOL_CALL]',
                backend=self.name,
            )
        assert "TOOL_GUARDRAIL_REPEAT_FAILURE_BLOCKED" in prompt
        assert "这一次相同工具调用未执行" in prompt
        return ModelResponse(text="已看到提示，改用其他路径继续推进。", backend=self.name)


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
        raise ProviderResponseError("Anthropic-compatible 流式响应没有文本内容", error_code="MODEL_EMPTY_RESPONSE")


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
            raise ProviderResponseError("Anthropic-compatible 流式响应没有文本内容", error_code="MODEL_EMPTY_RESPONSE")
        assert "上一轮模型接口返回了空文本" in prompt
        assert "hello empty repair" in prompt
        return ModelResponse(text="已根据工具结果继续完成。", backend=self.name)


class _SeparatedEmptyResponsesBackend:
    name = "fake_separated_empty_responses_backend"

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
            raise ProviderResponseError("first empty response", error_code="MODEL_EMPTY_RESPONSE")
        if self.calls == 3:
            assert "上一轮模型接口返回了空文本" in prompt
            return ModelResponse(
                text=(
                    '[TOOL_CALL]\n{"tool":"write_file","path":"summary.txt",'
                    '"content":"first recovery succeeded"}\n[/TOOL_CALL]'
                ),
                backend=self.name,
            )
        if self.calls == 4:
            raise ProviderResponseError("second isolated empty response", error_code="MODEL_EMPTY_RESPONSE")
        assert "上一轮模型接口返回了空文本" in prompt
        return ModelResponse(text="两次独立空响应后仍完成。", backend=self.name)


class _IncompleteThenFinalAfterToolBackend:
    name = "fake_incomplete_then_final_after_tool_backend"

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
            raise ProviderResponseError(
                "anthropic_compatible 模型响应未完成（stop_reason=max_tokens）",
                error_code="MODEL_INCOMPLETE_RESPONSE",
                details={
                    "stop_reason": "max_tokens",
                    "partial_text_chars": 16314,
                    "tool_use_blocks": 0,
                },
            )
        assert "[tool-system:model-incomplete-response]" in prompt
        assert "stop_reason: max_tokens" in prompt
        assert "discarded_partial_text_chars: 16314" in prompt
        assert "hello incomplete repair" in prompt
        return ModelResponse(text="已从已完成工具结果继续收口。", backend=self.name)


class _IncompleteWithoutToolBackend:
    name = "fake_incomplete_without_tool_backend"

    def __init__(self):
        self.calls = 0

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.calls += 1
        raise ProviderResponseError(
            "anthropic_compatible 模型响应未完成（stop_reason=max_tokens）",
            error_code="MODEL_INCOMPLETE_RESPONSE",
            details={"stop_reason": "max_tokens", "partial_text_chars": "invalid"},
        )


class _RepeatedIncompleteAfterToolBackend:
    name = "fake_repeated_incomplete_after_tool_backend"

    def __init__(self):
        self.calls = 0

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                text='[TOOL_CALL]\n{"tool":"read_file","path":"notes.txt"}\n[/TOOL_CALL]',
                backend=self.name,
            )
        raise ProviderResponseError(
            "anthropic_compatible 模型响应未完成（stop_reason=max_tokens）",
            error_code="MODEL_INCOMPLETE_RESPONSE",
            details={"stop_reason": "max_tokens", "partial_text_chars": 4096},
        )


class _LongAppendPromptWindowBackend:
    name = "fake_long_append_prompt_window_backend"

    def __init__(self, rounds: int = 45):
        self.calls = 0
        self.rounds = rounds
        # 工具 schema 目录本身已超过旧的 32K 人工窗口；本测试用 45 轮累计写入
        # 验证长工具循环保持有界，不依赖不存在的短输出 artifact 提示来制造压力。
        self.context_window_tokens = 40_000
        self.max_prompt_chars = 0
        self.rows: list[str] = []

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.calls += 1
        self.max_prompt_chars = max(self.max_prompt_chars, len(prompt))
        if self.calls <= self.rounds:
            self.rows.append(f"row-{self.calls}: " + ("x" * 900))
            content = "\\n".join(self.rows) + "\\n"
            return ModelResponse(
                text=(
                    "[TOOL_CALL]\n"
                    f'{{"tool":"write_file","path":"data/weekly_data.json","content":"{content}"}}\n'
                    "[/TOOL_CALL]"
                ),
                backend=self.name,
            )
        return ModelResponse(text="连续写入后已正常收口。", backend=self.name)


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


def test_tool_loop_reuses_one_workspace_context_snapshot_across_model_rounds(tmp_path):
    (tmp_path / "notes.txt").write_text("hello tool world", encoding="utf-8")
    agent = SimpleAgent(AgentConfig(enable_tools=True, memory_path="memory.jsonl"), tmp_path)
    backend = ToolCallingBackend()
    prompts: list[str] = []
    generate = backend.generate

    def recording_generate(prompt: str, on_chunk=None):
        prompts.append(prompt)
        return generate(prompt, on_chunk=on_chunk)

    backend.generate = recording_generate
    agent.backend = backend
    agent.prompts.snapshot_workspace_context = MagicMock(
        return_value="- primary_workspace_root: /turn-snapshot\n- current_local_time: frozen"
    )

    result = agent.run("读取 notes.txt 并总结", save=False)

    assert result.response == "工具执行完成"
    assert len(prompts) == 2
    assert all("current_local_time: frozen" in prompt for prompt in prompts)
    agent.prompts.snapshot_workspace_context.assert_called_once_with()


def test_tool_round_streams_tool_progress_chunks():
    """工具执行期间应向 chat/gateway chunk 流写入轻量进度，避免前台看起来卡死。"""
    chunks: list[str] = []
    records: list[ToolCallRecordParams] = []
    params = ToolLoopExecuteParams(
        user_prompt="执行命令",
        memories=[],
        runtime_injections=[],
        prompt_files=[],
        tool_catalog_section="",
        tool_recommendations_section="",
        tool_context=[],
        effective_on_chunk=chunks.append,
        allowed_tools=None,
        write_boundary=None,
        task_attributes=None,
        request_id="run-progress",
        run_id="run-progress",
        task_id="run-progress",
        one_shot_tool_calls=set(),
        executed_tools=[],
        archive_tool_calls=[],
    )

    execute_tool_round(
        ToolRoundExecutionRequest(
            _OneShotHarnessAgent(None),
            params,
            1,
            ModelResponse(
                text='[TOOL_CALL]\n{"tool":"run_command","command":"echo hello"}\n[/TOOL_CALL]',
                backend="test",
            ),
            [{"tool": "run_command", "command": "echo hello"}],
            lambda _request: ToolExecutionResult("run_command", True, "hello\n"),
            records.append,
        )
    )

    rendered = "".join(chunks)
    assert "[工具] round=1 #1 run_command 开始: echo hello" in rendered
    assert "[工具] round=1 #1 run_command 完成" in rendered
    assert records[0].result.ok is True


def test_plain_parallel_project_prompt_recommends_tool_search(tmp_path):
    """大白话里的并行任务先命中按需搜索，不在首轮直接暴露编排工具。"""
    agent = SimpleAgent(AgentConfig(enable_tools=True, memory_path="memory.jsonl"), tmp_path)

    _catalog, recommendations = agent.tools.render_catalog_section(), agent.tools.render_recommended_tools_section(
        "请让子代理分别去看不同项目，最后你汇总。"
    )

    assert "tool_search" in recommendations
    assert "create_subagents" not in recommendations


def test_runtime_tool_sections_use_user_prompt_for_orchestration_recommendations(tmp_path):
    """运行时推荐工具必须看用户原始任务，不能退化成空 query。"""
    from agent_py_agent.agent.agent_core.runtime.loop_support import (
        ToolSectionsRequest,
        _resolve_tool_sections,
    )

    agent = SimpleAgent(AgentConfig(enable_tools=True, memory_path="memory.jsonl"), tmp_path)

    _catalog, recommendations = _resolve_tool_sections(ToolSectionsRequest(
        agent=agent,
        user_prompt="请让子代理分别去看不同项目，最后你汇总。",
        allowed_tools=None,
        runtime_snapshot=agent.tools.runtime_snapshot(),
    ))

    assert "tool_search" in recommendations
    assert "create_subagents" not in recommendations


def test_runtime_tool_sections_only_collapse_catalog_when_native_is_effective(tmp_path):
    from types import SimpleNamespace

    from agent_py_agent.agent.agent_core.runtime.loop_support import (
        ToolSectionsRequest,
        _resolve_tool_sections,
    )

    agent = SimpleAgent(
        AgentConfig(enable_tools=True, tool_protocol="native", memory_path="memory.jsonl"),
        tmp_path,
    )
    snapshot = agent.tools.runtime_snapshot()

    # Echo cannot consume provider-native schemas, so configured "native" must
    # retain the complete text catalog.
    text_catalog, _ = _resolve_tool_sections(
        ToolSectionsRequest(agent, "继续编码", None, snapshot)
    )
    assert "read_file" in text_catalog

    agent.backend = SimpleNamespace(name="anthropic_compatible")
    native_catalog, _ = _resolve_tool_sections(
        ToolSectionsRequest(agent, "继续编码", None, snapshot)
    )
    assert "结构化 Schema 为准" in native_catalog
    assert len(native_catalog) < len(text_catalog) // 2


def test_tool_loop_reports_empty_final_model_response_after_retry():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        (workspace / "notes.txt").write_text("hello empty model response", encoding="utf-8")
        cfg = AgentConfig(enable_tools=True, memory_path="memory.jsonl")
        agent = SimpleAgent(cfg, workspace)
        agent.backend = _EmptyAfterToolBackend()

        with pytest.raises(ProviderResponseError, match="流式响应没有文本内容"):
            agent.run("读取 notes 后总结", save=False, allowed_tools=["read_file"])
        assert agent.backend.calls == 3


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


def test_tool_loop_resets_empty_response_retry_after_successful_model_turn():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        (workspace / "notes.txt").write_text("hello separated empty repairs", encoding="utf-8")
        cfg = AgentConfig(enable_tools=True, memory_path="memory.jsonl")
        agent = SimpleAgent(cfg, workspace)
        agent.backend = _SeparatedEmptyResponsesBackend()

        result = agent.run(
            "读取 notes，写出摘要后继续总结",
            save=False,
            allowed_tools=["read_file", "write_file"],
        )

        _assert_verified_response(
            result,
            "两次独立空响应后仍完成。",
            {("write_file", "succeeded"): 1},
        )
        assert result.executed_tools == ["read_file", "write_file"]
        assert (workspace / "summary.txt").read_text(encoding="utf-8") == "first recovery succeeded"
        assert agent.backend.calls == 5


def test_tool_loop_continues_once_after_incomplete_response_with_durable_tool_results():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        (workspace / "notes.txt").write_text("hello incomplete repair", encoding="utf-8")
        cfg = AgentConfig(enable_tools=True, memory_path="memory.jsonl")
        agent = SimpleAgent(cfg, workspace)
        agent.backend = _IncompleteThenFinalAfterToolBackend()

        result = agent.run("读取 notes 后继续完成", save=False, allowed_tools=["read_file"])

        assert result.response == "已从已完成工具结果继续收口。"
        assert result.executed_tools == ["read_file"]
        assert agent.backend.calls == 3


def test_tool_loop_does_not_replay_incomplete_ordinary_chat_response():
    with tempfile.TemporaryDirectory() as td:
        cfg = AgentConfig(enable_tools=True, memory_path="memory.jsonl")
        agent = SimpleAgent(cfg, Path(td))
        agent.backend = _IncompleteWithoutToolBackend()

        with pytest.raises(ProviderResponseError) as exc_info:
            agent.run("只回答一句普通聊天", save=False)

        assert exc_info.value.error_code == "MODEL_INCOMPLETE_RESPONSE"
        assert agent.backend.calls == 1


def test_tool_loop_continues_only_once_for_repeated_incomplete_response():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        (workspace / "notes.txt").write_text("hello bounded incomplete repair", encoding="utf-8")
        cfg = AgentConfig(enable_tools=True, memory_path="memory.jsonl")
        agent = SimpleAgent(cfg, workspace)
        agent.backend = _RepeatedIncompleteAfterToolBackend()

        with pytest.raises(ProviderResponseError) as exc_info:
            agent.run("读取 notes 后继续完成", save=False, allowed_tools=["read_file"])

        assert exc_info.value.error_code == "MODEL_INCOMPLETE_RESPONSE"
        assert agent.backend.calls == 3


def test_max_tool_rounds_zero_allows_multiple_tool_rounds():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        (workspace / "notes.txt").write_text("hello unlimited", encoding="utf-8")
        cfg = AgentConfig(
            enable_tools=True,
            memory_path="memory.jsonl",
            max_tool_rounds=0,
            prompt_files=[],
        )
        agent = SimpleAgent(cfg, workspace)
        agent.backend = _UnlimitedRoundsBackend()

        result = agent.run("重复读取 notes 后收口", save=False)

        assert result.response == "无限轮数配置已正常收口"
        assert result.tool_rounds == 2
        assert agent.backend.calls == 3
        assert "已达到最大工具轮数限制" not in result.prompt


def test_tool_loop_executes_complete_unclosed_write_file_tool_call():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        cfg = AgentConfig(enable_tools=True, memory_path="memory.jsonl")
        agent = SimpleAgent(cfg, workspace)
        agent.backend = UnclosedWriteFileBackend(workspace)

        result = agent.run("写 index.html", save=False, allowed_tools=["write_file"])

        _assert_verified_response(
            result,
            "写入完成",
            {("write_file", "succeeded"): 1},
        )
        assert result.tool_rounds == 1
        assert agent.backend.calls == 2
        assert (workspace / "index.html").read_text(encoding="utf-8") == (
            "<!doctype html><html><head><title>OK</title></head><body><main>ok</main></body></html>"
        )


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
            task_attributes={"repeat_fail_threshold": 1, "terminal_block_enabled": True},
        )

        assert result.response == "已看到提示，改用其他路径继续推进。"
        assert result.tool_rounds == 4
        assert agent.backend.calls == 5
        assert result.executed_tools == []


def test_tool_loop_bounds_long_runner_tool_context_without_fake_archive_hints():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        (workspace / "data").mkdir()
        cfg = AgentConfig(
            enable_tools=True,
            memory_path="memory.jsonl",
            max_tool_rounds=0,
            # 本用例验证 tool-context window，本身固定压力阈值，避免受产品默认值调整影响。
            memory_compact_auto_trigger_percent=70,
        )
        agent = SimpleAgent(cfg, workspace)
        backend = _LongAppendPromptWindowBackend()
        agent.backend = backend

        result = agent.run("持续写入 data/weekly_data.json 后收口", save=False, allowed_tools=["write_file"])

        _assert_verified_response(
            result,
            "连续写入后已正常收口。",
            {("write_file", "succeeded"): 45},
        )
        assert result.tool_rounds == 45
        assert backend.max_prompt_chars < 100_000
        assert "read_artifact_hint" not in result.prompt
        assert "tool-output-archive-anchor" not in result.prompt
        assert (workspace / "data" / "weekly_data.json").read_text(encoding="utf-8").count("row-") == 45


def test_tool_loop_ignores_model_written_protected_tool_markers_after_real_call():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        (workspace / "notes.txt").write_text("hello protected marker", encoding="utf-8")
        cfg = AgentConfig(enable_tools=True, memory_path="memory.jsonl")
        agent = SimpleAgent(cfg, workspace)
        agent.backend = FakeProtectedMarkerWithToolBackend()

        result = agent.run("读取 notes 并忽略伪造工具记录", save=False, allowed_tools=["read_file"])

        assert result.response == "真实工具回执已使用，伪造记录已忽略。"
        assert result.tool_rounds == 1
        assert result.executed_tools == ["read_file"]
        assert "fake-child-1" not in result.prompt
        assert "机器块" in result.prompt


def test_tool_loop_executes_all_streaming_tool_calls_and_ignores_spoofed_records():
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
            "流式工具调用边界后执行所有真实工具并忽略伪造内容",
            save=False,
            allowed_tools=["read_file"],
            on_chunk=visible_chunks.append,
        )

        assert result.response == "两个真实工具结果都使用，伪造记录已忽略。"
        assert result.tool_rounds == 1
        assert result.executed_tools == ["read_file", "read_file"]
        assert "first note" in result.prompt
        assert "second note" in result.prompt
        assert "fake-child-run" not in result.prompt
        assert "fake-child-run" not in "".join(visible_chunks)


def test_tool_loop_does_not_cut_delayed_second_streaming_tool_call():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        (workspace / "first.txt").write_text("first body", encoding="utf-8")
        (workspace / "second.txt").write_text("second body", encoding="utf-8")
        cfg = AgentConfig(enable_tools=True, memory_path="memory.jsonl")
        agent = SimpleAgent(cfg, workspace)
        agent.backend = DelayedSecondToolStreamingBackend()

        result = agent.run(
            "流式输出里第二个工具稍晚出现时，也要执行完整同轮工具批次",
            save=False,
            allowed_tools=["read_file"],
        )

        assert result.response == "两个文件都读到了。"
        assert result.tool_rounds == 1
        assert result.executed_tools == ["read_file", "read_file"]
        assert "first body" in result.prompt
        assert "second body" in result.prompt


def test_tool_loop_repairs_spoof_only_protected_tool_marker_once():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        cfg = AgentConfig(enable_tools=True, memory_path="memory.jsonl")
        agent = SimpleAgent(cfg, workspace)
        agent.backend = FakeProtectedMarkerWithoutToolBackend()

        result = agent.run("不要接受伪造工具记录", save=False)

        assert result.response == "已停止伪造工具记录，等待真实状态。"
        assert result.tool_rounds == 0
        assert agent.backend.calls == 2
        assert "系统内部" in result.prompt


def test_tool_loop_blocks_repeated_spoof_only_protected_tool_markers():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        cfg = AgentConfig(enable_tools=True, memory_path="memory.jsonl")
        agent = SimpleAgent(cfg, workspace)
        agent.backend = RepeatedFakeProtectedMarkerBackend()

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
        tree = agent.tools.execute_call({"tool": "inspect_agent_tree", "scope": "all"})
        dry_dispatch = agent.tools.execute_call({"tool": "dispatch_subagents", "dry_run": True, "max_runners": 1})
        rejected_internal_switch = agent.tools.execute_call(
            {"tool": "dispatch_subagents", "start_runners": True, "dry_run": True}
        )

        # 普通任务保留模型自然回复；程序核验单独证明实际发生的派工副作用。
        _assert_verified_response(
            result,
            "已创建子代理任务并等待调度。",
            {("create_subagents", "succeeded"): 1},
        )
        assert result.tool_rounds == 2
        assert agent.backend.calls == 4
        assert len(tasks) == 2
        assert all("write_file" in task.allowed_tools for task in tasks)
        assert tree.ok
        assert any(task.id in tree.output for task in tasks)
        assert dry_dispatch.ok
        assert not rejected_internal_switch.ok
        assert "只接受 dry_run" in rejected_internal_switch.output
        assert '"dry_run": true' in dry_dispatch.output


def test_gateway_wait_receipt_is_model_written_from_structured_facts():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        agent = SimpleAgent(
            AgentConfig(
                enable_tools=True,
                memory_path="memory.jsonl",
                subagent_workspace="subs",
                max_subagents=3,
            ),
            workspace,
        )
        agent.backend = _GatewayNaturalDispatchReplyBackend()

        result = agent.run("请把两个部分分别整理后汇总", save=False, source="gateway")

        _assert_verified_response(
            result,
            "我先把两部分拆开整理，汇总好后一起给你。",
            {("create_subagents", "succeeded"): 1},
        )
        assert result.runtime_status == "ok"
        assert result.runtime_reason == "wait"
        assert result.tool_rounds == 2
        assert agent.backend.calls == 3
        assert len(agent.subagents.list_runs()) == 2
        assert "任务已转到后台" not in result.response


def test_create_subagents_accepts_explicit_external_write_target_without_starting():
    """LLM: explicit user output dirs are allowed unless they hit the dangerous-root policy."""
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
                "defer_start": True,
            }
        )

        assert result.ok
        assert "工作区外" not in result.output
        assert len(agent.subagents.list_runs()) == 1


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

        # 去重仍生效；程序核验分别记录一次成功和一次被拦截的未执行尝试。
        _assert_verified_response(
            result,
            "重复派工已被拦截并收口。",
            {
                ("create_subagents", "succeeded"): 1,
                ("create_subagents", "not_started"): 1,
            },
        )
        assert result.tool_rounds == 2
        assert len(tasks) == 1


def test_non_mutating_schedule_result_does_not_consume_one_shot_key():
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
    payload = {"tool": "schedule_child_subagents", "dry_run": False, "children": [{"goal": "cart"}]}

    first = service._execute_one_tool_call(ToolCallExecuteParams(params, 1, 1, payload))
    second = service._execute_one_tool_call(ToolCallExecuteParams(params, 2, 1, payload))

    assert first.ok is True
    assert second.ok is True
    assert agent.tools.calls == 2
    assert "阻止重复执行" not in second.output


def test_batch_create_blocks_overlapping_single_child_calls_in_same_turn():
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
        write_boundary=None,
        task_attributes=None,
        request_id="",
        run_id="",
        task_id="",
        one_shot_tool_calls=set(),
        executed_tools=[],
        archive_tool_calls=[],
    )
    tools = _SuccessfulCreateTools()
    agent = _OneShotHarnessAgent(tools)
    service = ToolLoopService(agent)
    goals = ["研究营养均衡", "研究采购预算", "研究食材复用"]
    batch = {
        "tool": "create_subagents",
        "goal": "并行研究营养计划",
        "items": [{"goal": goal, "role": "worker"} for goal in goals],
    }

    first = service._execute_one_tool_call(ToolCallExecuteParams(params, 1, 1, batch))
    repeated = [
        service._execute_one_tool_call(
            ToolCallExecuteParams(
                params,
                1,
                index,
                {"tool": "create_subagents", "goal": goal, "role": "worker"},
            )
        )
        for index, goal in enumerate(goals, start=2)
    ]

    assert first.ok is True
    assert tools.calls == 1
    assert all(result.ok is False and "阻止重复执行" in result.output for result in repeated)


def test_tool_loop_drains_pending_deferred_tool_calls_before_model_turn():
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
        write_boundary=None,
        task_attributes=None,
        request_id="",
        run_id="",
        task_id="",
        one_shot_tool_calls=set(),
        executed_tools=[],
        archive_tool_calls=[],
        live_archive_state={
            "pending_deferred_tool_calls": [
                {"tool": "read_file", "path": "notes.txt", "offset": 100, "max_chars": 50}
            ]
        },
    )
    agent = object()
    service = ToolLoopService(agent)
    drained: list[list[dict[str, object]]] = []
    model_calls: list[int] = []

    def fake_run_tool_round(request):
        drained.append(list(request.calls))
        return request.tool_rounds, None

    def fake_model_turn_or_retry(params_arg, tool_rounds, empty_repairs):
        del params_arg, empty_repairs
        model_calls.append(tool_rounds)
        return "prompt", ModelResponse(text="done", backend="test"), True, False, 0

    service._run_tool_round = fake_run_tool_round
    service._model_turn_or_retry = fake_model_turn_or_retry

    final_prompt, response, tool_rounds = service.execute(params)

    assert drained == [[{"tool": "read_file", "path": "notes.txt", "offset": 100, "max_chars": 50}]]
    assert model_calls == [1]
    assert final_prompt == "prompt"
    assert response.text == "done"
    assert tool_rounds == 1
    assert "pending_deferred_tool_calls" not in params.live_archive_state


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

        _assert_verified_response(
            result,
            "重复 dispatch 已允许继续推进。",
            {("dispatch_subagents", "succeeded"): 2},
        )
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
        assert result.runtime_status == "unfinished"
        assert result.runtime_reason == "TOOL_ROUND_LIMIT_REACHED"
        assert result.runtime_source == "tool_loop"
        assert agent.backend.calls == 3


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
        assert result.runtime_status == "unfinished"
        assert result.runtime_reason == "TOOL_ROUND_LIMIT_REACHED"
        assert result.runtime_source == "tool_loop"
        assert agent.backend.calls == 3


def test_tool_round_limit_does_not_schedule_ordinary_checklist_continuation(tmp_path):
    from agent_py_agent.agent.agent_core.runtime.owner_roots import runtime_owner_root
    from agent_py_agent.agent.task_progress import write_task_progress

    (tmp_path / "notes.txt").write_text("hello", encoding="utf-8")
    agent = SimpleAgent(
        AgentConfig(
            enable_tools=True,
            memory_path="memory.jsonl",
            max_tool_rounds=1,
        ),
        tmp_path,
    )
    agent.backend = MaxToolRoundBackend()
    thread = agent.conversation_store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "chat-limit",
            "channel_user_id": "user-1",
        }
    )
    agent.conversation_store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-limit",
            "goal": "读取并整理文件",
            "status": "active",
        }
    )
    write_task_progress(
        runtime_owner_root(agent),
        "task-limit",
        {"items": [{"id": "verify", "status": "pending", "title": "完成验证"}]},
    )

    result = agent.run(
        "读取 notes 并完成验证",
        save=True,
        task_id="task-limit",
        task_attributes={
            "conversation_thread_id": thread.thread_id,
            "conversation_task_id": "task-limit",
        },
        source="gateway",
    )

    assert result.runtime_status == "unfinished"
    link = next(
        item
        for item in agent.conversation_store.task_links(thread.thread_id)
        if item.task_id == "task-limit"
    )
    assert link.status == "active"
    assert agent.conversation_store.list_progress_policies(enabled_only=True) == []


def test_explicit_goal_cannot_close_with_open_progress(tmp_path):
    from agent_py_agent.agent.agent_core.runtime.owner_roots import runtime_owner_root
    from agent_py_agent.agent.task_progress import write_task_progress

    (tmp_path / "notes.txt").write_text("hello tool world", encoding="utf-8")
    agent = SimpleAgent(
        AgentConfig(enable_tools=True, memory_path="memory.jsonl"),
        tmp_path,
    )
    class OpenProgressBackend:
        name = "open_progress"

        def __init__(self):
            self.calls = 0
            self.prompts: list[str] = []

        def generate(self, prompt: str, on_chunk=None):
            del on_chunk
            self.calls += 1
            self.prompts.append(prompt)
            if self.calls == 1:
                return ModelResponse(
                    text='[TOOL_CALL]\n{"tool": "read_file", "path": "notes.txt"}\n[/TOOL_CALL]',
                    backend=self.name,
                )
            return ModelResponse(text="文件已经读取，但验证项仍未完成。", backend=self.name)

    agent.backend = OpenProgressBackend()
    thread = agent.conversation_store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "chat-open-progress",
            "channel_user_id": "user-1",
        }
    )
    agent.conversation_store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-open-progress",
            "goal": "读取后继续完成验证",
            "status": "active",
        }
    )
    goal = agent.conversation_store.create_goal(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-open-progress",
            "objective": "读取后继续完成验证",
        }
    )
    write_task_progress(
        runtime_owner_root(agent),
        "task-open-progress",
        {"items": [{"id": "verify", "status": "pending", "title": "完成验证"}]},
    )

    result = agent.run(
        "继续处理",
        save=True,
        task_id="request-attempt-open-progress",
        task_attributes={
            "conversation_thread_id": thread.thread_id,
            "conversation_task_id": "task-open-progress",
            "thread_goal_id": goal.goal_id,
        },
        source="gateway",
    )

    assert agent.backend.calls == 2
    assert result.response.startswith("文件已经读取，但验证项仍未完成")
    assert result.runtime_status == "unfinished"
    assert result.runtime_reason == "TASK_PROGRESS_OPEN"
    assert result.runtime_source == "task_progress"
    current = agent.conversation_store.load_thread(thread.thread_id)
    assert current is not None and current.active_task_ids == ("task-open-progress",)
    policies = agent.conversation_store.list_progress_policies(enabled_only=True)
    assert len(policies) == 1
    assert policies[0].task_id == "task-open-progress"
    assert policies[0].metadata["tool"] == "goal_progress_continuation"
    assert policies[0].metadata["expedite_reason"] == "typed_unfinished_foreground"
    assert policies[0].next_due_at <= policies[0].metadata["expedited_at"]


def test_ordinary_task_open_progress_is_history_not_turn_lifecycle(tmp_path):
    from agent_py_agent.agent.agent_core.runtime.owner_roots import runtime_owner_root
    from agent_py_agent.agent.conversation.authority import (
        CONVERSATION_TASK_TURN_ACTIVE_ATTR,
    )
    from agent_py_agent.agent.task_progress import write_task_progress

    agent = SimpleAgent(
        AgentConfig(enable_tools=True, memory_path="memory.jsonl"),
        tmp_path,
    )

    class OrdinaryTaskBackend:
        name = "ordinary_open_progress"

        def __init__(self):
            self.prompts: list[str] = []

        def generate(self, prompt: str, on_chunk=None):
            del on_chunk
            self.prompts.append(prompt)
            assert "[tool-system:task-progress-completion-check]" not in prompt
            assert "[tool-system:task-progress-tracking-reminder]" not in prompt
            return ModelResponse(text="当前这轮已经结束。", backend=self.name)

    backend = OrdinaryTaskBackend()
    agent.backend = backend
    thread = agent.conversation_store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "chat-ordinary-open-progress",
            "channel_user_id": "user-1",
        }
    )
    agent.conversation_store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-ordinary-open-progress",
            "goal": "完成普通任务",
            "status": "active",
        }
    )
    write_task_progress(
        runtime_owner_root(agent),
        "task-ordinary-open-progress",
        {"items": [{"id": "verify", "status": "pending", "title": "可选核对"}]},
    )

    result = agent.run(
        "继续处理",
        save=True,
        task_id="request-attempt-ordinary-open-progress",
        task_attributes={
            "conversation_thread_id": thread.thread_id,
            "conversation_task_id": "task-ordinary-open-progress",
            CONVERSATION_TASK_TURN_ACTIVE_ATTR: True,
        },
        source="gateway",
    )

    assert len(backend.prompts) == 1
    assert result.response == "当前这轮已经结束。"
    assert result.runtime_status == "ok"
    current = agent.conversation_store.load_thread(thread.thread_id)
    assert current is not None and current.active_task_ids == ()
    links = agent.conversation_store.task_links(thread.thread_id)
    assert len(links) == 1 and links[0].status == "completed"
    assert agent.conversation_store.list_progress_policies(enabled_only=True) == []
