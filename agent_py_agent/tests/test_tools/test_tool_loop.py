"""LLM: tests for tool loop, delegation, max rounds, allowlists, and security grants.

给人看的解释：
这个文件放和"工具循环流程"相关的测试：工具调用闭环、子代理派工和去重、最大轮数收口、
子代理 output.json 收口、工具授权过滤和安全能力授权。
"""

import hashlib
import json
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

    kwargs.setdefault("tool_protocol", "native")
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
        self.root = Path(tempfile.gettempdir())
        self.effective_workspace_root = self.root


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
        source_protocol="native",
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
    # text 协议已删除(EXEC-31b): 该夹具统一返回 native 快照
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



class _NativeFakeBackend:
    """测试假后端基类: 声明 native 支持(EXEC-31b text 删除后所有假后端走 native)。"""

    def probe_tool_capability(self):
        from agent_py_agent.agent.tooling.runtime_contracts import ProviderToolCapability

        return ProviderToolCapability(
            provider=str(getattr(self, "name", "fake") or "fake"),
            endpoint="local://fake",
            model="",
            stream=False,
            native_supported=True,
            evidence="test_fake_native",
        )


class _UnlimitedRoundsBackend(_NativeFakeBackend):
    name = "fake_unlimited_rounds_backend"

    def __init__(self):
        self.calls = 0

    def generate(self, prompt: str, on_chunk=None):
        self.calls += 1
        if self.calls <= 2:
            return ToolCallingBackend().generate(prompt, on_chunk=on_chunk)
        assert "已达到最大工具轮数限制" not in prompt
        return ModelResponse(text="无限轮数配置已正常收口", backend=self.name)


# LLM: 假后端顺序请求不同文件并最终自己收口；不发网络，真实工具循环必须保持原用户请求不变。
# 类用途: 回归有进展但连续不说正文的原生工具工作，避免被轮数启发式催停。
class _SilentProgressBackend(_NativeFakeBackend):
    name = "fake_silent_progress"

    # LLM: 记录每次实际模型输入以检查宿主是否篡改任务，不模拟工具执行结果。
    # 函数用途: 初始化轮数与请求记录，供七轮不同文件读取的断言使用。
    def __init__(self):
        self.prompts = []

    # LLM: 前七轮没有正文但有有效工具调用；第八轮明确 final，与提示文字无关。
    # 函数用途: 驱动真实读取并保存组包结果，让测试发现中途追加的停止指令。
    def generate(self, prompt: str, on_chunk=None, **kwargs):
        self.prompts.append(prompt)
        number = len(self.prompts)
        if number <= 7:
            return ModelResponse(
                text="",
                tool_use_blocks=[{"id": f"call_progress_{number}", "name": "read_file",
                                  "input": {"path": f"part-{number}.txt"}}],
                backend=self.name,
            )
        return ModelResponse(text="已读完全部记录并汇总。", backend=self.name)


def test_silent_successful_tool_rounds_do_not_rewrite_user_request(tmp_path):
    # LLM: root 只是构造参数，文件工具读的是 owner 墙生效后的 effective_workspace_root；
    # 样例必须写进该根，否则模型请求的相对路径全部 PATH_NOT_FOUND，读不到就证明不了“真执行过”。
    # 函数用途: 用真实工具循环证明合法连续读取既成功执行、又不会被宿主改写用户请求收口。
    agent = SimpleAgent(_text_agent_config(enable_tools=True, max_tool_rounds=20), tmp_path)
    workspace = Path(agent.effective_workspace_root)
    for number in range(1, 8):
        (workspace / f"part-{number}.txt").write_text(f"记录内容 {number}", encoding="utf-8")
    backend = _SilentProgressBackend()
    agent.backend = backend
    result = agent.run("检查各份记录，完成后汇总。", save=False, allowed_tools=["read_file"])
    assert result.response == "已读完全部记录并汇总。"
    assert len(backend.prompts) == 8
    assert all("你已连续多轮只调用工具" not in prompt for prompt in backend.prompts)
    assert all("请停止循环, 基于已有信息直接收口回答" not in prompt for prompt in backend.prompts)
    assert len(result.executed_tools) == 7, [
        {key: row.get(key) for key in ("ok", "error_code", "output_preview", "failure", "effect")}
        for row in result.archive_tool_calls
    ]


class _GatewayNaturalDispatchReplyBackend(_NativeFakeBackend):
    name = "fake_gateway_natural_dispatch_reply"

    def __init__(self):
        self.calls = 0

    def generate(self, prompt: str, on_chunk=None):
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                tool_use_blocks=[{"id": "call_auto", "name": "create_subagents",
                    "input": {"goal": "分别整理两部分",
                              "items": [{"goal": "整理第一部分"}, {"goal": "整理第二部分"}],
                              "tool_preset": "read_only"}}],
                backend=self.name,
            )
        if self.calls == 2:
            assert "[natural-user-reply]" not in prompt
            assert "create_subagents" in prompt
            return ModelResponse(
                tool_use_blocks=[{"id": "call_auto", "name": 'wait', "input": {"seconds": 120, "reason": "稍后继续整理并汇总两个部分"}}],
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


class _RepeatedMissingReadBackend(_NativeFakeBackend):
    name = "fake_repeated_missing_read_backend"

    def __init__(self):
        self.calls = 0

    def generate(self, prompt: str, on_chunk=None):
        self.calls += 1
        if self.calls <= 4:
            if self.calls == 4:
                assert "tool-loop-guardrail-hint" in prompt
            return ModelResponse(
                tool_use_blocks=[{"id": "call_auto", "name": 'read_file', "input": {"path": "missing.txt"}}],
                backend=self.name,
            )
        assert "TOOL_GUARDRAIL_REPEAT_FAILURE_BLOCKED" in prompt
        return ModelResponse(text="已看到提示，改用其他路径继续推进。", backend=self.name)


# LLM: 只模拟原生模型响应；工具操作仍由实际测试执行链完成，不能靠文本伪造工具结果。
# 类用途: 首轮读取后持续抛空响应，验证修复次数有界。
class _EmptyAfterToolBackend(_NativeFakeBackend):
    name = "fake_empty_after_tool_backend"

    # LLM: 每个测试实例独立计数，不共享调用状态。
    # 函数用途: 初始化本例模型调用次数。
    def __init__(self):
        self.calls = 0

    # LLM: 接受原生messages并返回唯一工具call id；不发网络请求。
    # 函数用途: 按调用序号提供工具响应、空响应异常或最终回答。
    def generate(self, prompt: str, on_chunk=None, **kwargs) -> ModelResponse:
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                text="",
                tool_use_blocks=[{"id": "call_auto", "name": 'read_file', "input": {"path": "notes.txt"}}],
                backend=self.name,
            )
        raise ProviderResponseError(
            "Anthropic-compatible 流式响应没有文本内容", error_code="MODEL_EMPTY_RESPONSE"
        )


# LLM: 只模拟原生模型响应；工具操作仍由实际测试执行链完成，不能靠文本伪造工具结果。
# 类用途: 读取后只空一次，验证原生结果保留且不重复执行。
class _EmptyThenFinalAfterToolBackend(_NativeFakeBackend):
    name = "fake_empty_then_final_after_tool_backend"

    # LLM: 每个测试实例独立计数，不共享调用状态。
    # 函数用途: 初始化本例模型调用次数。
    def __init__(self):
        self.calls = 0

    # LLM: 接受原生messages并返回唯一工具call id；不发网络请求。
    # 函数用途: 按调用序号提供工具响应、空响应异常或最终回答。
    def generate(self, prompt: str, on_chunk=None, **kwargs) -> ModelResponse:
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                text="",
                tool_use_blocks=[{"id": "call_auto", "name": 'read_file', "input": {"path": "notes.txt"}}],
                backend=self.name,
            )
        if self.calls == 2:
            raise ProviderResponseError(
                "Anthropic-compatible 流式响应没有文本内容", error_code="MODEL_EMPTY_RESPONSE"
            )
        assert "上一轮模型接口返回了空文本" in json.dumps(kwargs.get("messages"), ensure_ascii=False)
        assert "hello empty repair" in json.dumps(kwargs.get("messages"), ensure_ascii=False)
        return ModelResponse(text="已根据工具结果继续完成。", backend=self.name)


# LLM: 只模拟原生模型响应；工具操作仍由实际测试执行链完成，不能靠文本伪造工具结果。
# 类用途: 两次工具成功之间分别空响应，验证修复额度逐轮重置。
class _SeparatedEmptyResponsesBackend(_NativeFakeBackend):
    name = "fake_separated_empty_responses_backend"

    # LLM: 每个测试实例独立计数，不共享调用状态。
    # 函数用途: 初始化本例模型调用次数。
    def __init__(self):
        self.calls = 0

    # LLM: 接受原生messages并返回唯一工具call id；不发网络请求。
    # 函数用途: 按调用序号提供工具响应、空响应异常或最终回答。
    def generate(self, prompt: str, on_chunk=None, **kwargs) -> ModelResponse:
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                text="",
                tool_use_blocks=[{"id": "call_auto", "name": 'read_file', "input": {"path": "notes.txt"}}],
                backend=self.name,
            )
        if self.calls == 2:
            raise ProviderResponseError("first empty response", error_code="MODEL_EMPTY_RESPONSE")
        if self.calls == 3:
            assert "上一轮模型接口返回了空文本" in json.dumps(kwargs.get("messages"), ensure_ascii=False)
            return ModelResponse(
                text="",
                tool_use_blocks=[{"id": "call_recovered_write", "name": "write_file",
                    "input": {"path": "summary.txt",
                              "content": "first recovery succeeded"}}],
                backend=self.name,
            )
        if self.calls == 4:
            raise ProviderResponseError(
                "second isolated empty response", error_code="MODEL_EMPTY_RESPONSE"
            )
        assert "上一轮模型接口返回了空文本" in json.dumps(kwargs.get("messages"), ensure_ascii=False)
        return ModelResponse(text="两次独立空响应后仍完成。", backend=self.name)


class _IncompleteThenFinalAfterToolBackend(_NativeFakeBackend):
    name = "fake_incomplete_then_final_after_tool_backend"

    def __init__(self):
        self.calls = 0

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                tool_use_blocks=[{"id": "call_auto", "name": 'read_file', "input": {"path": "notes.txt"}}],
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


class _IncompleteWithoutToolBackend(_NativeFakeBackend):
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


class _RepeatedIncompleteAfterToolBackend(_NativeFakeBackend):
    name = "fake_repeated_incomplete_after_tool_backend"

    def __init__(self):
        self.calls = 0

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                tool_use_blocks=[{"id": "call_auto", "name": 'read_file', "input": {"path": "notes.txt"}}],
                backend=self.name,
            )
        raise ProviderResponseError(
            "anthropic_compatible 模型响应未完成（stop_reason=max_tokens）",
            error_code="MODEL_INCOMPLETE_RESPONSE",
            details={"stop_reason": "max_tokens", "partial_text_chars": 4096},
        )


class _LongAppendPromptWindowBackend(_NativeFakeBackend):
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
                tool_use_blocks=[{"id": "call_auto", "name": "write_file",
                    "input": {"path": "data/weekly_data.json",
                              "content": content}}],
                backend=self.name,
            )
        return ModelResponse(text="连续写入后已正常收口。", backend=self.name)


@pytest.mark.xfail(reason="EXEC-31b: text 驱动 fake 待 native 适配")
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
            tool_protocol="native",
            memory_path="memory.jsonl",
        )
        agent = SimpleAgent(cfg, workspace)
        agent.backend = ToolCallingBackend()
        result = agent.run("读取 notes.txt 并总结", save=False)
        assert result.response == "工具执行完成"
        assert result.tool_rounds == 1
        assert "hello tool world" in result.prompt


@pytest.mark.xfail(reason="EXEC-31b: text 驱动 fake 待 native 适配")
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


def test_tool_loop_archive_does_not_replace_owner_execution_cwd(tmp_path):
    service_cwd = tmp_path / "service-cwd"
    service_cwd.mkdir()
    agent = SimpleAgent(
        _text_agent_config(enable_tools=True, memory_path="memory.jsonl"),
        service_cwd,
    )
    task_root = Path(agent.home_paths.owner_home_dir) / "tasks" / "task-a"
    output_dir = task_root / "output"
    work_dir = task_root / "work"
    output_dir.mkdir(parents=True)
    work_dir.mkdir()
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
    assert f"当前工具工作目录（仅供执行定位）: {agent.home_paths.owner_home_dir}" in projected
    assert f"task_output_dir: {output_dir}" not in projected
    assert f"task_work_dir: {work_dir}" not in projected
    assert str(service_cwd.resolve()) not in projected


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


@pytest.mark.xfail(reason="EXEC-31b: text 驱动 fake 待 native 适配")
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
                tool_use_blocks=[{"id": "call_auto", "name": 'run_command', "input": {"command": "echo hello"}}],
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


def test_plain_parallel_project_prompt_recommends_direct_create(tmp_path):
    """大白话并行任务直接推荐首轮可见的创建工具。"""
    agent = SimpleAgent(_text_agent_config(enable_tools=True, memory_path="memory.jsonl"), tmp_path)

    _catalog, recommendations = (
        agent.tools.render_catalog_section(),
        agent.tools.render_recommended_tools_section("请让子代理分别去看不同项目，最后你汇总。"),
    )

    assert "create_subagents" in recommendations


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

    assert "create_subagents" in recommendations


@pytest.mark.xfail(reason="EXEC-31b: text 驱动 fake 待 native 适配")
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
        cfg = _text_agent_config(enable_tools=True, memory_path="memory.jsonl")
        agent = SimpleAgent(cfg, workspace)
        agent.backend = _EmptyAfterToolBackend()
        workspace = agent.effective_workspace_root
        (workspace / "notes.txt").write_text("hello empty model response", encoding="utf-8")

        with pytest.raises(ProviderResponseError, match="流式响应没有文本内容"):
            agent.run("读取 notes 后总结", save=False, allowed_tools=["read_file"])
        assert agent.backend.calls == 3


def test_tool_loop_retries_once_when_final_model_response_is_empty_after_tool():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        cfg = _text_agent_config(enable_tools=True, memory_path="memory.jsonl")
        agent = SimpleAgent(cfg, workspace)
        agent.backend = _EmptyThenFinalAfterToolBackend()
        workspace = agent.effective_workspace_root
        (workspace / "notes.txt").write_text("hello empty repair", encoding="utf-8")

        result = agent.run("读取 notes 后继续总结", save=False, allowed_tools=["read_file"])

        assert result.response == "已根据工具结果继续完成。"
        assert result.executed_tools == ["read_file"]
        assert agent.backend.calls == 3


def test_tool_loop_resets_empty_response_retry_after_successful_model_turn():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        cfg = _text_agent_config(enable_tools=True, memory_path="memory.jsonl")
        agent = SimpleAgent(cfg, workspace)
        agent.backend = _SeparatedEmptyResponsesBackend()
        workspace = agent.effective_workspace_root
        (workspace / "notes.txt").write_text("hello separated empty repairs", encoding="utf-8")

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


@pytest.mark.xfail(reason="EXEC-31b: text 驱动 fake 待 native 适配")
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


@pytest.mark.xfail(reason="EXEC-31b: text 驱动 fake 待 native 适配")
def test_tool_loop_does_not_replay_incomplete_ordinary_chat_response():
    with tempfile.TemporaryDirectory() as td:
        cfg = _text_agent_config(enable_tools=True, memory_path="memory.jsonl")
        agent = SimpleAgent(cfg, Path(td))
        agent.backend = _IncompleteWithoutToolBackend()

        with pytest.raises(ProviderResponseError) as exc_info:
            agent.run("只回答一句普通聊天", save=False)

        assert exc_info.value.error_code == "MODEL_INCOMPLETE_RESPONSE"
        assert agent.backend.calls == 1


@pytest.mark.xfail(reason="EXEC-31b: text 驱动 fake 待 native 适配")
def test_tool_loop_continues_bounded_times_for_repeated_incomplete_response():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        (workspace / "notes.txt").write_text("hello bounded incomplete repair", encoding="utf-8")
        cfg = _text_agent_config(enable_tools=True, memory_path="memory.jsonl")
        agent = SimpleAgent(cfg, workspace)
        agent.backend = _RepeatedIncompleteAfterToolBackend()

        with pytest.raises(ProviderResponseError) as exc_info:
            agent.run("读取 notes 后继续完成", save=False, allowed_tools=["read_file"])

        assert exc_info.value.error_code == "MODEL_INCOMPLETE_RESPONSE"
        # 截断恢复上限 3 次(每次注入「继续输出」上下文不重来):第 1 轮工具成功,
        # calls 2-4 三次重试仍截断,calls 5 达到上限直接失败(真机 deepseek 低输出
        # 上限下 1 次重试不够,放宽后任务不再一轮截断就 BLOCKED)。
        assert agent.backend.calls == 5


@pytest.mark.xfail(reason="EXEC-31b: text 驱动 fake 待 native 适配")
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


@pytest.mark.xfail(reason="EXEC-31b: text 驱动 fake 待 native 适配")
def test_tool_loop_rejects_unclosed_write_then_executes_complete_repair():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        cfg = _text_agent_config(
            enable_tools=True,
            tool_protocol="native",
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


@pytest.mark.xfail(reason="EXEC-31b: text 驱动 fake 待 native 适配")
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


@pytest.mark.xfail(reason="EXEC-31b: text 驱动 fake 待 native 适配")
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


@pytest.mark.xfail(reason="EXEC-31b: text 驱动 fake 待 native 适配")
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


@pytest.mark.xfail(reason="EXEC-31b: text 驱动 fake 待 native 适配")
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


@pytest.mark.xfail(reason="EXEC-31b: native 下工具结果经 IR 消息不回显 prompt 文本, 断言待适配")
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


@pytest.mark.xfail(reason="EXEC-31b: native 下工具结果经 IR 消息不回显 prompt 文本, 断言待适配")
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


@pytest.mark.xfail(reason="EXEC-31b: native 下工具结果经 IR 消息不回显 prompt 文本, 断言待适配")
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


@pytest.mark.xfail(reason="EXEC-31b: native 下派工/回执路径行为差异, 待适配")
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
            register_with=agent,
        )
        dry_dispatch = execute_registry_test_call(
            agent.tools,
            "dispatch_subagents",
            {"dry_run": True, "max_runners": 1},
            call_id="dry-dispatch-created-subagents",
            register_with=agent,
        )
        rejected_internal_switch = execute_registry_test_call(
            agent.tools,
            "dispatch_subagents",
            {"start_runners": True, "dry_run": True},
            call_id="reject-internal-dispatch-switch",
            register_with=agent,
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


@pytest.mark.xfail(reason="EXEC-31b: native 下派工/回执路径行为差异, 待适配")
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


def test_create_subagents_accepts_explicit_external_write_target_and_auto_starts(monkeypatch):
    """LLM: explicit user output dirs are allowed and creation uses host-owned auto-start."""
    monkeypatch.setattr(
        "agent_py_agent.agent.agent_core.orchestration.lifecycle.auto_start_tasks",
        lambda _agent, tasks, _params: {
            "status": "started",
            "run_ids": [task.id for task in tasks],
            "started_run_ids": [task.id for task in tasks],
        },
    )
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        external_dir = workspace.parent / "external-target"
        cfg = _text_agent_config(
            enable_tools=True,
            memory_path="memory.jsonl",
            subagent_workspace="subs",
            max_subagents=3,
            # B 切片：测工具本体不走托管权威链（带 home 会被推断 MANAGED，
            # 未登记 run 的测试调用会被权威门按契约拦截）。
            execution_mode="local_unmanaged",
        )
        agent = SimpleAgent(cfg, workspace)

        result = execute_registry_test_call(
            agent.tools,
            "create_subagents",
            {
                "goal": "创建一个 txt 文件",
                "allowed_tools": ["read_file", "write_file"],
                "output_files": [str(external_dir / "result.txt")],
            },
            call_id="create-external-output-subagent",
        )

        assert result.ok
        assert "工作区外" not in result.output
        assert json.loads(result.output)["auto_start"]["status"] == "started"
        assert len(agent.subagents.list_runs()) == 1
        assert agent.subagents.list_runs()[0].attributes["output_files"] == [
            str(external_dir / "result.txt")
        ]


@pytest.mark.xfail(reason="EXEC-31b: native 下派工/回执路径行为差异, 待适配")
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


@pytest.mark.xfail(reason="EXEC-31b: native 下派工/回执路径行为差异, 待适配")
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

        assert result.response == "这次回复没有完整生成，其中附带的操作没有执行。请重试查看结果。"
        assert "[TOOL_CALL]" not in result.response
        assert result.tool_rounds == 1
        assert result.runtime_status == "unfinished"
        assert result.runtime_reason == "TOOL_ROUND_LIMIT_REACHED"
        assert result.runtime_source == "tool_loop"
        assert agent.backend.calls == 4


def test_tool_round_limit_does_not_schedule_ordinary_task_resume(tmp_path):
    """普通任务轮限只诚实交接，不创建宿主定时模型轮询。"""
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
    thread = agent.conversation_store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "chat-limit",
            "channel_user_id": "user-1",
        }
    )
    agent.conversation_store.tasks.bind(
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
            "conversation_request_id": "gwreq-limit",
        },
        source="cli_gateway",  # 前台形态之一(前缀匹配),固定创建路径语义
    )

    assert result.runtime_status == "unfinished"
    assert result.runtime_reason == "TOOL_ROUND_LIMIT_REACHED"
    link = next(
        item
        for item in agent.conversation_store.tasks.list(thread.thread_id)
        if item.task_id == "task-limit"
    )
    assert link.status == "active"
    assert agent.conversation_store.progress.list(enabled_only=True) == []


def test_active_goal_continuation_ignores_legacy_ordinary_policy(tmp_path):
    """轮限承诺只认 exact Goal；遗留普通 policy 不再取得续跑权。"""
    from types import SimpleNamespace

    from agent_py_agent.agent.agent_core._tool_loop_service import (
        _active_goal_continuation_available,
    )

    (tmp_path / "notes.txt").write_text("hello", encoding="utf-8")
    agent = SimpleAgent(
        _text_agent_config(enable_tools=True, memory_path="memory.jsonl"),
        tmp_path,
    )
    thread = agent.conversation_store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "chat-resume-signal",
            "channel_user_id": "user-1",
        }
    )
    agent.conversation_store.tasks.bind(
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

    assert _active_goal_continuation_available(agent, params()) is False
    # 编号只是旧绑定；目标不存在时不能承诺自动续跑。
    assert (
        _active_goal_continuation_available(
            agent, params(task_attributes={**params().task_attributes, "thread_goal_id": "goal-1"})
        )
        is False
    )

    # 生产语义:同一任务同一时刻只有一个 ordinary_task_resume policy(创建后每次
    # 收口 expedite 复用),预算随 metadata 递增——这里用 mark_progress_reported
    # 更新同一 policy 模拟真实状态。
    policy = agent.conversation_store.progress.create(
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
    assert _active_goal_continuation_available(agent, params()) is False
    agent.conversation_store.progress.mark_reported(
        policy.policy_id, metadata_updates={"resume_used": 2}
    )
    assert _active_goal_continuation_available(agent, params()) is False
    agent.conversation_store.progress.mark_reported(
        policy.policy_id, metadata_updates={"resume_used": 3}
    )
    assert _active_goal_continuation_available(agent, params()) is False
    agent.conversation_store.progress.disable(policy.policy_id)
    assert (
        _active_goal_continuation_available(agent, params()) is False
    )
    assert _active_goal_continuation_available(object(), params()) is False


def test_cli_tool_limit_promise_requires_exact_active_goal(tmp_path):
    """CLI 轮限收口也只认 exact active Goal，不认普通任务或已结束 Goal。"""
    from types import SimpleNamespace

    from agent_py_agent.agent.agent_core._tool_loop_service import (
        _active_goal_continuation_available,
    )

    (tmp_path / "notes.txt").write_text("hello", encoding="utf-8")
    agent = SimpleAgent(
        _text_agent_config(enable_tools=True, memory_path="memory.jsonl"),
        tmp_path,
    )
    thread = agent.conversation_store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "cli",
            "channel_conversation_id": "req-cli-signal",
            "channel_user_id": "local-agent",
        }
    )

    def params(**overrides) -> ToolLoopExecuteParams:
        base = {
            "task_attributes": {
                "conversation_thread_id": thread.thread_id,
            },
            "context_scope": "default",
            "source": "cli_run",
            "run_id": "run-cli-signal",
            "task_id": "task-cli-signal",
        }
        base.update(overrides)
        return SimpleNamespace(**base)

    # 无 goal: EXEC-39 停即停, 承诺文案必须是「已暂停」。
    assert _active_goal_continuation_available(agent, params()) is False
    # exact active Goal 授权成立。
    agent.conversation_store.goals.create(
        {
            "thread_id": thread.thread_id,
            "objective": "继续推进任务直到完成",
            "task_id": "task-cli-signal",
        }
    )
    assert _active_goal_continuation_available(agent, params()) is True
    # goal 变非 active(complete) → 不承诺续跑。
    goal = agent.conversation_store.goals.load(thread.thread_id, task_id="task-cli-signal")
    agent.conversation_store.goals.update(
        {
            "thread_id": thread.thread_id,
            "goal_id": goal.goal_id,
            "status": "complete",
        }
    )
    assert _active_goal_continuation_available(agent, params()) is False


def test_legacy_ordinary_task_resume_policy_is_retired_without_running(tmp_path):
    """升级前遗留的普通续跑 policy 到期时直接退休，不能执行模型。"""
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
    thread = agent.conversation_store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "chat-limit-budget",
            "channel_user_id": "user-1",
        }
    )
    # 进度账本可以按 task-path 跨 turn 复用，但自动续跑 policy 必须按真实
    # conversation task 寻址；两类身份不能再合并成一个 key。
    task_path = str(tmp_path / "task-limit-workspace")
    agent.conversation_store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-limit",
            "goal": "读取并整理文件",
            "status": "active",
            "task_path": task_path,
        }
    )
    policy = agent.conversation_store.progress.create(
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

    from agent_py_agent.agent.conversation.runtime import (
        _runnable_due_policies,
        _snooze_suppressed_policies,
    )

    runnable, suppressed = _runnable_due_policies(
        agent.conversation_store,
        [policy],
        now=policy.next_due_at + 1,
        agent=agent,
    )
    assert runnable == []
    assert suppressed == [(policy, "removed_ordinary_task_resume_policy")]
    _snooze_suppressed_policies(
        agent.conversation_store,
        suppressed,
        now=policy.next_due_at + 1,
        agent=agent,
    )
    policies = agent.conversation_store.progress.list(enabled_only=False)
    assert len(policies) == 1
    assert not policies[0].enabled
    assert agent.conversation_store.progress.list(enabled_only=True) == []


def test_background_main_agent_no_ordinary_resume_policy(tmp_path):
    """后台普通任务遇到轮限也不创建 Goal、周期策略或隐式续跑 wake。"""
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
    thread = agent.conversation_store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "chat-limit-bg",
            "channel_user_id": "user-1",
        }
    )
    agent.conversation_store.tasks.bind(
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
    assert agent.conversation_store.progress.list(enabled_only=False) == []
    assert agent.conversation_store.goals.list(thread.thread_id) == []
    assert agent.conversation_store.wakes.pending() == []


def test_explicit_goal_turn_finishes_without_todo_overriding_goal(tmp_path):
    from agent_py_agent.agent.agent_core.runtime.owner_roots import runtime_owner_root
    from agent_py_agent.agent.task_progress import write_task_progress

    (tmp_path / "notes.txt").write_text("hello tool world", encoding="utf-8")
    agent = SimpleAgent(
        _text_agent_config(enable_tools=True, memory_path="memory.jsonl"),
        tmp_path,
    )

    class OpenProgressBackend:
        name = "open_progress"

        def probe_tool_capability(self):
            from agent_py_agent.agent.backends.base import ProviderToolCapability, _utc_now_iso

            return ProviderToolCapability(
                provider=self.name, endpoint="local://goal-test", model="", stream=False,
                native_supported=True, evidence="test_native_tools", observed_at=_utc_now_iso(),
            )

        def __init__(self):
            self.calls = 0
            self.prompts: list[str] = []

        def generate(self, prompt: str, on_chunk=None, **kwargs):
            del on_chunk, kwargs
            self.calls += 1
            self.prompts.append(prompt)
            if self.calls == 1:
                return ModelResponse(
                    text="",
                    tool_use_blocks=[{"id": "call_auto", "name": 'read_file', "input": {"path": "notes.txt"}}],
                    backend=self.name,
                )
            return ModelResponse(text="文件已经读取，但验证项仍未完成。", backend=self.name)

    agent.backend = OpenProgressBackend()
    thread = agent.conversation_store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "chat-open-progress",
            "channel_user_id": "user-1",
        }
    )
    # link 已绑定任务目录(模拟 promote 后的真实状态):收口读侧 progress_ledger_id
    # = task-path:<sha256(task_path)>,账本必须立在同一个 key 上(task_progress_tool
    # 产品写侧在 run 内 materialize 之后始终按此 key 记账)。
    task_path = str(tmp_path / "goal-workspace")
    agent.conversation_store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-open-progress",
            "goal": "读取后继续完成验证",
            "status": "active",
            "task_path": task_path,
        }
    )
    goal = agent.conversation_store.goals.create(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-open-progress",
            "objective": "读取后继续完成验证",
        }
    )
    ledger_key = f"task-path:{hashlib.sha256(task_path.encode('utf-8')).hexdigest()[:16]}"
    write_task_progress(
        runtime_owner_root(agent),
        ledger_key,
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
    assert result.runtime_status == "ok"
    assert result.runtime_reason != "TASK_PROGRESS_OPEN"
    current = agent.conversation_store.threads.load(thread.thread_id)
    assert current is not None and current.active_task_ids == ("task-open-progress",)
    assert agent.conversation_store.progress.list(enabled_only=True) == []
    pending = agent.conversation_store.wakes.pending()
    assert len(pending) == 1 and pending[0].root_task_id == "task-open-progress"
    assert pending[0].reason == "thread_goal_continue"


@pytest.mark.parametrize(
    ("source", "save"),
    (("gateway", True), ("gateway", False), ("background_main_agent", False)),
)
def test_ordinary_task_open_progress_gets_one_same_turn_reconciliation(
    tmp_path,
    source,
    save,
):
    from agent_py_agent.agent.agent_core.runtime.owner_roots import runtime_owner_root
    from agent_py_agent.agent.conversation.authority import (
        CONVERSATION_TASK_TURN_ACTIVE_ATTR,
    )
    from agent_py_agent.agent.task_progress import write_task_progress

    agent = SimpleAgent(
        _text_agent_config(enable_tools=True, memory_path="memory.jsonl"),
        tmp_path,
    )

    class OrdinaryTaskBackend(_NativeFakeBackend):
        name = "ordinary_open_progress"

        def __init__(self):
            self.prompts: list[str] = []

        def generate(self, prompt: str, on_chunk=None, **kwargs):
            del on_chunk, kwargs
            self.prompts.append(prompt)
            return ModelResponse(text="当前这轮已经结束。", backend=self.name)

    backend = OrdinaryTaskBackend()
    agent.backend = backend
    thread = agent.conversation_store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "chat-ordinary-open-progress",
            "channel_user_id": "user-1",
        }
    )
    task_path = tmp_path / "ordinary-open-progress-workspace"
    task_path.mkdir()
    agent.conversation_store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-ordinary-open-progress",
            "goal": "完成普通任务",
            "status": "active",
            "task_path": str(task_path),
        }
    )
    from agent_py_agent.agent.agent_core.runtime.task_identity import (
        task_path_progress_ledger_id,
    )

    write_task_progress(
        runtime_owner_root(agent),
        task_path_progress_ledger_id(task_path),
        {"items": [{"id": "verify", "status": "pending", "title": "可选核对"}]},
    )

    result = agent.run(
        "继续处理",
        save=save,
        task_id=(
            "task-ordinary-open-progress"
            if source == "background_main_agent"
            else "request-attempt-ordinary-open-progress"
        ),
        task_attributes={
            "conversation_thread_id": thread.thread_id,
            "conversation_task_id": "task-ordinary-open-progress",
            CONVERSATION_TASK_TURN_ACTIVE_ATTR: True,
        },
        source=source,
    )

    assert len(backend.prompts) == 1
    assert result.response == "当前这轮已经结束。"
    assert result.runtime_status == "ok"
    assert result.runtime_reason == ""
    assert result.runtime_source == ""
    current = agent.conversation_store.threads.load(thread.thread_id)
    assert current is not None and current.active_task_ids == ()
    links = agent.conversation_store.tasks.list(thread.thread_id)
    assert len(links) == 1 and links[0].status == "completed"
    assert agent.conversation_store.progress.list(enabled_only=True) == []
