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
from agent_py_agent.agent.agent_core._tool_loop_service import (
    ToolLoopService,
    _runtime_workspace_context,
)
from agent_py_agent.agent.agent_core.tool_loop.round_execution import (
    ToolCallExecuteParams,
    ToolCallRecordParams,
    ToolRoundExecutionRequest,
    execute_tool_round,
)
from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.backends.errors import ProviderResponseError
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig as _AgentConfig
from agent_py_agent.agent.tooling import ToolHandlerOutcome
from agent_py_agent.agent.tooling.action_policy import ActionDecision
from agent_py_agent.agent.tooling.executor import ToolExecution
from agent_py_agent.agent.tooling.runtime_contracts import (
    ProviderToolCapability,
    ToolCall,
    ToolFailureFacts,
    ToolProtocolSnapshot,
    ToolResult,
    ToolSuccessFacts,
)
from agent_py_agent.tests._tool_runtime_harness import execute_registry_test_call

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


def _text_agent_config(**kwargs) -> _AgentConfig:
    """Make the legacy fake text backends explicit instead of relying on fallback."""

    kwargs.setdefault("tool_protocol", "text")
    return _AgentConfig(**kwargs)


def _assert_verified_response(
    result,
    model_text: str,
    expected_operations: dict[tuple[str, str], int],
) -> None:
    """Keep the model-authored reply while checking the machine-authored operation proof."""

    assert result.response == model_text
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


def _canonical_test_call(
    tool_name: str,
    arguments: dict[str, object],
    *,
    call_id: str = "test-call-1",
    run_id: str = "run-progress",
) -> ToolCall:
    return ToolCall(
        call_id=call_id,
        tool_name=tool_name,
        arguments=arguments,
        source_protocol="text",
        schema_hash="sha256:test-schema",
        run_id=run_id,
        turn_id=f"{run_id}:turn-1",
        attempt_id=f"{run_id}:attempt-1",
    )


def _successful_test_execution(call: ToolCall, output: str) -> ToolExecution:
    return ToolExecution(
        call=call,
        decision=ActionDecision("allow"),
        result=ToolResult.succeeded(
            call,
            output,
            facts=ToolSuccessFacts(effect_outcome="confirmed"),
        ),
        states=(
            "received",
            "normalized",
            "validated",
            "authorized",
            "approved",
            "running",
            "succeeded",
            "reconciled",
            "persisted",
            "projected",
        ),
    )


def _failed_test_execution(call: ToolCall, outcome: ToolHandlerOutcome) -> ToolExecution:
    error_code = outcome.error_code or "TOOL_GUARDRAIL_DENIED"
    return ToolExecution(
        call=call,
        decision=ActionDecision(
            "deny",
            (error_code,),
            {"failure_stage": outcome.failure_stage or "runtime_gate"},
        ),
        result=ToolResult.failed(
            call,
            outcome.output,
            error_code=error_code,
            failure_stage=outcome.failure_stage or "runtime_gate",
            facts=ToolFailureFacts(handler_executed=False),
        ),
        states=("received", "normalized", "failed", "persisted", "projected"),
    )


def _text_protocol_snapshot(run_id: str) -> ToolProtocolSnapshot:
    return ToolProtocolSnapshot(
        run_id,
        "text",
        ProviderToolCapability(
            provider="test",
            endpoint="local://test",
            model="test-model",
            stream=False,
            native_supported=False,
            evidence="test_fixture",
        ),
    )


def _native_protocol_snapshot(run_id: str) -> ToolProtocolSnapshot:
    return ToolProtocolSnapshot(
        run_id,
        "native",
        ProviderToolCapability(
            provider="test",
            endpoint="local://test",
            model="test-model",
            stream=False,
            native_supported=True,
            evidence="test_fixture",
        ),
    )


class _BlockedScheduleTools:
    def __init__(self):
        self.calls = 0

    def execute_tool(self, call: ToolCall, **kwargs):
        gate = kwargs["pre_handler_gate"](call)
        if gate is not None:
            return _failed_test_execution(call, gate)
        self.calls += 1
        return _successful_test_execution(
            call,
            '{"blocked": true, "reason": "duplicate_leaf_target:app.js", "created_run_ids": []}',
        )


class _SuccessfulCreateTools:
    def __init__(self):
        self.calls = 0

    def execute_tool(self, call: ToolCall, **kwargs):
        gate = kwargs["pre_handler_gate"](call)
        if gate is not None:
            return _failed_test_execution(call, gate)
        self.calls += 1
        return _successful_test_execution(call, '{"created": 1}')


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
        raise ProviderResponseError(
            "Anthropic-compatible 流式响应没有文本内容", error_code="MODEL_EMPTY_RESPONSE"
        )


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
            raise ProviderResponseError(
                "Anthropic-compatible 流式响应没有文本内容", error_code="MODEL_EMPTY_RESPONSE"
            )
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
            raise ProviderResponseError(
                "second isolated empty response", error_code="MODEL_EMPTY_RESPONSE"
            )
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
        # 工具 schema 与当前基础提示词已经超过旧的 40K 人工窗口；本测试用 45 轮累计写入
        # 验证长工具循环保持有界，不把“基础提示词放不下”误报成 reducer 回归。
        self.context_window_tokens = 80_000
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
        cfg = _text_agent_config(
            enable_tools=True,
            tool_protocol="text",
            memory_path="memory.jsonl",
        )
        agent = SimpleAgent(cfg, workspace)
        agent.backend = ToolCallingBackend()
        result = agent.run("读取 notes.txt 并总结", save=False)
        assert result.response == "工具执行完成"
        assert result.tool_rounds == 1
        assert "hello tool world" in result.prompt


def test_tool_loop_reuses_one_workspace_context_snapshot_across_model_rounds(tmp_path):
    (tmp_path / "notes.txt").write_text("hello tool world", encoding="utf-8")
    agent = SimpleAgent(_text_agent_config(enable_tools=True, memory_path="memory.jsonl"), tmp_path)
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


def test_tool_loop_projects_live_promoted_workspace_from_same_write_boundary(tmp_path):
    service_cwd = tmp_path / "service-cwd"
    service_cwd.mkdir()
    task_root = tmp_path / "owners" / "u1" / "tasks" / "task-a"
    output_dir = task_root / "output"
    work_dir = task_root / "work"
    output_dir.mkdir(parents=True)
    work_dir.mkdir()
    agent = SimpleAgent(
        _text_agent_config(enable_tools=True, memory_path="memory.jsonl"),
        service_cwd,
    )
    params = ToolLoopExecuteParams(
        user_prompt="写一个小项目",
        memories=[],
        runtime_injections=[],
        prompt_files=[],
        tool_catalog_section="",
        tool_recommendations_section="",
        tool_context=[],
        effective_on_chunk=None,
        allowed_tools=None,
        write_boundary={"allowed_write_roots": [str(output_dir), str(work_dir)]},
        task_attributes={
            "conversation_thread_id": "thread-a",
            "conversation_task_id": "task-a",
            "run_workspace": {
                "task_root": str(task_root),
                "output_dir": str(output_dir),
                "work_dir": str(work_dir),
            },
        },
        request_id="request-a",
        run_id="run-a",
        task_id="task-a",
        one_shot_tool_calls=set(),
        executed_tools=[],
        archive_tool_calls=[],
        workspace_context_snapshot=agent.prompts.snapshot_workspace_context(),
    )

    projected = _runtime_workspace_context(agent, params)

    assert projected is not None
    assert f"当前工具工作目录（仅供执行定位）: {task_root}" in projected
    assert f"task_output_dir: {output_dir}" in projected
    assert f"task_work_dir: {work_dir}" in projected
    assert f"当前工具工作目录（仅供执行定位）: {service_cwd.resolve()}" not in projected


def test_audit_source_worker_uses_facts_only_workspace_snapshot(tmp_path):
    from agent_py_agent.agent.common.audit_activation import (
        AUDIT_ATTR,
        AUDIT_SOURCE_ID_ATTR,
        AUDIT_SOURCE_OWNER_HOME_ATTR,
        AUDIT_SOURCE_WATCH_ID_ATTR,
        AUDIT_SOURCE_WORKER_ATTR,
        AUDIT_SOURCE_WORKER_KEY_ATTR,
        audit_source_worker_key,
    )
    from agent_py_agent.agent.conversation.authority import (
        CONVERSATION_REQUEST_ID_ATTR,
    )

    agent = SimpleAgent(
        _text_agent_config(enable_tools=False, memory_path="memory.jsonl"),
        tmp_path,
    )
    backend = MagicMock()
    backend.name = "facts-only-backend"
    backend.generate.return_value = ModelResponse(
        text="当前来源研判完成",
        backend=backend.name,
    )
    agent.backend = backend
    agent.prompts.snapshot_workspace_context = MagicMock(
        return_value="- current_local_date: 2026-08-01"
    )
    audit_id = "audit-facts-only"
    watch_id = "ws-auth"
    attrs = {
        AUDIT_ATTR: True,
        CONVERSATION_REQUEST_ID_ATTR: audit_id,
        AUDIT_SOURCE_WORKER_ATTR: True,
        AUDIT_SOURCE_ID_ATTR: "auth",
        AUDIT_SOURCE_WATCH_ID_ATTR: watch_id,
        AUDIT_SOURCE_WORKER_KEY_ATTR: audit_source_worker_key(
            audit_id,
            watch_id,
        ),
        AUDIT_SOURCE_OWNER_HOME_ATTR: str(tmp_path),
    }

    result = agent.run(
        "研判当前来源",
        save=False,
        task_attributes=attrs,
    )

    assert result.response
    agent.prompts.snapshot_workspace_context.assert_called_once_with(facts_only=True)


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
        tool_protocol_snapshot=_text_protocol_snapshot("run-progress"),
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
            [_canonical_test_call("run_command", {"command": "echo hello"})],
            lambda request: _successful_test_execution(request.call, "hello\n"),
            records.append,
        )
    )

    rendered = "".join(chunks)
    assert "[工具] round=1 #1 run_command 开始: echo hello" in rendered
    assert "[工具] round=1 #1 run_command 完成" in rendered
    assert records[0].result.ok is True


def test_plain_parallel_project_prompt_recommends_tool_search(tmp_path):
    """大白话里的并行任务先命中按需搜索，不在首轮直接暴露编排工具。"""
    agent = SimpleAgent(_text_agent_config(enable_tools=True, memory_path="memory.jsonl"), tmp_path)

    _catalog, recommendations = (
        agent.tools.render_catalog_section(),
        agent.tools.render_recommended_tools_section("请让子代理分别去看不同项目，最后你汇总。"),
    )

    assert "tool_search" in recommendations
    assert "create_subagents" not in recommendations


def test_runtime_tool_sections_use_user_prompt_for_orchestration_recommendations(tmp_path):
    """运行时推荐工具必须看用户原始任务，不能退化成空 query。"""
    from agent_py_agent.agent.agent_core.runtime.loop_support import (
        ToolSectionsRequest,
        _resolve_tool_sections,
    )

    agent = SimpleAgent(_text_agent_config(enable_tools=True, memory_path="memory.jsonl"), tmp_path)

    _catalog, recommendations = _resolve_tool_sections(
        ToolSectionsRequest(
            agent=agent,
            user_prompt="请让子代理分别去看不同项目，最后你汇总。",
            allowed_tools=None,
            runtime_snapshot=agent.tools.runtime_snapshot(),
            protocol_snapshot=_text_protocol_snapshot("tool-sections"),
        )
    )

    assert "tool_search" in recommendations
    assert "create_subagents" not in recommendations


def test_runtime_tool_sections_only_collapse_catalog_when_native_is_effective(tmp_path):
    from agent_py_agent.agent.agent_core.runtime.loop_support import (
        ToolSectionsRequest,
        _resolve_tool_sections,
    )

    agent = SimpleAgent(
        _text_agent_config(enable_tools=True, tool_protocol="native", memory_path="memory.jsonl"),
        tmp_path,
    )
    snapshot = agent.tools.runtime_snapshot()

    # Echo cannot consume provider-native schemas, so configured "native" must
    # retain the complete text catalog once the run-start probe selected text.
    text_catalog, _ = _resolve_tool_sections(
        ToolSectionsRequest(
            agent,
            "继续编码",
            None,
            snapshot,
            _text_protocol_snapshot("tool-sections-text"),
        )
    )
    assert "read_file" in text_catalog

    # A separate run-start snapshot with observed native capability collapses
    # the prose catalog. Changing backend identity mid-run is deliberately not
    # part of the contract anymore.
    native_catalog, _ = _resolve_tool_sections(
        ToolSectionsRequest(
            agent,
            "继续编码",
            None,
            snapshot,
            _native_protocol_snapshot("tool-sections-native"),
        )
    )
    assert "结构化 Schema 为准" in native_catalog
    assert len(native_catalog) < len(text_catalog) // 2


def test_tool_loop_reports_empty_final_model_response_after_retry():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        (workspace / "notes.txt").write_text("hello empty model response", encoding="utf-8")
        cfg = _text_agent_config(enable_tools=True, memory_path="memory.jsonl")
        agent = SimpleAgent(cfg, workspace)
        agent.backend = _EmptyAfterToolBackend()

        with pytest.raises(ProviderResponseError, match="流式响应没有文本内容"):
            agent.run("读取 notes 后总结", save=False, allowed_tools=["read_file"])
        assert agent.backend.calls == 3


def test_tool_loop_retries_once_when_final_model_response_is_empty_after_tool():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        (workspace / "notes.txt").write_text("hello empty repair", encoding="utf-8")
        cfg = _text_agent_config(enable_tools=True, memory_path="memory.jsonl")
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
        cfg = _text_agent_config(enable_tools=True, memory_path="memory.jsonl")
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
        cfg = _text_agent_config(enable_tools=True, memory_path="memory.jsonl")
        agent = SimpleAgent(cfg, workspace)
        agent.backend = _IncompleteThenFinalAfterToolBackend()

        result = agent.run("读取 notes 后继续完成", save=False, allowed_tools=["read_file"])

        assert result.response == "已从已完成工具结果继续收口。"
        assert result.executed_tools == ["read_file"]
        assert agent.backend.calls == 3


def test_tool_loop_does_not_replay_incomplete_ordinary_chat_response():
    with tempfile.TemporaryDirectory() as td:
        cfg = _text_agent_config(enable_tools=True, memory_path="memory.jsonl")
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
        cfg = _text_agent_config(enable_tools=True, memory_path="memory.jsonl")
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
        cfg = _text_agent_config(
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


def test_tool_loop_rejects_unclosed_write_then_executes_complete_repair():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        cfg = _text_agent_config(
            enable_tools=True,
            tool_protocol="text",
            memory_path="memory.jsonl",
        )
        agent = SimpleAgent(cfg, workspace)
        agent.backend = UnclosedWriteFileBackend(workspace)

        result = agent.run("写 index.html", save=False, allowed_tools=["write_file"])

        _assert_verified_response(
            result,
            "写入完成",
            {("write_file", "succeeded"): 1},
        )
        assert result.tool_rounds == 1
        assert agent.backend.calls == 3
        assert (workspace / "index.html").read_text(encoding="utf-8") == (
            "<!doctype html><html><head><title>OK</title></head><body><main>ok</main></body></html>"
        )


def test_tool_loop_enforces_per_agent_tool_budget_for_run_id():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        (workspace / "notes.txt").write_text("budget note", encoding="utf-8")
        cfg = _text_agent_config(
            enable_tools=True,
            memory_path="memory.jsonl",
            max_tool_rounds=4,
            tool_agent_budget_window_seconds=600,
            tool_agent_budget_max_calls=1,
        )
        agent = SimpleAgent(cfg, workspace)
        agent.backend = BudgetedRepeatedReadBackend()

        result = agent.run(
            "重复读文件后自检", save=False, allowed_tools=["read_file"], run_id="run-budget"
        )

        assert result.response == "预算触发后已自检收口。"
        assert result.tool_rounds == 2
        assert agent.backend.calls == 3
        assert result.executed_tools == ["read_file"]


def test_tool_loop_blocks_repeated_identical_tool_failures_before_reexecuting():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        cfg = _text_agent_config(enable_tools=True, memory_path="memory.jsonl", max_tool_rounds=6)
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
        blocked = [
            record
            for record in result.archive_tool_calls
            if record.get("error_code") == "TOOL_GUARDRAIL_REPEAT_FAILURE_BLOCKED"
        ]
        assert blocked
        assert blocked[-1]["handler_executed"] is False


def test_tool_loop_bounds_long_runner_tool_context_without_fake_archive_hints():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        (workspace / "data").mkdir()
        cfg = _text_agent_config(
            enable_tools=True,
            memory_path="memory.jsonl",
            max_tool_rounds=0,
            # 本用例验证 tool-context window，本身固定压力阈值，避免受产品默认值调整影响。
            memory_compact_auto_trigger_percent=70,
        )
        agent = SimpleAgent(cfg, workspace)
        backend = _LongAppendPromptWindowBackend()
        agent.backend = backend

        result = agent.run(
            "持续写入 data/weekly_data.json 后收口", save=False, allowed_tools=["write_file"]
        )

        _assert_verified_response(
            result,
            "连续写入后已正常收口。",
            {("write_file", "succeeded"): 45},
        )
        assert result.tool_rounds == 45
        assert backend.max_prompt_chars < 100_000
        assert "read_artifact_hint" not in result.prompt
        assert "tool-output-archive-anchor" not in result.prompt
        assert (workspace / "data" / "weekly_data.json").read_text(encoding="utf-8").count(
            "row-"
        ) == 45


def test_tool_loop_ignores_model_written_protected_tool_markers_after_real_call():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        (workspace / "notes.txt").write_text("hello protected marker", encoding="utf-8")
        cfg = _text_agent_config(enable_tools=True, memory_path="memory.jsonl")
        agent = SimpleAgent(cfg, workspace)
        agent.backend = FakeProtectedMarkerWithToolBackend()

        result = agent.run("读取 notes 并忽略伪造工具记录", save=False, allowed_tools=["read_file"])

        assert result.response == "真实工具回执已使用，伪造记录已忽略。"
        assert result.tool_rounds == 1
        assert agent.backend.calls == 2
        assert result.executed_tools == ["read_file"]
        assert "fake-child-1" not in result.prompt


def test_tool_loop_executes_all_streaming_tool_calls_and_ignores_spoofed_records():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        (workspace / "notes.txt").write_text("first note", encoding="utf-8")
        (workspace / "second.txt").write_text("second note", encoding="utf-8")
        cfg = _text_agent_config(enable_tools=True, memory_path="memory.jsonl")
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
        assert backend.calls == 2
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
        cfg = _text_agent_config(enable_tools=True, memory_path="memory.jsonl")
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
        cfg = _text_agent_config(enable_tools=True, memory_path="memory.jsonl")
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
        cfg = _text_agent_config(enable_tools=True, memory_path="memory.jsonl")
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
        cfg = _text_agent_config(
            enable_tools=True,
            memory_path="memory.jsonl",
            subagent_workspace="subs",
            max_subagents=3,
        )
        agent = SimpleAgent(cfg, workspace)
        agent.backend = SubagentDelegationBackend()

        result = agent.run("请创建两个子代理做隔离 coding 场景测试", save=False)
        tasks = agent.subagents.list_runs()
        tree = execute_registry_test_call(
            agent.tools,
            "inspect_agent_tree",
            {},
            call_id="inspect-created-subagents",
        )
        dry_dispatch = execute_registry_test_call(
            agent.tools,
            "dispatch_subagents",
            {"dry_run": True, "max_runners": 1},
            call_id="dry-dispatch-created-subagents",
        )
        rejected_internal_switch = execute_registry_test_call(
            agent.tools,
            "dispatch_subagents",
            {"start_runners": True, "dry_run": True},
            call_id="reject-internal-dispatch-switch",
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
        assert rejected_internal_switch.error_code == "TOOL_INTERNAL_PARAMETER_FORBIDDEN"
        assert rejected_internal_switch.handler_executed is False
        assert '"dry_run": true' in dry_dispatch.output


def test_gateway_wait_receipt_is_model_written_from_structured_facts():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        agent = SimpleAgent(
            _text_agent_config(
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
            {
                ("create_subagents", "succeeded"): 1,
                ("wait", "succeeded"): 1,
            },
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
        cfg = _text_agent_config(
            enable_tools=True,
            memory_path="memory.jsonl",
            subagent_workspace="subs",
            max_subagents=3,
        )
        agent = SimpleAgent(cfg, workspace)

        result = execute_registry_test_call(
            agent.tools,
            "create_subagents",
            {
                "goal": "创建一个 txt 文件",
                "allowed_tools": ["read_file", "write_file"],
                "output_files": [str(external_dir / "result.txt")],
                "defer_start": True,
            },
            call_id="create-external-output-subagent",
        )

        assert result.ok
        assert "工作区外" not in result.output
        assert len(agent.subagents.list_runs()) == 1
        assert agent.subagents.list_runs()[0].attributes["output_files"] == [
            str(external_dir / "result.txt")
        ]


def test_repeated_orchestration_tool_call_is_not_executed_twice():
    """LLM: verify that identical consecutive orchestration tool calls are deduplicated.

    新手说明:
    连续两次 create_subagents 只应执行一次，第二次被拦截。
    """
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        cfg = _text_agent_config(
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
        request_id="request-one-shot-blocked",
        run_id="run-one-shot-blocked",
        task_id="task-one-shot-blocked",
        one_shot_tool_calls=set(),
        executed_tools=[],
        archive_tool_calls=[],
    )
    agent = _OneShotHarnessAgent(_BlockedScheduleTools())
    service = ToolLoopService(agent)
    arguments = {"dry_run": False, "children": [{"goal": "cart"}]}
    first_call = _canonical_test_call(
        "schedule_child_subagents",
        arguments,
        call_id="blocked-schedule-1",
        run_id="run-one-shot-blocked",
    )
    second_call = _canonical_test_call(
        "schedule_child_subagents",
        arguments,
        call_id="blocked-schedule-2",
        run_id="run-one-shot-blocked",
    )

    first = service._execute_one_tool_call(ToolCallExecuteParams(params, 1, 1, first_call))
    second = service._execute_one_tool_call(ToolCallExecuteParams(params, 2, 1, second_call))

    assert first.result.ok is True
    assert second.result.ok is True
    assert agent.tools.calls == 2
    assert "阻止重复执行" not in second.result.output


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
        request_id="request-one-shot-batch",
        run_id="run-one-shot-batch",
        task_id="task-one-shot-batch",
        one_shot_tool_calls=set(),
        executed_tools=[],
        archive_tool_calls=[],
    )
    tools = _SuccessfulCreateTools()
    agent = _OneShotHarnessAgent(tools)
    service = ToolLoopService(agent)
    goals = ["研究营养均衡", "研究采购预算", "研究食材复用"]
    batch = {
        "goal": "并行研究营养计划",
        "items": [{"goal": goal, "role": "worker"} for goal in goals],
    }

    first = service._execute_one_tool_call(
        ToolCallExecuteParams(
            params,
            1,
            1,
            _canonical_test_call(
                "create_subagents",
                batch,
                call_id="batch-create-1",
                run_id="run-one-shot-batch",
            ),
        )
    )
    repeated = [
        service._execute_one_tool_call(
            ToolCallExecuteParams(
                params,
                1,
                index,
                _canonical_test_call(
                    "create_subagents",
                    {"goal": goal, "role": "worker"},
                    call_id=f"single-create-{index}",
                    run_id="run-one-shot-batch",
                ),
            )
        )
        for index, goal in enumerate(goals, start=2)
    ]

    assert first.result.ok is True
    assert tools.calls == 1
    assert all(
        result.result.ok is False and "阻止重复执行" in result.result.output for result in repeated
    )


def test_tool_loop_drains_pending_deferred_tool_calls_before_model_turn(tmp_path):
    run_id = "run-deferred-drain"
    agent = SimpleAgent(
        _text_agent_config(enable_tools=True, memory_path="memory.jsonl"),
        tmp_path,
    )
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
        request_id="request-deferred-drain",
        run_id=run_id,
        task_id="task-deferred-drain",
        one_shot_tool_calls=set(),
        executed_tools=[],
        archive_tool_calls=[],
        tool_runtime_snapshot=agent.tools.runtime_snapshot(run_id=run_id),
        tool_protocol_snapshot=_text_protocol_snapshot(run_id),
        live_archive_state={
            "pending_deferred_tool_calls": [
                {"tool": "read_file", "path": "notes.txt", "offset": 100, "max_chars": 50}
            ]
        },
    )
    service = ToolLoopService(agent)
    drained: list[list[ToolCall]] = []
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

    assert [[call.tool_name for call in calls] for calls in drained] == [["read_file"]]
    assert drained[0][0].arguments == {
        "path": "notes.txt",
        "offset": 100,
        "max_chars": 50,
    }
    assert model_calls == [1]
    assert final_prompt == "prompt"
    assert response.text == "done"
    assert tool_rounds == 1
    assert "pending_deferred_tool_calls" not in params.live_archive_state


def test_repeated_dispatch_is_allowed_for_parent_progress_loops():
    """LLM: dispatch_subagents may need repeated identical calls when rate limits leave pending children."""
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        cfg = _text_agent_config(
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


def test_max_tool_rounds_generates_model_authored_interim_response():
    """LLM: verify that hitting max_tool_rounds produces an unfinished model reply.

    新手说明:
    把 max_tool_rounds 设为 1，模型应从结构化限制事实写阶段回复，不得宣称收口。
    """
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        (workspace / "notes.txt").write_text("hello", encoding="utf-8")
        cfg = _text_agent_config(
            enable_tools=True,
            memory_path="memory.jsonl",
            max_tool_rounds=1,
        )
        agent = SimpleAgent(cfg, workspace)
        agent.backend = MaxToolRoundBackend()

        result = agent.run("读取 notes", save=False)

        assert result.response == "这一轮已完成现有步骤，但任务尚未结束，系统会沿当前状态继续。"
        assert result.tool_rounds == 1
        assert result.runtime_status == "unfinished"
        assert result.runtime_reason == "TOOL_ROUND_LIMIT_REACHED"
        assert result.runtime_source == "tool_loop"
        assert agent.backend.calls == 3


def test_max_tool_rounds_hard_stops_when_model_still_requests_tools():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        cfg = _text_agent_config(enable_tools=True, memory_path="memory.jsonl", max_tool_rounds=1)
        agent = SimpleAgent(cfg, workspace)
        agent.backend = StubbornToolAfterLimitBackend()

        result = agent.run("读取 notes", save=False)

        assert result.response == ""
        assert "[TOOL_CALL]" not in result.response
        assert result.tool_rounds == 1
        assert result.runtime_status == "unfinished"
        assert result.runtime_reason == "TOOL_ROUND_LIMIT_REACHED"
        assert result.runtime_source == "tool_loop"
        assert agent.backend.calls == 4


def test_tool_round_limit_schedules_ordinary_task_resume(tmp_path):
    """LLM: 普通任务(无 /goal)轮限收口后创建带预算的自动续跑 policy。

    2026-08-08 真机查证:gateway worker 的 run source 是硬编码 "gateway"(request_execution
    _gateway_run_params),原白名单本就匹配——收口后 policy 确实创建。真 bug 在调度器
    (policy 创建后无人消费),此处测试只固定创建侧语义:前台普通任务(gateway/chat/
    cli_run/cli_*/http:*/空)创建 kind=ordinary_task_resume 的 policy,预算
    resume_used < resume_limit 才续;后台唤醒轮不占预算。
    """
    (tmp_path / "notes.txt").write_text("hello", encoding="utf-8")
    agent = SimpleAgent(
        _text_agent_config(
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

    result = agent.run(
        "读取 notes 并完成验证",
        save=True,
        task_id="task-limit",
        task_attributes={
            "conversation_thread_id": thread.thread_id,
            "conversation_task_id": "task-limit",
        },
        source="cli_gateway",  # 前台形态之一(前缀匹配),固定创建路径语义
    )

    assert result.runtime_status == "unfinished"
    assert result.runtime_reason == "TOOL_ROUND_LIMIT_REACHED"
    link = next(
        item
        for item in agent.conversation_store.task_links(thread.thread_id)
        if item.task_id == "task-limit"
    )
    assert link.status == "active"
    policies = agent.conversation_store.list_progress_policies(enabled_only=True)
    assert len(policies) == 1
    assert policies[0].task_id == "task-limit" or policies[0].task_id.startswith("task-path:")
    assert policies[0].metadata["kind"] == "ordinary_task_resume"
    assert policies[0].metadata["tool"] == "task_round_resume"
    assert policies[0].metadata["resume_used"] == 1
    assert policies[0].metadata["resume_limit"] == 3
    # due_now expedite: next_due_at 提前到当前,调度器下一 tick 即拉起续跑。
    assert policies[0].next_due_at <= policies[0].metadata["expedited_at"]


def test_ordinary_task_resume_available_signal(tmp_path):
    """LLM: 轮限收口提示词按续跑预算切换——还有预算说「会自动继续」,耗尽说「请回复『继续』」。

    全部用结构化信号(policy metadata 的 resume_used/resume_limit)判定,禁自然语言。
    """
    from types import SimpleNamespace

    from agent_py_agent.agent.agent_core._tool_loop_service import (
        _ordinary_task_resume_available,
    )

    (tmp_path / "notes.txt").write_text("hello", encoding="utf-8")
    agent = SimpleAgent(
        _text_agent_config(enable_tools=True, memory_path="memory.jsonl"),
        tmp_path,
    )
    thread = agent.conversation_store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "chat-resume-signal",
            "channel_user_id": "user-1",
        }
    )
    agent.conversation_store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-resume-signal",
            "goal": "读取并整理文件",
            "status": "active",
        }
    )

    def params(**overrides) -> ToolLoopExecuteParams:
        base = {
            "task_attributes": {
                "conversation_thread_id": thread.thread_id,
                "conversation_task_id": "task-resume-signal",
            },
            "context_scope": "default",
            "source": "gateway",
            "run_id": "run-resume-signal",
            "task_id": "task-resume-signal",
        }
        base.update(overrides)
        return SimpleNamespace(**base)

    # 无 policy(本次收口将创建第一次)= 有预算。
    assert _ordinary_task_resume_available(agent, params()) is True
    # /goal 任务无预算概念,永远乐观(用户显式目标即无限续跑授权)。
    assert (
        _ordinary_task_resume_available(
            agent, params(task_attributes={**params().task_attributes, "thread_goal_id": "goal-1"})
        )
        is True
    )

    # 生产语义:同一任务同一时刻只有一个 ordinary_task_resume policy(创建后每次
    # 收口 expedite 复用),预算随 metadata 递增——这里用 mark_progress_reported
    # 更新同一 policy 模拟真实状态。
    policy = agent.conversation_store.set_progress_policy(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-resume-signal",
            "interval_seconds": 180,
            "route_channel": "internal",
            "route_target": "",
            "metadata": {
                "kind": "ordinary_task_resume",
                "tool": "task_round_resume",
                "resume_used": 1,
                "resume_limit": 3,
            },
        }
    )
    assert _ordinary_task_resume_available(agent, params()) is True
    agent.conversation_store.mark_progress_reported(
        policy.policy_id, metadata_updates={"resume_used": 2}
    )
    assert _ordinary_task_resume_available(agent, params()) is True
    agent.conversation_store.mark_progress_reported(
        policy.policy_id, metadata_updates={"resume_used": 3}
    )
    assert _ordinary_task_resume_available(agent, params()) is False
    # 无 conversation_store 的宿主侧调用 → None(沿用乐观文案)。
    assert _ordinary_task_resume_available(object(), params()) is None


def test_ordinary_task_resume_budget_exhausted_disables_policy(tmp_path):
    """LLM: 普通任务续跑预算耗尽后 policy 退休,调度器不再拉起,等用户显式「继续」。

    resume_used >= resume_limit 时 _schedule_typed_unfinished_continuation 必须
    disable policy(否则 scheduler 每 interval 照拉,预算失去意义)。
    """
    (tmp_path / "notes.txt").write_text("hello", encoding="utf-8")
    agent = SimpleAgent(
        _text_agent_config(
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
            "channel_conversation_id": "chat-limit-budget",
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
    agent.conversation_store.set_progress_policy(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-limit",
            "interval_seconds": 180,
            "route_channel": "internal",
            "route_target": "",
            "metadata": {
                "kind": "ordinary_task_resume",
                "tool": "task_round_resume",
                "resume_used": 3,
                "resume_limit": 3,
            },
        }
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
    policies = agent.conversation_store.list_progress_policies(enabled_only=False)
    assert len(policies) == 1
    assert not policies[0].enabled
    assert agent.conversation_store.list_progress_policies(enabled_only=True) == []


def test_background_main_agent_no_ordinary_resume_policy(tmp_path):
    """LLM: 后台唤醒轮收口不占普通任务续跑预算。

    唤醒轮是「读状态、给回执」语义,推进由 wait 定时器/wake 信号驱动——同 source
    排除先例(task_progress_continuation_decision)。若误建 policy,调度器每 interval
    都会拉起一个空转唤醒 run,白烧预算。前台 cli_gateway 走创建路径(上个测试),
    这里验证排除分支:同样收口,policy 必须为零。
    """
    (tmp_path / "notes.txt").write_text("hello", encoding="utf-8")
    agent = SimpleAgent(
        _text_agent_config(
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
            "channel_conversation_id": "chat-limit-bg",
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

    result = agent.run(
        "读取 notes 并完成验证",
        save=True,
        task_id="task-limit",
        task_attributes={
            "conversation_thread_id": thread.thread_id,
            "conversation_task_id": "task-limit",
        },
        source="background_main_agent",
    )

    assert result.runtime_status == "unfinished"
    assert agent.conversation_store.list_progress_policies(enabled_only=False) == []


def test_explicit_goal_cannot_close_with_open_progress(tmp_path):
    from agent_py_agent.agent.agent_core.runtime.owner_roots import runtime_owner_root
    from agent_py_agent.agent.task_progress import write_task_progress

    (tmp_path / "notes.txt").write_text("hello tool world", encoding="utf-8")
    agent = SimpleAgent(
        _text_agent_config(enable_tools=True, memory_path="memory.jsonl"),
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
        _text_agent_config(enable_tools=True, memory_path="memory.jsonl"),
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
