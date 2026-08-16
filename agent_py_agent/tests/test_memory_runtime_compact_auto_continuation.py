"""Runtime compact auto-continuation tests.
运行时自动 compact 续接测试。"""

from __future__ import annotations

import json
from dataclasses import replace

from agent_py_agent.agent.agent_core._runtime_params import FinalizeContext
from agent_py_agent.agent.agent_core.compact_auto_continuation import (
    build_compact_auto_continue_injection,
    compact_auto_continuation_decision,
)
from agent_py_agent.agent.agent_core.finalization_compact_auto import (
    _should_auto_continue_after_cycle,
    _should_return_after_continuation,
    compact_auto_cycle_fields,
)
from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.agent_core.runtime.loop_support import (
    _pending_deferred_tool_calls,
)
from agent_py_agent.agent.agent_core.runtime_mixin import _compact_auto_continue_params
from agent_py_agent.agent.backends.base import ModelResponse
from agent_py_agent.agent.conversation.authority import (
    CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR,
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.memory_archive.compact_continue_packet import (
    CompactContinuePacketRequest,
    build_compact_continue_packet,
)
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.cli.local_commands import _default_run_recovery_next_actions


class CaptureBackend:
    name = "capture"

    def __init__(self):
        self.prompts: list[str] = []
        self.usages: list[dict[str, int]] = []

    def generate(self, prompt: str, on_chunk=None):
        self.prompts.append(prompt)
        usage = self.usages[min(len(self.prompts) - 1, len(self.usages) - 1)] if self.usages else {}
        return ModelResponse(text=f"capture response {len(self.prompts)}", backend=self.name, usage=usage)


class ContextOverflowThenCaptureBackend:
    name = "context-overflow-then-capture"
    # 窗口必须远大于当前系统提示体量(2026-07 实测续跑 prompt ~14k tokens):此前写死
    # 20k,系统提示随功能增长超过 preflight 阈值后,续跑轮被 preflight 合成响应抢拦,
    # backend 根本收不到第 2 个 prompt,4 个续跑断言全挂。本 backend 的 overflow 靠
    # 硬编码 runtime_status 触发、compact 靠 force_trigger,均不依赖窗口大小。
    context_window_tokens = 60_000

    def __init__(self):
        self.prompts: list[str] = []

    def generate(self, prompt: str, on_chunk=None):
        self.prompts.append(prompt)
        if len(self.prompts) == 1:
            return ModelResponse(
                text="context overflow before completion",
                backend=self.name,
                runtime_status="context_overflow",
                runtime_reason="context_overflow",
                runtime_source="provider_error",
                usage={"input_tokens": 19_500, "output_tokens": 50},
            )
        return ModelResponse(
            text=f"capture response {len(self.prompts)}",
            backend=self.name,
            usage={"input_tokens": 500, "output_tokens": 100},
        )


class RepeatingContextOverflowBackend:
    name = "repeating-context-overflow"
    # 同 ContextOverflowThenCaptureBackend:窗口留足余量,防 preflight 抢拦续跑轮。
    context_window_tokens = 60_000

    def __init__(self):
        self.prompts: list[str] = []

    def generate(self, prompt: str, on_chunk=None):
        self.prompts.append(prompt)
        if len(self.prompts) in {1, 3}:
            return ModelResponse(
                text=f"context overflow checkpoint {len(self.prompts)}",
                backend=self.name,
                runtime_status="context_overflow",
                runtime_reason="context_overflow",
                runtime_source="provider_error",
                usage={"input_tokens": 19_500, "output_tokens": 50},
            )
        if len(self.prompts) == 2:
            return ModelResponse(
                text=(
                    "[TOOL_CALL]\n"
                    '{"tool":"write_file","path":"outputs/compact-repeat.txt","content":"继续后写入一次进展。"}\n'
                    "[/TOOL_CALL]"
                ),
                backend=self.name,
                usage={"input_tokens": 500, "output_tokens": 100},
            )
        return ModelResponse(
            text="重复压缩后完成。",
            backend=self.name,
            usage={"input_tokens": 500, "output_tokens": 100},
        )


def _finalize_context_for_continuation(*, tool_rounds: int, executed_tools: list) -> FinalizeContext:
    return FinalizeContext(
        user_prompt="继续做当前任务",
        final_prompt="",
        final_response=ModelResponse(text="", backend="test"),
        memories=[],
        executed_tools=executed_tools,
        archive_tool_calls=[],
        routed_context=None,
        resume_context_result=None,
        runtime_injections=[],
        compression_snapshot_id="",
        compression_snapshot_path="",
        compression_applied=False,
        request_id="req",
        run_id="run",
        task_id="task",
        source="test",
        do_save=True,
        recovery_task_refs=None,
        recovery_content_paths=None,
        recovery_next_actions=None,
        task_attributes=None,
        tool_rounds=tool_rounds,
        compact_auto_continue_depth=1,
    )


def test_preflight_continuation_can_compact_again_after_tool_progress() -> None:
    ctx = _finalize_context_for_continuation(
        tool_rounds=3,
        executed_tools=[{"tool": "read_file", "success": True}],
    )

    assert _should_return_after_continuation(ctx, {"trigger_source": "preflight"}) is False


def test_preflight_continuation_returns_when_no_tool_progress() -> None:
    ctx = _finalize_context_for_continuation(tool_rounds=0, executed_tools=[])

    assert _should_return_after_continuation(ctx, {"trigger_source": "token_budget"}) is True


def test_preflight_compact_response_continues_even_without_tool_progress() -> None:
    ctx = _finalize_context_for_continuation(tool_rounds=0, executed_tools=[])

    assert _should_return_after_continuation(ctx, {"trigger_source": "preflight"}) is False


def test_repeated_preflight_compact_without_tool_progress_returns_after_limit() -> None:
    ctx = replace(
        _finalize_context_for_continuation(tool_rounds=0, executed_tools=[]),
        compact_auto_no_tool_continue_depth=3,
    )

    assert _should_return_after_continuation(ctx, {"trigger_source": "preflight"}) is True


def test_compact_auto_continue_replaces_previous_session_carrier() -> None:
    params = RunParams(inject=["keep me", "# Compact Auto Continuation\nold"])
    result = type("Result", (), {"tool_rounds": 1, "executed_tools": ["read_file"]})()

    updated = _compact_auto_continue_params(params, "# Compact Auto Continuation\nnew", result)

    assert updated.inject == ["keep me", "# Compact Auto Continuation\nnew"]
    assert updated.compact_auto_no_tool_continue_depth == 0


def test_compact_auto_continue_carries_archive_tool_calls_for_closeout_evidence() -> None:
    read_record = {
        "run_id": "run-1",
        "scoped_call_id": "run-1:1-1",
        "call_id": "1-1",
        "tool": "read_file",
        "parameters": {"path": "/tmp/source/shard-01.md"},
        "ok": True,
    }
    write_record = {
        "run_id": "run-1",
        "scoped_call_id": "run-1:2-1",
        "call_id": "2-1",
        "tool": "write_file",
        "parameters": {"path": "/tmp/output/final_report.md"},
        "ok": True,
    }
    next_read_same_call_id = {
        "run_id": "run-1",
        "scoped_call_id": "run-1:1-1",
        "call_id": "1-1",
        "tool": "read_file",
        "parameters": {"path": "/tmp/source/shard-02.md"},
        "ok": True,
    }
    params = RunParams(carried_archive_tool_calls=[read_record])
    result = type(
        "Result",
        (),
        {
            "tool_rounds": 1,
            "executed_tools": ["write_file"],
            "archive_tool_calls": [read_record, next_read_same_call_id, write_record],
        },
    )()

    updated = _compact_auto_continue_params(params, "# Compact Auto Continuation\nnew", result)

    assert updated.carried_archive_tool_calls == [read_record, next_read_same_call_id, write_record]


def test_compact_auto_continue_carries_pending_deferred_calls_from_continue_packet() -> None:
    first_pending = {
        "kind": "tool_call",
        "tool": "read_file",
        "parameters": {"tool": "read_file", "path": "data/long.txt", "start_line": 19780, "max_chars": 100000},
        "ok": False,
        "status": "error",
        "error_code": "CONTEXT_COMPACT_DEFERRED",
    }
    second_pending = {
        "kind": "tool_call",
        "tool": "read_file",
        "parameters": {"tool": "read_file", "path": "data/long.txt", "start_line": 22000, "max_chars": 100000},
        "ok": False,
        "status": "error",
        "error_code": "CONTEXT_COMPACT_DEFERRED",
    }
    params = RunParams()
    result = type(
        "Result",
        (),
        {
            "tool_rounds": 1,
            "executed_tools": ["read_file"],
            "archive_tool_calls": [],
            "memory_compact_auto_continue_packet": {
                "pending_deferred_tool_calls": [first_pending, second_pending],
            },
        },
    )()

    updated = _compact_auto_continue_params(params, "# Compact Auto Continuation\nnew", result)

    assert first_pending in updated.carried_archive_tool_calls
    assert second_pending in updated.carried_archive_tool_calls


def test_pending_deferred_tool_calls_keep_only_unexecuted_calls() -> None:
    deferred_first = {
        "tool": "read_file",
        "ok": False,
        "error_code": "CONTEXT_COMPACT_DEFERRED",
        "parameters": {"tool": "read_file", "path": "/tmp/source.txt", "offset": 100, "max_chars": 50},
    }
    successful_first = {
        "tool": "read_file",
        "ok": True,
        "parameters": {"tool": "read_file", "path": "/tmp/source.txt", "offset": 100, "max_chars": 50},
    }
    deferred_second = {
        "tool": "read_file",
        "ok": False,
        "error_code": "CONTEXT_COMPACT_DEFERRED",
        "parameters": {"tool": "read_file", "path": "/tmp/source.txt", "offset": 150, "max_chars": 50},
    }

    pending = _pending_deferred_tool_calls([deferred_first, successful_first, deferred_second])

    assert pending == [{"tool": "read_file", "path": "/tmp/source.txt", "offset": 150, "max_chars": 50}]


def test_continuation_with_tool_progress_continues_after_normal_threshold_compact() -> None:
    ctx = _finalize_context_for_continuation(
        tool_rounds=1,
        executed_tools=[{"tool": "read_file", "success": True}],
    )

    assert _should_auto_continue_after_cycle(
        ctx,
        {"source": "token_budget", "reason": "normal_threshold"},
        {"allowed_to_continue": True},
    ) is True


def test_first_normal_threshold_compact_continues_after_tool_progress() -> None:
    ctx = replace(
        _finalize_context_for_continuation(
            tool_rounds=3,
            executed_tools=["read_file", "write_file"],
        ),
        compact_auto_continue_depth=0,
    )

    assert _should_auto_continue_after_cycle(
        ctx,
        {"source": "token_budget", "reason": "normal_threshold"},
        {"allowed_to_continue": True},
    ) is True


def test_structurally_completed_conversation_task_never_auto_continues() -> None:
    result = type(
        "Result",
        (),
        {
            "response": "已交付",
            "conversation_task_completed": True,
            "memory_compact_auto_allowed_to_continue": True,
            "memory_compact_auto_continue_ready": True,
            "memory_compact_auto_apply_id": "apply-done",
            "memory_compact_auto_continue_packet": {"ready_to_continue": True},
        },
    )()

    decision = compact_auto_continuation_decision(result)

    assert decision.should_continue is False
    assert decision.reason == "turn_complete"


def test_finalization_skips_compact_cycle_after_structured_turn_completion(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            my_agent_home=str(tmp_path / "home"),
        ),
        tmp_path,
    )
    ctx = replace(
        _finalize_context_for_continuation(tool_rounds=0, executed_tools=[]),
        task_attributes={
            "conversation_thread_id": "thread-1",
            "conversation_task_id": "task-1",
            "conversation_task_completed": True,
        },
        final_response=ModelResponse(
            text="已交付",
            backend="test",
            usage={"input_tokens": 19_000, "output_tokens": 100},
        ),
    )

    fields = compact_auto_cycle_fields(agent, ctx, {"turn": 19_100, "active": 19_100})

    assert fields["memory_compact_auto_status"] == "skipped_after_turn_complete"
    assert fields["memory_compact_trigger_reason"] == "turn_complete"
    assert fields["memory_compact_auto_allowed_to_continue"] is False
    assert fields["memory_compact_auto_continue_packet"] == {}


def test_ordinary_conversation_delegates_no_tool_compact_to_transcript(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path)
    ctx = replace(
        _finalize_context_for_continuation(tool_rounds=0, executed_tools=[]),
        context_scope="conversation",
        task_attributes={
            "conversation_thread_id": "thread-1",
            CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR: True,
        },
        final_response=ModelResponse(
            text="[RUN_CONTEXT_PRESSURE]",
            backend="test",
            runtime_status="context_overflow",
            runtime_reason="context_overflow",
            runtime_source="preflight",
            usage={"input_tokens": 19_000, "output_tokens": 10},
        ),
    )

    fields = compact_auto_cycle_fields(agent, ctx, {"turn": 19_010, "active": 19_010})

    assert fields["memory_compact_auto_status"] == "delegated_to_conversation_store"
    assert fields["memory_compact_auto_next_action"] == "compact_conversation_and_retry"
    assert fields["memory_compact_auto_allowed_to_continue"] is False
    assert fields["memory_compact_auto_apply_id"] == ""
    assert not (agent.home_paths.owner_home_dir / "memory_archive" / "compact_applies").exists()


def test_conversation_with_tool_progress_still_uses_single_transcript_compact(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path)
    ctx = replace(
        _finalize_context_for_continuation(tool_rounds=1, executed_tools=["read_file"]),
        context_scope="conversation",
        task_attributes={
            "conversation_thread_id": "thread-1",
            CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR: True,
        },
    )

    fields = compact_auto_cycle_fields(agent, ctx, {"turn": 19_100, "active": 19_100})

    assert fields["memory_compact_auto_status"] == "delegated_to_conversation_store"
    assert fields["memory_compact_auto_allowed_to_continue"] is False
    assert fields["memory_compact_auto_apply_id"] == ""
    assert not (agent.home_paths.owner_home_dir / "memory_archive" / "compact_applies").exists()


def test_compact_continuation_counts_only_new_archive_records_as_progress() -> None:
    carried = {
        "run_id": "run-1",
        "scoped_call_id": "run-1:1-1",
        "tool": "read_file",
        "parameters": {"path": "source.txt"},
        "ok": True,
    }
    params = RunParams(
        carried_archive_tool_calls=[carried],
        compact_auto_no_tool_continue_depth=1,
    )
    result = type(
        "Result",
        (),
        {
            "tool_rounds": 9,
            "executed_tools": ["read_file"],
            "archive_tool_calls": [carried],
        },
    )()

    updated = _compact_auto_continue_params(
        params,
        "# Compact Auto Continuation\ncontinue",
        result,
    )

    assert updated.compact_auto_no_tool_continue_depth == 2


def test_compact_continuation_resets_idle_depth_after_new_archive_record() -> None:
    carried = {
        "run_id": "run-1",
        "scoped_call_id": "run-1:1-1",
        "tool": "read_file",
        "parameters": {"path": "source.txt"},
        "ok": True,
    }
    added = {
        "run_id": "run-1",
        "scoped_call_id": "run-1:2-1",
        "tool": "write_file",
        "parameters": {"path": "output.txt"},
        "ok": True,
    }
    params = RunParams(
        carried_archive_tool_calls=[carried],
        compact_auto_no_tool_continue_depth=2,
    )
    result = type(
        "Result",
        (),
        {
            "tool_rounds": 9,
            "executed_tools": ["read_file", "write_file"],
            "archive_tool_calls": [carried, added],
        },
    )()

    updated = _compact_auto_continue_params(
        params,
        "# Compact Auto Continuation\ncontinue",
        result,
    )

    assert updated.compact_auto_no_tool_continue_depth == 0


def test_compact_deferred_record_is_not_counted_as_progress() -> None:
    deferred = {
        "run_id": "run-1",
        "scoped_call_id": "run-1:1-1",
        "tool": "read_file",
        "parameters": {"path": "source.txt"},
        "ok": False,
        "error_code": "CONTEXT_COMPACT_DEFERRED",
    }
    params = RunParams(compact_auto_no_tool_continue_depth=1)
    result = type(
        "Result",
        (),
        {
            "tool_rounds": 1,
            "executed_tools": [],
            "archive_tool_calls": [deferred],
        },
    )()

    updated = _compact_auto_continue_params(
        params,
        "# Compact Auto Continuation\ncontinue",
        result,
    )

    assert updated.compact_auto_no_tool_continue_depth == 2


def test_run_auto_compact_apply_continues_once_after_continue_packet(tmp_path):
    """LLM: Tests saved auto compact apply performs one guarded continuation turn."""
    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            my_agent_home=str(tmp_path / "home"),
        ),
        tmp_path,
    )
    agent.backend.context_window_tokens = 20

    result = agent.run(
        "验收: auto compact packet exists\n约束: no automatic tool execution\n测试: focused compact runtime test",
        save=True,
        request_id="req-auto-compact",
        run_id="run-auto-compact",
        task_id="run-auto-compact",
        recovery_next_actions=["continue only after reading continue packet"],
    )

    assert result.memory_compact_auto_status == "returned_after_continuation"
    assert result.memory_compact_auto_next_action == "return_result"
    assert result.memory_compact_auto_allowed_to_continue is False
    assert result.memory_compact_auto_continue_ready is False
    assert result.memory_compact_auto_tool_execution == "none"
    assert result.memory_compact_auto_continued is True
    assert result.memory_compact_auto_continued_from_apply_id
    assert "# Compact Auto Continuation" in result.prompt
    assert (agent.home_paths.owner_home_dir / "memory_archive" / "compact_applies").exists()


def test_run_auto_compact_apply_continues_with_home_entries_and_packet(tmp_path):
    home = tmp_path / "home"
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", my_agent_home=str(home), tool_protocol="text"),
        tmp_path,
    )
    backend = ContextOverflowThenCaptureBackend()
    agent.backend = backend
    agent.home_paths.agents_md.write_text("执行制度：每轮先读 AGENTS。\n", encoding="utf-8")
    agent.home_paths.soul_md.write_text("人格：稳住状态继续干。\n", encoding="utf-8")
    agent.home_paths.user_md.write_text("用户偏好：大白话汇报。\n", encoding="utf-8")
    agent.home_paths.memory_md.write_text("关键记忆：不要重做已完成步骤。\n", encoding="utf-8")

    result = agent.run(
        "验收: continuation packet injected\n约束: do not redo completed work\n测试: focused compact auto continuation",
        save=True,
        request_id="req-auto-continuation",
        run_id="run-auto-continuation",
        task_id="run-auto-continuation",
        recovery_next_actions=["continue from compact next_step"],
    )

    assert len(backend.prompts) == 2
    assert result.response == "capture response 2"
    assert result.memory_compact_auto_continued is True
    assert result.memory_compact_auto_continued_from_apply_id
    second_prompt = backend.prompts[1]
    assert "# Home Entry: LONG-TERM WORKING AGREEMENT" in second_prompt
    assert "# Home Entry: ASSISTANT PERSONA" in second_prompt
    assert "# Home Entry: CURRENT USER OR GROUP PROFILE" in second_prompt
    assert "# Home Entry: memory.md" not in second_prompt
    assert "关键记忆：不要重做已完成步骤。" not in second_prompt
    assert second_prompt.index("# Home Entry: LONG-TERM WORKING AGREEMENT") < second_prompt.index(
        "# Compact Auto Continuation"
    )
    assert "继续当前任务的未完成部分" in second_prompt
    assert "Do not redo completed work" in second_prompt


def test_run_auto_compact_continuation_reuses_original_task_workspace(tmp_path):
    home = tmp_path / "home"
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", my_agent_home=str(home), tool_protocol="text"),
        tmp_path,
    )
    backend = ContextOverflowThenCaptureBackend()
    agent.backend = backend

    agent.run(
        "分析 all-agent 项目并写中文报告",
        save=True,
        request_id="req-workspace-continuation",
        run_id="run-workspace-continuation",
    )

    date_roots = list((agent.home_paths.owner_tasks_dir).glob("*/*"))
    task_names = sorted(path.name for path in date_roots if path.is_dir())
    assert task_names == ["分析-all-agent-项目并写中文报告"]
    workspace = json.loads((date_roots[0] / "work" / "run_workspace.json").read_text(encoding="utf-8"))
    assert workspace["run_id"] == "run-workspace-continuation"


def test_run_auto_compact_apply_returns_after_no_tool_continuation(tmp_path):
    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            my_agent_home=str(tmp_path / "home"),
        ),
        tmp_path,
    )
    backend = ContextOverflowThenCaptureBackend()
    agent.backend = backend

    result = agent.run(
        "验收: compact packet exists\n约束: do not redo completed work\n测试: focused compact",
        save=True,
        request_id="req-auto-return-compact",
        run_id="run-auto-return-compact",
        task_id="run-auto-return-compact",
        recovery_next_actions=["continue from compact packet"],
    )

    apply_dir = agent.home_paths.owner_home_dir / "memory_archive" / "runs" / "run-auto-return-compact" / "compact_applies"
    metadata_files = [
        path
        for path in apply_dir.glob("apply-*.json")
        if not any(marker in path.name for marker in (".apply_bundle.", ".restore_refs.", ".work_state_snapshot.", ".self_check"))
    ]
    assert len(backend.prompts) == 2
    assert len(metadata_files) >= 1
    assert result.memory_compact_auto_continued is True
    assert result.memory_compact_auto_continuation_depth == 1
    assert result.memory_compact_auto_status == "returned_after_continuation"


def test_auto_compact_uses_local_prompt_estimate_when_provider_underreports(tmp_path):
    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path)
    agent.backend.context_window_tokens = 200
    ctx = replace(
        _finalize_context_for_continuation(tool_rounds=1, executed_tools=["read_file"]),
        request_id="req-underreported-usage",
        run_id="run-underreported-usage",
        task_id="run-underreported-usage",
        compact_auto_continue_depth=0,
        final_prompt="完整阅读这些材料并继续推进。\n" + ("很长的上下文片段。" * 400),
        final_response=ModelResponse(
            text="我已经记录进度，下一步继续读。",
            backend="underreported-usage",
            usage={"input_tokens": 10, "output_tokens": 5},
        ),
        routed_context=type(
            "RoutedContext",
            (),
            {"matches": [], "required_read_paths": [], "candidate_paths": []},
        )(),
    )

    result = agent._get_services().finalization.finalize(ctx)

    assert result.memory_compact_auto_status == "ready_after_action_guard"
    assert result.memory_compact_auto_continue_ready is True
    assert result.memory_compact_auto_apply_id
    assert result.memory_compact_ratio >= 0.5


def test_run_auto_compact_apply_can_repeat_when_continuation_makes_tool_progress(tmp_path):
    # 本测试构造"连续两次 compact 续接"的压力剧本；显式关闭单轮 PTL retry，
    # 否则第二次溢出会被 PTL 轻量自救（回收旧工具结果重试成功），走不到第二次
    # compact——那是 PTL 的预期收益，但本测试要验证的是 compact 连续续接能力本身
    # （PTL 救不回的场景仍依赖它），PTL 行为由 test_tool_context_ptl_retry.py 覆盖。
    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            enable_tools=True,
            my_agent_home=str(tmp_path / "home"),
            tool_context_ptl_retry_max=0,
        ),
        tmp_path,
    )
    backend = RepeatingContextOverflowBackend()
    agent.backend = backend

    result = agent.run(
        "做一个需要连续续接的长任务，过程中写一条进展记录。",
        save=True,
        request_id="req-repeat-compact",
        run_id="run-repeat-compact",
        task_id="run-repeat-compact",
    )

    apply_dir = agent.home_paths.owner_home_dir / "memory_archive" / "runs" / "run-repeat-compact" / "compact_applies"
    metadata_files = [
        path
        for path in apply_dir.glob("apply-*.json")
        if not any(marker in path.name for marker in (".apply_bundle.", ".restore_refs.", ".work_state_snapshot.", ".self_check"))
    ]
    assert len(backend.prompts) == 4
    assert len(metadata_files) >= 2
    assert result.memory_compact_auto_continued is True
    assert result.memory_compact_auto_continuation_depth == 2
    assert (tmp_path / "outputs" / "compact-repeat.txt").read_text(encoding="utf-8") == "继续后写入一次进展。"


def test_compact_auto_continue_injection_prioritizes_resume_focus_and_captured_refs() -> None:
    packet = {
        "apply_id": "apply-1",
        "continue_mode": "automated_guarded",
        "guard": {"status": "allowed"},
        "next_actions": ["把已有研究笔记合并进最终报告，不要重新派相同调研子代理。"],
        "work_state_snapshot": {
            "goal": "整理多个项目架构报告",
            "current_phase": "compact_apply",
            "next_step": "合并已有研究笔记",
            "changed_files": ["outputs/final-report.md"],
            "read_files": ["notes/openclaw.md", "notes/hermes.md"],
            "task_progress": {
                "summary": "已读 12 个项目，剩余 3 个",
                "next_action": "继续补剩余项目事实",
                "counts": {"total": 15, "done": 12, "in_progress": 1},
                "ref": "/tmp/task_progress/progress.json",
                "active_items": [{"id": "project-13", "title": "补 Hermes", "status": "in_progress"}],
                "recent_done_items": [{"id": "project-12", "title": "OpenClaw", "status": "done", "notes": "已读核心运行时"}],
                "quality_hints": {"messages": ["有些完成项缺 evidence。"]},
            },
            "tool_progress": [
                {
                    "tool": "read_file",
                    "source_path": "notes/hermes.md",
                    "artifact_ref": "run-1:tool-2",
                    "size_bytes": 4096,
                }
            ],
        },
        "artifact_read_hints": [
            {"artifact_ref": "run-1:tool-2", "source_path": "artifacts/search-result.json"}
        ],
        "recommended_read_paths": ["memory_archive/compact_applies/apply-1.work_state_snapshot.json"],
    }

    rendered = build_compact_auto_continue_injection(packet)

    assert "## Resume Focus" in rendered
    assert "把已有研究笔记合并进最终报告" in rendered
    assert "## Already Captured Refs" in rendered
    assert "outputs/final-report.md" in rendered
    assert "notes/openclaw.md" in rendered
    assert "run-1:tool-2" in rendered
    assert "## Task Progress Ledger" in rendered
    assert "full_ledger_ref: /tmp/task_progress/progress.json" in rendered
    assert "逐项事实" in rendered
    assert "project-13" in rendered
    assert "## Exact Tool Output Index" in rendered
    assert "source_path=notes/hermes.md" in rendered
    assert "精确字段" in rendered
    assert "不要先重读 compact 文件" in rendered
    assert "next_path" in rendered
    assert "END" in rendered
    assert "## Recommended Read Paths" not in rendered
    assert "memory_archive/compact_applies/apply-1.work_state_snapshot.json" not in rendered


def test_compact_auto_continue_injection_renders_line_cursor_coverage() -> None:
    rendered = build_compact_auto_continue_injection(
        {
            "apply_id": "apply-line-cursor",
            "plan_id": "plan-line-cursor",
            "resume_focus": {
                "next_action": "继续从下一行读取。",
                "captured_refs": {
                    "full_read_coverage": {
                        "kind": "line_window",
                        "source_path": "logs/big.txt",
                        "covered_until_line": 40,
                        "total_lines": 60,
                        "complete": False,
                    }
                },
            },
        }
    )

    assert "covered_until_line=40" in rendered
    assert "total_lines=60" in rendered
    assert "total_chars=60" not in rendered


def test_compact_auto_continue_injection_shows_existing_child_agents() -> None:
    packet = {
        "apply_id": "apply-children",
        "continue_mode": "automated_guarded",
        "guard": {"status": "allowed"},
        "work_state_snapshot": {
            "goal": "整理多个项目架构报告",
            "current_phase": "compact_apply",
            "next_step": "继续汇总现有子代理结果",
            "runtime_handoff": {
                "agent_tree": {
                    "counts": {"total": 1, "running": 1},
                    "active_agents": [
                        {
                            "run_id": "subagent-child-a",
                            "parent_run_id": "run-parent",
                            "status": "RUNNING",
                            "current_tool": "read_file",
                            "last_progress_summary": "正在读核心模块",
                            "state_ref": "tasks/2026-06-04/demo-task/work/agents/subagent-child-a/canonical_state.json",
                        }
                    ],
                }
            },
        },
    }

    rendered = build_compact_auto_continue_injection(packet)

    assert "## Existing Child Agents" in rendered
    assert "subagent-child-a" in rendered
    assert "正在读核心模块" in rendered
    assert "不要重复 create_subagents" in rendered


def test_compact_auto_continue_injection_shows_completed_child_agents() -> None:
    packet = build_compact_continue_packet(
        CompactContinuePacketRequest(
            metadata={"apply_id": "apply-done-children", "plan_id": "plan-done-children"},
            work_state={
                "goal": "整理多个项目架构报告",
                "phase": "compact_apply",
                "next_step": "汇总已完成子代理报告",
                "next_actions": ["汇总已完成子代理报告"],
                "runtime_handoff": {
                    "agent_tree": {
                        "counts": {"total": 2, "done": 2},
                        "active_agents": [],
                        "recent_agents": [
                            {
                                "run_id": "subagent-child-a",
                                "status": "DONE",
                                "last_progress_summary": "已写 codex-main-report.md",
                            },
                            {
                                "run_id": "subagent-child-b",
                                "status": "DONE",
                                "last_progress_summary": "已写 hermes-agent-main-report.md",
                            },
                        ],
                    }
                },
            },
            consistency={"status": "ok"},
            action_guard={"allowed_to_continue": True, "status": "allowed"},
            handoff={},
            recommended_read_paths=[],
            next_actions=["汇总已完成子代理报告"],
            main_context_bundle={},
        )
    )

    rendered = build_compact_auto_continue_injection(packet)

    assert packet["work_state_snapshot"]["runtime_handoff"]["agent_tree"]["recent_agents"]
    assert "## Existing Child Agents" in rendered
    assert "subagent-child-a" in rendered
    assert "DONE" in rendered
    assert "汇总已完成子代理报告" in rendered


def test_compact_continue_packet_carries_task_state_refs_for_repeat_resume() -> None:
    packet = build_compact_continue_packet(
        CompactContinuePacketRequest(
            metadata={"apply_id": "apply-refs", "plan_id": "plan-refs"},
            work_state={
                "goal": "整理多个项目架构报告",
                "phase": "compact_apply",
                "next_step": "合并已有研究笔记",
                "next_actions": ["先合并已有笔记，再补缺口。"],
                "changed_files": ["outputs/final-report.md"],
                "read_files": ["notes/openclaw.md"],
                "artifact_refs": [
                    {
                        "kind": "tool_output",
                        "path": "artifacts/search-result.json",
                        "source_path": "notes/search-source.md",
                        "tool": "web_search",
                        "scoped_call_id": "run-1:tool-2",
                    }
                ],
                "tool_progress": [
                    {
                        "tool": "web_search",
                        "source_path": "notes/search-source.md",
                        "artifact_ref": "run-1:tool-2",
                        "size_bytes": 2048,
                    }
                ],
            },
            consistency={"status": "ok"},
            action_guard={"allowed_to_continue": True, "status": "allowed"},
            handoff={},
            recommended_read_paths=["memory_archive/compact_applies/apply-refs.work_state_snapshot.json"],
            next_actions=["先合并已有笔记，再补缺口。"],
            main_context_bundle={},
        )
    )

    focus = packet["resume_focus"]
    refs = packet["work_state_snapshot"]["captured_refs"]

    assert focus["next_action"] == "先合并已有笔记，再补缺口。"
    assert "outputs/final-report.md" in refs["changed_files"]
    assert "notes/openclaw.md" in refs["read_files"]
    assert refs["artifact_refs"][0]["artifact_ref"] == "artifacts/search-result.json"
    assert refs["artifact_refs"][0]["source_path"] == "notes/search-source.md"
    assert packet["work_state_snapshot"]["tool_progress"][0]["source_path"] == "notes/search-source.md"


def test_compact_continue_packet_prioritizes_full_read_cursor(tmp_path: Path) -> None:
    first = tmp_path / "read-1.json"
    second = tmp_path / "read-2.json"
    first.write_text(
        '{"content":"[char-window offset=0 chars=100 total_chars=500]\\nPARTIAL view only"}',
        encoding="utf-8",
    )
    second.write_text(
        '{"content":"[char-window offset=100 chars=100 total_chars=500]\\nPARTIAL view only"}',
        encoding="utf-8",
    )
    packet = build_compact_continue_packet(
        CompactContinuePacketRequest(
            metadata={"apply_id": "apply-full-read", "plan_id": "plan-full-read"},
            work_state={
                "goal": "完整读完 data/big.txt，按顺序慢慢读。",
                "phase": "compact_apply",
                "next_step": "继续读取 data/big.txt 的 offset=200，同时搜索章节标记。",
                "next_actions": ["继续读取 data/big.txt 的 offset=200，同时搜索章节标记。"],
                "task_progress": {
                    "summary": "已读到 offset=200",
                    "next_action": "继续读取 data/big.txt 的 offset=200，同时搜索章节标记。",
                    "ref": str(tmp_path / "progress.json"),
                    "coverage": {
                        "coverage_requirement": "full_source_read",
                        "targets": [
                            {
                                "id": "data/big.txt",
                                "source_ref": "data/big.txt",
                                "coverage_kind": "full_source_read",
                            }
                        ],
                    },
                },
                "artifact_refs": [
                    {
                        "kind": "tool_output",
                        "path": str(first),
                        "source_path": "data/big.txt",
                        "tool": "read_file",
                        "ok": True,
                        "status": "ok",
                        "read_window": {
                            "kind": "char_window",
                            "offset": 0,
                            "chars": 100,
                            "next_offset": 100,
                            "total_chars": 500,
                        },
                    },
                    {
                        "kind": "tool_output",
                        "path": str(second),
                        "source_path": "data/big.txt",
                        "tool": "read_file",
                        "ok": True,
                        "status": "ok",
                        "read_window": {
                            "kind": "char_window",
                            "offset": 100,
                            "chars": 100,
                            "next_offset": 200,
                            "total_chars": 500,
                        },
                    },
                ],
            },
            consistency={"status": "ok"},
            action_guard={"allowed_to_continue": True, "status": "allowed"},
            handoff={},
            recommended_read_paths=[],
            next_actions=["继续读取 data/big.txt 的 offset=200，同时搜索章节标记。"],
            main_context_bundle={},
        )
    )

    focus = packet["resume_focus"]
    captured = focus["captured_refs"]

    assert focus["next_action"].startswith("继续完整阅读 data/big.txt")
    assert "先沉淀上一段已读出的关键事实" in focus["next_action"]
    assert 'read_file(path="data/big.txt", offset=200, max_chars=50000)' in focus["next_action"]
    assert "同时搜索章节标记" not in focus["next_action"]
    assert str(tmp_path / "progress.json") in focus["next_action"]
    assert "不要回到 offset=0" in " ".join(focus["do_not_repeat"])
    assert captured["artifact_ref_count"] == 2
    assert captured["omitted_artifact_ref_count"] == 0
    assert captured["full_read_coverage"]["covered_until"] == 200
    assert packet["work_state_snapshot"]["task_progress"]["ref"] == str(tmp_path / "progress.json")


def test_compact_continue_packet_keeps_task_action_without_full_read_contract(tmp_path: Path) -> None:
    first = tmp_path / "read-1.json"
    second = tmp_path / "read-2.json"
    first.write_text(
        '{"content":"[char-window offset=0 chars=100 total_chars=500]\\nPARTIAL view only"}',
        encoding="utf-8",
    )
    second.write_text(
        '{"content":"[char-window offset=100 chars=100 total_chars=500]\\nPARTIAL view only"}',
        encoding="utf-8",
    )
    packet = build_compact_continue_packet(
        CompactContinuePacketRequest(
            metadata={"apply_id": "apply-read-cursor", "plan_id": "plan-read-cursor"},
            work_state={
                "goal": "读取 data/big.txt 并整理报告。",
                "phase": "compact_apply",
                "next_step": "继续读取 data/big.txt 的 offset=100。",
                "next_actions": ["继续读取 data/big.txt 的 offset=100。"],
                "task_progress": {
                    "summary": "旧进度只记到 offset=100",
                    "next_action": "继续读取 data/big.txt 的 offset=100。",
                    "ref": str(tmp_path / "progress.json"),
                },
                "artifact_refs": [
                    {
                        "kind": "tool_output",
                        "path": str(first),
                        "source_path": "data/big.txt",
                        "tool": "read_file",
                        "ok": True,
                        "status": "ok",
                        "read_window": {
                            "kind": "char_window",
                            "offset": 0,
                            "chars": 100,
                            "next_offset": 100,
                            "total_chars": 500,
                        },
                    },
                    {
                        "kind": "tool_output",
                        "path": str(second),
                        "source_path": "data/big.txt",
                        "tool": "read_file",
                        "ok": True,
                        "status": "ok",
                        "read_window": {
                            "kind": "char_window",
                            "offset": 100,
                            "chars": 100,
                            "next_offset": 200,
                            "total_chars": 500,
                        },
                    },
                ],
            },
            consistency={"status": "ok"},
            action_guard={"allowed_to_continue": True, "status": "allowed"},
            handoff={},
            recommended_read_paths=[],
            next_actions=["继续读取 data/big.txt 的 offset=100。"],
            main_context_bundle={},
        )
    )

    focus = packet["resume_focus"]

    assert focus["next_action"] == "继续读取 data/big.txt 的 offset=100。"
    assert focus["captured_refs"]["full_read_coverage"]["covered_until_offset"] == 200
    assert "按机器游标继续" not in " ".join(focus["do_not_repeat"])


def test_compact_continue_packet_carries_read_coverage_beyond_clipped_tool_progress() -> None:
    packet = build_compact_continue_packet(
        CompactContinuePacketRequest(
            metadata={"apply_id": "apply-coverage", "plan_id": "plan-coverage"},
            work_state={
                "goal": "读取 data/big.txt 并整理报告。",
                "phase": "compact_apply",
                "next_step": "继续读取 data/big.txt 的 offset=60000。",
                "next_actions": ["继续读取 data/big.txt 的 offset=60000。"],
                "read_coverage": {
                    "schema_version": 1,
                    "source_count": 1,
                    "primary": {
                        "kind": "char_window",
                        "source_path": "data/big.txt",
                        "covered_until": 60000,
                        "covered_until_offset": 60000,
                        "total": 70000,
                        "total_chars": 70000,
                        "next_offset": 60000,
                        "complete": False,
                        "range_count": 60,
                        "omitted_range_count": 48,
                    },
                },
                "tool_progress": [
                    {"tool": "read_file", "source_path": "data/big.txt", "offset": offset, "next_offset": offset + 1000}
                    for offset in range(12000, 60000, 1000)
                ],
            },
            consistency={"status": "ok"},
            action_guard={"allowed_to_continue": True, "status": "allowed"},
            handoff={},
            recommended_read_paths=[],
            next_actions=["继续读取 data/big.txt 的 offset=60000。"],
            main_context_bundle={},
        )
    )

    snapshot = packet["work_state_snapshot"]

    assert snapshot["tool_progress"][0]["offset"] == 12000
    assert snapshot["read_coverage"]["primary"]["covered_until_offset"] == 60000
    assert packet["resume_focus"]["next_action"] == "继续读取 data/big.txt 的 offset=60000。"
    assert packet["resume_focus"]["captured_refs"]["full_read_coverage"]["covered_until_offset"] == 60000


def test_compact_continue_packet_prioritizes_incomplete_source_over_completed_primary() -> None:
    packet = build_compact_continue_packet(
        CompactContinuePacketRequest(
            metadata={"apply_id": "apply-multi-source", "plan_id": "plan-multi-source"},
            work_state={
                "goal": "分析多个项目源码。",
                "phase": "compact_apply",
                "next_step": "继续分析剩余项目。",
                "next_actions": ["继续分析剩余项目。"],
                "read_coverage": _multi_source_read_coverage(),
                "tool_progress": [],
            },
            consistency={"status": "ok"},
            action_guard={"allowed_to_continue": True, "status": "allowed"},
            handoff={},
            recommended_read_paths=[],
            next_actions=["继续分析剩余项目。"],
            main_context_bundle={},
        )
    )

    focus = packet["resume_focus"]
    snapshot = packet["work_state_snapshot"]

    assert snapshot["read_coverage"]["incomplete_source_count"] == 2
    assert focus["captured_refs"]["incomplete_source_coverage"][0]["source_path"] == "/repo/project-b/core.py"
    assert focus["next_action"] == "继续分析剩余项目。"
    assert [item["source_path"] for item in focus["captured_refs"]["incomplete_source_coverage"]] == [
        "/repo/project-b/core.py",
        "/repo/project-c/routes.py",
    ]


def _multi_source_read_coverage() -> dict[str, object]:
    completed = _char_coverage_source("/repo/project-a/README.md", covered=1000, total=1000)
    incomplete = [
        _char_coverage_source("/repo/project-b/core.py", covered=500, total=2000),
        _line_coverage_source("/repo/project-c/routes.py", covered=40, total=120),
    ]
    return {
        "schema_version": 1,
        "source_count": 3,
        "incomplete_source_count": len(incomplete),
        "primary": completed,
        "sources": [completed, *incomplete],
        "incomplete_sources": incomplete,
    }


def _char_coverage_source(source_path: str, *, covered: int, total: int) -> dict[str, object]:
    return {
        "kind": "char_window",
        "source_path": source_path,
        "covered_until": covered,
        "covered_until_offset": covered,
        "total": total,
        "total_chars": total,
        "next_offset": 0 if covered >= total else covered,
        "complete": covered >= total,
    }


def _line_coverage_source(source_path: str, *, covered: int, total: int) -> dict[str, object]:
    return {
        "kind": "line_window",
        "source_path": source_path,
        "covered_until": covered,
        "covered_until_line": covered,
        "total": total,
        "total_lines": total,
        "next_start_line": 0 if covered >= total else covered + 1,
        "complete": covered >= total,
    }


def test_compact_continue_packet_does_not_advance_cursor_for_failed_read(tmp_path: Path) -> None:
    first = tmp_path / "read-1.json"
    first.write_text(
        '{"content":"[char-window offset=0 chars=100 total_chars=500]\\nPARTIAL view only"}',
        encoding="utf-8",
    )
    packet = build_compact_continue_packet(
        CompactContinuePacketRequest(
            metadata={"apply_id": "apply-read-cursor-failed", "plan_id": "plan-read-cursor-failed"},
            work_state={
                "goal": "读取 data/big.txt 并整理报告。",
                "phase": "compact_apply",
                "next_step": "继续读取 data/big.txt。",
                "artifact_refs": [
                    {
                        "kind": "tool_output",
                        "path": str(first),
                        "source_path": "data/big.txt",
                        "tool": "read_file",
                        "ok": True,
                        "status": "ok",
                        "read_window": {
                            "kind": "char_window",
                            "offset": 0,
                            "chars": 100,
                            "next_offset": 100,
                            "total_chars": 500,
                        },
                    },
                    {
                        "kind": "tool_output",
                        "source_path": "data/big.txt",
                        "tool": "read_file",
                        "ok": False,
                        "status": "error",
                        "error_code": "CONTEXT_COMPACT_DEFERRED",
                        "parameters": {"path": "data/big.txt", "offset": 100, "max_chars": 100},
                    },
                ],
            },
            consistency={"status": "ok"},
            action_guard={"allowed_to_continue": True, "status": "allowed"},
            handoff={},
            recommended_read_paths=[],
            next_actions=[],
            main_context_bundle={},
        )
    )

    focus = packet["resume_focus"]

    assert focus["next_action"] == "继续读取 data/big.txt。"
    assert focus["captured_refs"]["full_read_coverage"]["covered_until_offset"] == 100


def test_compact_continue_packet_treats_missing_offset_as_zero_for_artifact_cursor() -> None:
    packet = build_compact_continue_packet(
        CompactContinuePacketRequest(
            metadata={"apply_id": "apply-cursor-missing-offset", "plan_id": "plan-cursor-missing-offset"},
            work_state={
                "goal": "继续读 data/big.txt。",
                "phase": "compact_apply",
                "next_step": "继续读取 data/big.txt。",
                "artifact_refs": [
                    {
                        "kind": "tool_output",
                        "source_path": "data/big.txt",
                        "tool": "read_file",
                        "ok": True,
                        "status": "ok",
                        "parameters": {"path": "data/big.txt", "max_chars": 8000},
                    },
                    {
                        "kind": "tool_output",
                        "source_path": "data/big.txt",
                        "tool": "read_file",
                        "ok": True,
                        "status": "ok",
                        "parameters": {"path": "data/big.txt", "offset": 8000, "max_chars": 100000},
                    },
                ],
            },
            consistency={"status": "ok"},
            action_guard={"allowed_to_continue": True, "status": "allowed"},
            handoff={},
            recommended_read_paths=[],
            next_actions=[],
            main_context_bundle={},
        )
    )

    focus = packet["resume_focus"]

    assert focus["next_action"] == "继续读取 data/big.txt。"
    assert focus["captured_refs"]["full_read_coverage"]["covered_until_offset"] == 108000


def test_compact_continue_packet_prioritizes_full_read_line_cursor(tmp_path: Path) -> None:
    packet = build_compact_continue_packet(
        CompactContinuePacketRequest(
            metadata={"apply_id": "apply-full-read-lines", "plan_id": "plan-full-read-lines"},
            work_state={
                "goal": "完整读完 data/line-log.txt，按顺序慢慢读。",
                "phase": "compact_apply",
                "next_step": "继续读取 data/line-log.txt。",
                "task_progress": {
                    "summary": "已读到第 40 行",
                    "next_action": "继续读取 data/line-log.txt。",
                    "ref": str(tmp_path / "progress.json"),
                    "coverage": {
                        "coverage_requirement": "full_source_read",
                        "targets": [
                            {
                                "id": "data/line-log.txt",
                                "source_ref": "data/line-log.txt",
                                "coverage_kind": "full_source_read",
                            }
                        ],
                    },
                },
                "artifact_refs": [
                    _line_read_ref(tmp_path, {"name": "read-lines-1", "start": 1, "end": 2, "next": 3, "max": 40}),
                    _line_read_ref(tmp_path, {"name": "read-lines-2", "start": 3, "end": 40, "next": 41, "max": 400}),
                ],
            },
            consistency={"status": "ok"},
            action_guard={"allowed_to_continue": True, "status": "allowed"},
            handoff={},
            recommended_read_paths=[],
            next_actions=["继续读取 data/line-log.txt。"],
            main_context_bundle={},
        )
    )

    focus = packet["resume_focus"]
    captured = focus["captured_refs"]

    assert focus["next_action"].startswith("继续完整阅读 data/line-log.txt")
    assert 'read_file(path="data/line-log.txt", start_line=41, max_chars=50000)' in focus["next_action"]
    assert "已连续覆盖 40/60 行" in focus["next_action"]
    assert "不要回到 start_line=1" in focus["next_action"]
    assert captured["full_read_coverage"]["kind"] == "line_window"
    assert captured["full_read_coverage"]["covered_until_line"] == 40
    assert captured["full_read_coverage"]["total_lines"] == 60


def _line_read_ref(tmp_path: Path, page: dict[str, int | str]) -> dict:
    artifact = tmp_path / f"{page['name']}.json"
    start = int(page["start"])
    max_chars = int(page["max"])
    artifact.write_text(
        json.dumps({"content": "\n".join(_line_read_content(page))}),
        encoding="utf-8",
    )
    return {
        "kind": "tool_output",
        "path": str(artifact),
        "source_path": "data/line-log.txt",
        "tool": "read_file",
        "ok": True,
        "status": "ok",
        "parameters": {"path": "data/line-log.txt", "start_line": start, "max_chars": max_chars},
        "read_window": {
            "kind": "line_window",
            "start_line": start,
            "end_line": int(page["end"]),
            "next_start_line": int(page["next"]),
            "total_lines": 60,
        },
    }


def _line_read_content(page: dict[str, int | str]) -> list[str]:
    return [
        f"{page['start']}: line {page['start']}",
        f"{page['end']}: line {page['end']}",
        f"... 已截断；PARTIAL view only; 这不是完整文件。 total_lines=60; next_start_line={page['next']}; limit_chars={page['max']}。",
    ]


def test_compact_continue_packet_keeps_captured_refs_compact() -> None:
    artifact_refs = [
        {
            "kind": "tool_output",
            "path": f"artifacts/read-{index}.json",
            "source_path": "data/big.txt",
            "tool": "read_file",
            "scoped_call_id": f"run-1:{index}",
        }
        for index in range(20)
    ]

    packet = build_compact_continue_packet(
        CompactContinuePacketRequest(
            metadata={"apply_id": "apply-many-refs", "plan_id": "plan-many-refs"},
            work_state={
                "goal": "完整读完 data/big.txt，按顺序慢慢读。",
                "phase": "compact_apply",
                "next_step": "继续读取 data/big.txt。",
                "artifact_refs": artifact_refs,
            },
            consistency={"status": "ok"},
            action_guard={"allowed_to_continue": True, "status": "allowed"},
            handoff={},
            recommended_read_paths=[],
            next_actions=["继续读取 data/big.txt。"],
            main_context_bundle={},
        )
    )

    captured = packet["resume_focus"]["captured_refs"]

    assert captured["artifact_ref_count"] == 20
    assert captured["omitted_artifact_ref_count"] == 12
    assert len(captured["artifact_refs"]) == 8
    assert captured["artifact_refs"][0]["artifact_ref"] == "artifacts/read-12.json"


def test_compact_continue_packet_preserves_recorded_next_actions() -> None:
    packet = build_compact_continue_packet(
        CompactContinuePacketRequest(
            metadata={"apply_id": "apply-reader-first", "plan_id": "plan-reader-first"},
            work_state={
                "goal": "整理多个项目架构报告",
                "phase": "compact_apply",
                "next_step": "合并已有研究笔记",
                "next_actions": ["如需恢复本次单轮 run，先查看 memory-resume 和 LocalStore 记录。"],
                "changed_files": [],
                "read_files": ["notes/openclaw.md"],
                "artifact_refs": [],
            },
            consistency={"status": "ok"},
            action_guard={"allowed_to_continue": True, "status": "allowed"},
            handoff={},
            recommended_read_paths=["memory_archive/compact_applies/apply.work_state_snapshot.json"],
            next_actions=["如需恢复本次单轮 run，先查看 memory-resume 和 LocalStore 记录。"],
            main_context_bundle={},
        )
    )

    assert packet["resume_focus"]["next_action"] == "如需恢复本次单轮 run，先查看 memory-resume 和 LocalStore 记录。"
    assert packet["resume_focus"]["next_actions"] == ["如需恢复本次单轮 run，先查看 memory-resume 和 LocalStore 记录。"]


def test_default_run_recovery_next_actions_are_action_first() -> None:
    actions = _default_run_recovery_next_actions()

    assert actions
    assert "继续当前用户请求" in actions[0]
    assert all("memory-resume" not in action for action in actions)
    assert all("LocalStore" not in action for action in actions)


def test_run_auto_compact_apply_continues_with_optional_work_notes_missing(tmp_path):
    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            my_agent_home=str(tmp_path / "home"),
        ),
        tmp_path,
    )
    backend = ContextOverflowThenCaptureBackend()
    agent.backend = backend

    result = agent.run("请生成足够长的 compact 提示触发内容", save=True, request_id="req-blocked-continuation")

    assert len(backend.prompts) == 2
    assert result.memory_compact_auto_continued is True
    assert result.memory_compact_auto_continued_from_apply_id
    assert "# Compact Auto Continuation" in backend.prompts[1]


def test_run_auto_compact_normal_final_returns_without_auto_continuation(tmp_path):
    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            my_agent_home=str(tmp_path / "home"),
            prompt_files=[],
            enable_tools=False,
        ),
        tmp_path,
    )
    backend = CaptureBackend()
    agent.backend = backend
    # 窗口/用量同比放大(比率≈95% 不变):本测钉的是"贴线用量下正常收尾→建议压缩但不续跑",
    # 不钉初始目录的绝对 token 数——工具目录合法增长(如 service_window_seconds 声明)不该
    # 让预跑溢出闸抢在首次模型调用前触发,把本测变成 0 次调用的另一条路。
    agent.backend.context_window_tokens = 21_000
    backend.usages = [{"input_tokens": 20_000, "output_tokens": 100}]

    result = agent.run("请整理材料，写完后直接汇报完成。", save=True, request_id="req-normal-final-compact")

    assert len(backend.prompts) == 1
    assert result.memory_compact_suggested is True
    assert result.memory_compact_auto_status == "applied_return_result"
    assert result.memory_compact_auto_next_action == "return_result_after_compact"
    assert result.memory_compact_auto_allowed_to_continue is False
    assert result.memory_compact_auto_continued is False


# ---------------------------------------------------------------------------
# H1: compact 续跑必须从 carried archive 记录重建运行时状态（one_shot/tool_rounds/
# tool_context/executed_tools），否则续跑「失忆重来」会重复创建子代理、重置工具预算。
# ---------------------------------------------------------------------------


def _seed_for_carried(
    carried: list[dict[str, object]],
    active_turn_user_inputs: list[dict[str, object]] | None = None,
    *,
    source_protocol: str = "text",
):
    from agent_py_agent.agent.agent_core.runtime.loop_models import (
        RuntimeLoopParams,
        RuntimeToolLoopSeed,
    )
    from agent_py_agent.tests._tool_runtime_harness import (
        make_test_protocol_snapshot,
        runtime_snapshot_for_model_specs,
    )

    runtime_snapshot = runtime_snapshot_for_model_specs((), run_id="test-run")
    protocol_snapshot = make_test_protocol_snapshot(
        run_id="test-run",
        source_protocol=source_protocol,
    )

    loop_params = RuntimeLoopParams(
        user_prompt="继续做当前任务",
        root_user_prompt="继续做当前任务",
        memories=[],
        runtime_injections=[],
        routed_context=None,
        resume_context_section="",
        carried_archive_tool_calls=carried,
        carried_active_turn_user_inputs=active_turn_user_inputs,
    )
    return RuntimeToolLoopSeed(
        params=loop_params,
        memories=[],
        tool_catalog_section="",
        tool_recommendations_section="",
        tool_runtime_snapshot=runtime_snapshot,
        tool_protocol_snapshot=protocol_snapshot,
    )


def test_compact_continuation_rebuilds_runtime_state_from_carried_records():
    from types import SimpleNamespace

    from agent_py_agent.agent.agent_core.runtime.loop_support import _tool_loop_execute_params

    create_payload = {
        "tool": "create_subagents",
        "goal": "分析 X 项目",
        "items": [{"goal": "分析 X 的架构"}, {"goal": "分析 X 的测试"}],
    }
    carried = [
        {"tool": "create_subagents", "ok": True, "parameters": create_payload},
        {
            "tool": "read_file",
            "ok": True,
            "parameters": {"tool": "read_file", "path": "/src/a.py"},
            "output_preview": "def a(): ...",
            "scoped_call_id": "run-1:1-2",
        },
        # 真实工具失败记录：不计入 executed_tools、不进 one_shot，但仍占一轮预算。
        {"tool": "read_file", "ok": False, "parameters": {"tool": "read_file"}},
    ]

    loop_params = _tool_loop_execute_params(SimpleNamespace(), _seed_for_carried(carried))

    # executed_tools：只回填成功且工具名真实的，顺序保持。
    assert loop_params.executed_tools == ["create_subagents", "read_file"]
    # tool_rounds：用记录数作保守代理，绝不归零（否则 max_tool_rounds 预算每次续跑重置）。
    assert loop_params.tool_rounds == 3
    # tool_context：按 [tool-record]/[tool-output-record] 格式重建，且能被守卫识别为历史。
    assert loop_params.tool_context
    assert all(entry.startswith("[tool-record") for entry in loop_params.tool_context)
    assert any("output_preview: def a(): ..." in entry for entry in loop_params.tool_context)


def test_compact_continuation_rebuilds_unknown_operation_facts():
    from types import SimpleNamespace

    from agent_py_agent.agent.agent_core.runtime.loop_support import _tool_loop_execute_params

    carried = [
        {
            "tool": "send_message",
            "ok": False,
            "parameters": {"tool": "send_message", "message": "hello"},
            "operation_id": "tool_call:send-1",
            "tool_operation_status": "unknown",
            "tool_operation_action": "completion_persistence_failed",
            "tool_operation_idempotency_scope": "business",
            "tool_operation_replayed": False,
            "effect_outcome": "unknown",
            "effect_source_ref": "provider://message/1",
            "error_code": "TOOL_OPERATION_OUTCOME_UNKNOWN",
        }
    ]

    loop_params = _tool_loop_execute_params(SimpleNamespace(), _seed_for_carried(carried))

    entry = loop_params.tool_context[0]
    assert "operation_id: tool_call:send-1" in entry
    assert "tool_operation_status: unknown" in entry
    assert "tool_operation_action: completion_persistence_failed" in entry
    assert "effect_outcome: unknown" in entry
    assert "effect_source_ref: provider://message/1" in entry
    assert "tool_operation_replayed: False" in entry


def test_compact_continuation_rebuilt_one_shot_blocks_duplicate_subagent_creation():
    from types import SimpleNamespace

    from agent_py_agent.agent.agent_core.parameters import _one_shot_tool_call_key
    from agent_py_agent.agent.agent_core.runtime.loop_support import _tool_loop_execute_params

    create_payload = {
        "tool": "create_subagents",
        "goal": "分析 X 项目",
        "items": [{"goal": "分析 X 的架构"}, {"goal": "分析 X 的测试"}],
    }
    carried = [{"tool": "create_subagents", "ok": True, "parameters": create_payload}]

    loop_params = _tool_loop_execute_params(SimpleNamespace(), _seed_for_carried(carried))

    # 续跑后，去重 gate 用的 one_shot key 必须已重建，且与 live gate 用同一把钥匙——
    # 这样续跑里模型再发同一份 create_subagents（payload 相同），去重 gate 会拦住，不会
    # 重复创建子代理（真副作用）。
    live_key = _one_shot_tool_call_key(create_payload)
    assert live_key in loop_params.one_shot_tool_calls

    from agent_py_agent.agent.agent_core.tool_call_runtime import (
        ToolCallRuntimeRequest,
        guarded_tool_call_result,
    )
    from agent_py_agent.agent.agent_core.tool_loop.round_execution import ToolCallExecuteParams
    from agent_py_agent.tests._tool_runtime_harness import canonical_history_call

    agent = SimpleNamespace(_current_subagent_run_id="")
    call = canonical_history_call(
        "create_subagents",
        {key: value for key, value in create_payload.items() if key != "tool"},
    )
    exec_params = ToolCallExecuteParams(loop_params, 4, 1, call)
    guarded = guarded_tool_call_result(
        ToolCallRuntimeRequest(agent, exec_params, call)
    )

    assert guarded is not None
    assert guarded.ok is False
    assert "重复" in guarded.output


def test_compact_continuation_does_not_rebuild_native_tool_history():
    # native 双轨：续跑刻意不从 archive 重建 tool call/result IR（避免与 IR 双轨冲突、
    # 避免把 compact 刚卸掉的工具历史又塞回原生 messages）。
    from types import SimpleNamespace

    from agent_py_agent.agent.agent_core.runtime.loop_support import _tool_loop_execute_params

    carried = [
        {
            "tool": "read_file",
            "ok": True,
            "parameters": {"tool": "read_file", "path": "/src/a.py", "call_id": "toolu_abc"},
            "output_preview": "x",
        }
    ]

    loop_params = _tool_loop_execute_params(SimpleNamespace(), _seed_for_carried(carried))

    assert loop_params.tool_ir_history == []
    # 文本轨仍重建（喂守卫），但不发往 native provider（builder 旁路 tool_context）。
    assert loop_params.tool_context


def test_compact_continuation_carries_active_turn_user_input_as_real_native_turn():
    from types import SimpleNamespace

    from agent_py_agent.agent.agent_core.runtime.loop_support import _tool_loop_execute_params
    from agent_py_agent.agent.backends.tool_ir import UserTurn

    packet = {
        "schema_version": "active-turn-user-input.v1",
        "input_ids": ["guidance-1"],
        "text": "只使用标准 wheel，不要改回源码路径加载。",
    }
    params = RunParams(carried_active_turn_user_inputs=[packet])
    source_result = type(
        "Result",
        (),
        {
            "tool_rounds": 1,
            "executed_tools": ["run_command"],
            "active_turn_user_inputs": [packet],
        },
    )()

    continued = _compact_auto_continue_params(
        params,
        "# Compact Auto Continuation\ncontinue",
        source_result,
    )
    agent = SimpleNamespace(
        config=SimpleNamespace(
            tool_protocol="native",
            enable_tools=True,
            model_name="MiniMax-M2.7",
        ),
        backend=SimpleNamespace(name="anthropic_compatible"),
    )
    loop_params = _tool_loop_execute_params(
        agent,
        _seed_for_carried(
            [],
            continued.carried_active_turn_user_inputs,
            source_protocol="native",
        ),
    )

    assert continued.carried_active_turn_user_inputs == [packet]
    assert loop_params.active_turn_user_inputs == [packet]
    assert loop_params.tool_ir_history == [UserTurn(packet["text"])]
    assert loop_params.tool_context == [f"[ACTIVE_TURN_USER_INPUT]\n{packet['text']}"]


def test_compact_continuation_carries_active_turn_user_input_on_text_protocol():
    from types import SimpleNamespace

    from agent_py_agent.agent.agent_core.runtime.loop_support import _tool_loop_execute_params

    packet = {
        "schema_version": "active-turn-user-input.v1",
        "input_ids": ["guidance-2"],
        "text": "先修复验收失败再收口。",
    }
    agent = SimpleNamespace(
        config=SimpleNamespace(tool_protocol="text", enable_tools=True),
        backend=SimpleNamespace(name="echo"),
    )

    loop_params = _tool_loop_execute_params(agent, _seed_for_carried([], [packet]))

    assert loop_params.tool_ir_history == []
    assert loop_params.tool_context == [f"[ACTIVE_TURN_USER_INPUT]\n{packet['text']}"]


# ---------------------------------------------------------------------------
# H2: compact 续跑必须有绝对深度硬顶，防止「持续高于阈值且每轮都调工具」的任务无限续跑。
# ---------------------------------------------------------------------------


def test_compact_auto_continuation_hard_cap_forces_return(tmp_path):
    from agent_py_agent.agent.agent_core.finalization_compact_auto import (
        _DEFAULT_MAX_COMPACT_AUTO_CONTINUE_DEPTH,
        _max_compact_auto_continue_depth,
    )

    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path)
    agent.backend.context_window_tokens = 20_000

    # 软顶清零（每轮都有工具进展），但 depth 已达硬顶：必须强制 return，不再 compact。
    ctx = replace(
        _finalize_context_for_continuation(tool_rounds=5, executed_tools=["read_file", "write_file"]),
        compact_auto_continue_depth=_max_compact_auto_continue_depth(agent),
        compact_auto_no_tool_continue_depth=0,
        final_response=ModelResponse(
            text="还在继续读材料。",
            backend="test",
            usage={"input_tokens": 19_000, "output_tokens": 100},
        ),
    )

    fields = compact_auto_cycle_fields(agent, ctx, {"turn": 19_100, "active": 19_100})

    assert fields["memory_compact_auto_status"] == "returned_after_depth_cap"
    assert fields["memory_compact_auto_allowed_to_continue"] is False
    assert fields["memory_compact_auto_continue_ready"] is False
    assert fields["memory_compact_auto_continue_packet"] == {}
    assert str(_DEFAULT_MAX_COMPACT_AUTO_CONTINUE_DEPTH) in fields["memory_compact_message"]


def test_compact_auto_continuation_under_hard_cap_still_compacts(tmp_path):
    from agent_py_agent.agent.agent_core.finalization_compact_auto import (
        _max_compact_auto_continue_depth,
    )

    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path)
    agent.backend.context_window_tokens = 20_000

    # depth 仍在硬顶以下、每轮有工具进展：硬顶不触发，正常进 compact 周期（不是 depth_cap）。
    ctx = replace(
        _finalize_context_for_continuation(tool_rounds=5, executed_tools=["read_file"]),
        compact_auto_continue_depth=_max_compact_auto_continue_depth(agent) - 1,
        compact_auto_no_tool_continue_depth=0,
        final_response=ModelResponse(
            text="还在继续读材料。",
            backend="test",
            usage={"input_tokens": 19_000, "output_tokens": 100},
        ),
    )

    fields = compact_auto_cycle_fields(agent, ctx, {"turn": 19_100, "active": 19_100})

    assert fields["memory_compact_auto_status"] != "returned_after_depth_cap"


def test_compact_auto_continuation_hard_cap_is_configurable(tmp_path):
    from agent_py_agent.agent.agent_core.finalization_compact_auto import (
        _max_compact_auto_continue_depth,
    )

    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            my_agent_home=str(tmp_path / "home"),
            memory_compact_auto_continue_max_depth=7,
        ),
        tmp_path,
    )
    agent.backend.context_window_tokens = 20_000

    assert _max_compact_auto_continue_depth(agent) == 7

    ctx = replace(
        _finalize_context_for_continuation(tool_rounds=5, executed_tools=["read_file"]),
        compact_auto_continue_depth=7,
        compact_auto_no_tool_continue_depth=0,
        final_response=ModelResponse(
            text="还在继续读材料。",
            backend="test",
            usage={"input_tokens": 19_000, "output_tokens": 100},
        ),
    )

    fields = compact_auto_cycle_fields(agent, ctx, {"turn": 19_100, "active": 19_100})

    assert fields["memory_compact_auto_status"] == "returned_after_depth_cap"


def test_tool_output_index_hides_artifact_ref_for_non_externalized() -> None:
    """Fix B(阶段4):未外置 tool output(无 externalized 标记)在 Exact Tool Output Index 里
    不喂 artifact_ref、只给 source_path,免得模型对读不到的 scoped_call_id 瞎试(read_artifact
    读它必报 not_externalized);已外置(externalized=True)的正常喂 ref。"""
    packet = {
        "apply_id": "apply-fixb",
        "continue_mode": "automated_guarded",
        "guard": {"status": "allowed"},
        "next_actions": ["继续合并报告"],
        "work_state_snapshot": {
            "goal": "g",
            "tool_progress": [
                {
                    "tool": "read_file",
                    "source_path": "src/deferred.py",
                    "artifact_ref": "run-x:call_function_deferred_1",
                    "size_bytes": 131,
                },
                {
                    "tool": "read_file",
                    "source_path": "src/externalized.py",
                    "artifact_ref": "run-x:call_function_ext_1",
                    "size_bytes": 4096,
                    "externalized": True,
                },
            ],
        },
    }

    rendered = build_compact_auto_continue_injection(packet)

    assert "## Exact Tool Output Index" in rendered
    assert "source_path=src/deferred.py" in rendered
    assert "artifact_ref=run-x:call_function_deferred_1" not in rendered
    assert "source_path=src/externalized.py" in rendered
    assert "artifact_ref=run-x:call_function_ext_1" in rendered
