from __future__ import annotations

import threading
import time

from agent_py_agent.agent.contracts.tool_approval import build_tool_approval_request
from agent_py_agent.agent.tooling.runtime_contracts import ToolCall
from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime, TuiTurnSummary


def _approval_request(request_id: str):
    return _approval_request_for(request_id, "permission-call", "permission-run")


def _approval_request_for(request_id: str, call_id: str, run_id: str):
    call = ToolCall(
        call_id=call_id,
        tool_name="run_command",
        arguments={"command": "printf fixture"},
        source_protocol="native",
        schema_hash="sha256:permission-fixture",
        run_id=run_id,
        turn_id="permission-turn",
        attempt_id="permission-attempt",
    )
    return build_tool_approval_request(
        call,
        request_id=request_id,
        round_number=1,
        call_index=1,
        description="run_command(printf fixture)",
    )


def test_direct_turn_stream_tool_and_final_keep_order_and_no_duplicate() -> None:
    runtime = TuiRuntime("session-1")
    runtime.publish_session(version="0.3.0", model="fixture", workspace="/tmp")
    runtime.enqueue_prompt("request-1", "hello", queued=False)
    turn = runtime.begin_turn("request-1")
    turn.write_model("I will check.")
    turn.write_progress(
        {"round": 1, "call_index": 1, "tool": "run_command", "phase": "started"}
    )
    turn.write_progress(
        {
            "round": 1,
            "call_index": 1,
            "tool": "run_command",
            "phase": "finished",
            "ok": True,
            "output": "ok",
        }
    )
    turn.write_model("Final")
    runtime.complete_turn(
        "request-1",
        TuiTurnSummary(response_text="Final", context_tokens=100, tool_rounds=1),
    )
    snapshot = runtime.store.snapshot()
    roles = [(block.role, block.text, block.detail) for block in snapshot.stable_blocks]
    assert roles == [
        ("system", "", ""),
        ("user", "hello", ""),
        ("assistant", "I will check.", ""),
        ("tool", "", "ok"),
        ("assistant", "Final", ""),
    ]
    assert snapshot.active_blocks == ()
    assert snapshot.status.phase == "idle"
    assert snapshot.status.context_tokens == 100
    assert snapshot.status.tool_rounds == 1


def test_multiple_provider_thinking_blocks_keep_chronological_order() -> None:
    """每次 provider 调用的思考都独立留在对应回复之前，不能被后一次覆盖。"""
    runtime = TuiRuntime("session-multi-thinking")
    turn = runtime.begin_turn("request-thinking")

    turn.write_thinking_delta("先检查")
    turn.write_model("准备读文件。")
    turn.write_progress(
        {"round": 1, "call_index": 1, "tool": "read_file", "phase": "started"}
    )
    turn.write_progress(
        {
            "round": 1,
            "call_index": 1,
            "tool": "read_file",
            "phase": "finished",
            "ok": True,
            "output": "ok",
        }
    )
    turn.write_thinking_delta("再整合")
    turn.write_model("最终答案")
    runtime.complete_turn(
        "request-thinking",
        TuiTurnSummary(response_text="最终答案", context_tokens=200, tool_rounds=1),
    )

    visible = [
        (block.role, block.text)
        for block in runtime.store.snapshot().stable_blocks
    ]
    assert visible == [
        ("thinking", "先检查"),
        ("assistant", "准备读文件。"),
        ("tool", ""),
        ("thinking", "再整合"),
        ("assistant", "最终答案"),
    ]


def test_begin_turn_uses_canonical_generation_and_clears_previous_todo() -> None:
    runtime = TuiRuntime("session-generation")
    runtime.publish_task_progress_snapshot(
        [{"id": "old", "title": "旧任务", "status": "in_progress"}],
        generation_id="gateway-old",
        plan_revision=2,
    )
    assert any(
        block.role == "todo" for block in runtime.store.snapshot().active_blocks
    )

    runtime.begin_turn(
        "chat-local-new",
        task_progress_generation_id="gateway-new",
    )

    assert not any(
        block.role == "todo" for block in runtime.store.snapshot().active_blocks
    )
    runtime.publish_task_progress_snapshot(
        [{"id": "old", "title": "旧任务", "status": "done"}],
        generation_id="gateway-old",
        plan_revision=3,
    )
    assert not any(
        block.role == "todo" for block in runtime.store.snapshot().active_blocks
    )
    runtime.publish_task_progress_snapshot(
        [{"id": "new", "title": "新任务", "status": "in_progress"}],
        generation_id="gateway-new",
        plan_revision=1,
    )
    todo = next(
        block
        for block in runtime.store.snapshot().active_blocks
        if block.role == "todo"
    )
    assert [item["id"] for item in todo.metadata["items"]] == ["new"]


def test_recovered_history_is_visible_without_requeueing_prompts() -> None:
    runtime = TuiRuntime("session-history")
    runtime.publish_session(version="0.3.0", model="fixture", workspace="/tmp")

    runtime.publish_recovered_history(
        [("第一问", "第一答"), ("第二问", "第二答")]
    )

    snapshot = runtime.store.snapshot()
    assert [(block.role, block.text) for block in snapshot.stable_blocks] == [
        ("system", ""),
        ("user", "第一问"),
        ("assistant", "第一答"),
        ("user", "第二问"),
        ("assistant", "第二答"),
    ]
    assert snapshot.queued_inputs == ()


def test_gateway_connection_check_is_transient_on_success_and_typed_on_failure() -> None:
    successful = TuiRuntime("connection-success")
    successful.publish_connection_check()
    connecting = successful.store.snapshot()
    assert [(block.role, block.phase) for block in connecting.active_blocks] == [
        ("connection", "started")
    ]
    successful.resolve_connection_check(ok=True)
    assert successful.store.snapshot().active_blocks == ()
    assert successful.store.snapshot().stable_blocks == ()

    failed = TuiRuntime("connection-failed")
    failed.publish_connection_check()
    failed.resolve_connection_check(ok=False, error_code="GATEWAY_NOT_READY")
    snapshot = failed.store.snapshot()
    assert snapshot.active_blocks == ()
    assert [(block.role, block.phase, block.text) for block in snapshot.stable_blocks] == [
        ("error", "failed", "Gateway is unavailable.")
    ]


def test_gateway_context_usage_updates_status_without_creating_transcript_blocks() -> None:
    runtime = TuiRuntime("context-runtime")
    runtime.enqueue_prompt("context-request", "go", queued=False)
    turn = runtime.begin_turn("context-request")
    stable_before = runtime.store.snapshot().stable_blocks

    assert turn.on_gateway_event(
        {
            "kind": "context_usage_updated",
            "context_usage": {
                "schema": "model_visible_context_usage.v1",
                "estimated": True,
                "context_window_tokens": 128_000,
                "compact_trigger_tokens": 115_200,
                "current_tokens": 31_400,
                "prompt_tokens": 8_000,
                "messages_tokens": 6_000,
                "runtime_guidance_tokens": 400,
                "tool_schema_tokens": 17_000,
                "protocol": "native",
                "prompt": "ignore",
            },
        }
    ) is True

    snapshot = runtime.store.snapshot()
    assert snapshot.stable_blocks == stable_before
    assert snapshot.status.context_tokens == 31_400
    assert snapshot.status.context_usage is not None
    assert snapshot.status.context_usage.current_tokens == 31_400
    assert snapshot.status.context_usage.tool_schema_tokens == 17_000
    assert not hasattr(snapshot.status.context_usage, "prompt")


def test_manual_compact_boundary_retires_stale_context_and_updates_generation() -> None:
    runtime = TuiRuntime("manual-compact-runtime")
    runtime.enqueue_prompt("context-request", "go", queued=False)
    turn = runtime.begin_turn("context-request")
    assert turn.write_context_usage(
        {
            "schema": "model_visible_context_usage.v1",
            "estimated": True,
            "context_window_tokens": 128_000,
            "compact_trigger_tokens": 115_200,
            "current_tokens": 61_200,
            "prompt_tokens": 8_000,
            "messages_tokens": 35_000,
            "runtime_guidance_tokens": 1_200,
            "tool_schema_tokens": 17_000,
            "protocol": "native",
        }
    )

    runtime.publish_compact_boundary(
        1,
        text="Context compacted · generation 1\n上下文估算：45,639 → 15,029 tokens。",
    )

    snapshot = runtime.store.snapshot()
    assert snapshot.status.compact_count == 1
    assert snapshot.status.context_tokens == 0
    assert snapshot.status.context_usage is None
    assert snapshot.stable_blocks[-1].kind == "compact_boundary"
    assert "45,639 → 15,029" in snapshot.stable_blocks[-1].text


def test_manual_compact_has_active_animation_until_receipt() -> None:
    runtime = TuiRuntime("manual-compact-progress")

    runtime.publish_manual_compact_started("control-one")

    active = runtime.store.snapshot().active_blocks
    assert len(active) == 1
    assert active[0].role == "compact"
    assert active[0].metadata["stage"] == "preparing"
    assert active[0].metadata["percent"] == 0
    assert active[0].metadata["indeterminate"] is True

    runtime.publish_manual_compact_terminal("control-one", succeeded=True)

    assert runtime.store.snapshot().active_blocks == ()


def test_manual_compact_interruption_freezes_interrupted_without_generation() -> None:
    runtime = TuiRuntime("manual-compact-interrupted")
    runtime.publish_manual_compact_started("control-interrupted")

    assert (
        runtime.active_manual_compact_control_message_id()
        == "control-interrupted"
    )
    runtime.publish_manual_compact_terminal(
        "control-interrupted",
        succeeded=False,
        interrupted=True,
    )

    snapshot = runtime.store.snapshot()
    assert runtime.active_manual_compact_control_message_id() == ""
    assert snapshot.active_blocks == ()
    assert snapshot.status.compact_count == 0
    assert snapshot.stable_blocks[-1].role == "compact"
    assert snapshot.stable_blocks[-1].phase == "interrupted"


def test_idle_activity_snapshot_hydrates_compact_count_and_cannot_regress() -> None:
    runtime = TuiRuntime("resumed-compact-runtime")

    assert runtime.update_background_activity(0, {"compact_count": 3}) is True
    hydrated = runtime.store.snapshot()
    assert hydrated.status.compact_count == 3
    assert not any(block.role == "background" for block in hydrated.active_blocks)
    assert not any(
        diagnostic.code == "BACKGROUND_ACTIVITY_COMPLETE_WITHOUT_START"
        for diagnostic in hydrated.diagnostics
    )

    assert runtime.update_background_activity(0, {"compact_count": 1}) is False
    assert runtime.store.snapshot().status.compact_count == 3


def test_idle_goal_keeps_fixed_projection_until_authoritative_empty_snapshot() -> None:
    runtime = TuiRuntime("idle-goal-runtime")
    goal = {
        "goal_id": "goal-one",
        "name": "持续验证",
        "objective": "继续验证底座",
        "status": "paused",
        "tokens_used": 1200,
        "time_used_seconds": 45,
    }

    assert runtime.update_background_activity(0, {"goals": [goal]}) is True
    active = runtime.store.snapshot().active_blocks
    background = next(block for block in active if block.role == "background")
    assert background.metadata["goals"] == [goal]
    assert runtime.has_active_background_task() is False

    # 投影读取失败不能把上一份已知 Goal 擦掉。
    assert runtime.update_background_activity(
        0,
        {"goals": [], "goal_projection_ok": False},
    ) is False
    assert any(
        block.role == "background" for block in runtime.store.snapshot().active_blocks
    )

    assert runtime.update_background_activity(
        0,
        {"goals": [], "goal_projection_ok": True},
    ) is True
    assert not any(
        block.role == "background" for block in runtime.store.snapshot().active_blocks
    )


def test_gateway_context_compaction_creates_one_content_free_history_event() -> None:
    runtime = TuiRuntime("context-compaction-runtime")
    runtime.enqueue_prompt("context-request", "go", queued=False)
    turn = runtime.begin_turn("context-request")

    assert turn.on_gateway_event(
        {
            "kind": "context_window_compacted",
            "context_compaction": {
                "schema": "model_visible_context_compaction.v1",
                "generation": 1,
                "before_tokens": 118_400,
                "after_tokens": 31_200,
                "trigger_tokens": 115_200,
                "dropped_pairs": 84,
                "preserved_pairs": 12,
                "summary": "ignore",
            },
        }
    ) is True

    compact = runtime.store.snapshot().stable_blocks[-1]
    assert compact.kind == "context_window_compacted"
    assert compact.metadata == {
        "generation": 1,
        "before_tokens": 118_400,
        "after_tokens": 31_200,
        "trigger_tokens": 115_200,
        "dropped_pairs": 84,
        "preserved_pairs": 12,
    }
    assert "summary" not in compact.metadata


def test_gateway_conversation_compact_progress_updates_one_active_block_then_boundary() -> None:
    runtime = TuiRuntime("conversation-compact-runtime")
    runtime.enqueue_prompt("compact-request", "go", queued=False)
    turn = runtime.begin_turn("compact-request")

    base = {
        "schema": "conversation_compaction_progress.v1",
        "generation": 3,
        "operation_id": "transcript:compact-3",
        "source_kind": "conversation_transcript",
        "commit_authority": "conversation_thread",
        "before_tokens": 118_400,
        "after_tokens": 0,
        "trigger_tokens": 115_200,
        "source_messages": 80,
    }
    assert turn.on_gateway_event(
        {
            "kind": "conversation_compaction_progress",
            "compact_progress": {
                **base,
                "phase": "started",
                "stage": "preparing",
                "percent": 5,
                "summary": "ignore",
            },
        }
    ) is True
    assert runtime.needs_periodic_refresh() is True
    assert turn.on_gateway_event(
        {
            "kind": "conversation_compaction_progress",
            "compact_progress": {
                **base,
                "phase": "progress",
                "stage": "summarizing",
                "percent": 35,
            },
        }
    ) is True

    active = [block for block in runtime.store.snapshot().active_blocks if block.role == "compact"]
    assert len(active) == 1
    assert active[0].metadata["percent"] == 35
    assert active[0].metadata["stage"] == "summarizing"
    assert active[0].metadata["source_kind"] == "conversation_transcript"
    assert "summary" not in active[0].metadata

    assert turn.on_gateway_event(
        {
            "kind": "conversation_compaction_progress",
            "compact_progress": {
                **base,
                "phase": "completed",
                "stage": "completed",
                "percent": 100,
                "after_tokens": 31_200,
            },
        }
    ) is True
    assert not any(
        block.role == "compact" for block in runtime.store.snapshot().active_blocks
    )
    assert turn.on_gateway_event(
        {"kind": "conversation_compacted", "compact_generation": 3}
    ) is True
    assert runtime.store.snapshot().stable_blocks[-1].kind == "compact_boundary"
    assert runtime.store.snapshot().status.compact_count == 3


def test_failed_live_compact_does_not_hide_transcript_fallback_progress() -> None:
    """Different operation ids may reuse one not-yet-committed generation in the same turn."""
    runtime = TuiRuntime("conversation-compact-fallback")
    runtime.enqueue_prompt("compact-fallback", "go", queued=False)
    turn = runtime.begin_turn("compact-fallback")
    base = {
        "schema": "conversation_compaction_progress.v1",
        "generation": 2,
        "before_tokens": 118_400,
        "after_tokens": 0,
        "trigger_tokens": 115_200,
        "source_messages": 60,
    }

    assert turn.write_conversation_compact_progress(
        {
            **base,
            "operation_id": "live-tool:attempt-a",
            "phase": "started",
            "stage": "preparing",
            "percent": 5,
        }
    )
    assert turn.write_conversation_compact_progress(
        {
            **base,
            "operation_id": "live-tool:attempt-a",
            "phase": "failed",
            "stage": "failed",
            "percent": 0,
            "error_code": "COMPACT_MODEL_EMPTY_RESPONSE",
        }
    )
    assert turn.write_conversation_compact_progress(
        {
            **base,
            "operation_id": "transcript:attempt-b",
            "phase": "started",
            "stage": "preparing",
            "percent": 5,
        }
    )

    snapshot = runtime.store.snapshot()
    assert any(block.role == "compact" for block in snapshot.active_blocks)
    assert any(block.phase == "failed" for block in snapshot.stable_blocks)
    failed = next(block for block in snapshot.stable_blocks if block.phase == "failed")
    assert failed.metadata["error_code"] == "COMPACT_MODEL_EMPTY_RESPONSE"


def test_superseded_live_compact_retires_silently_before_transcript_fallback() -> None:
    """未采用的 live 候选不留红字，同代 fallback 从自己的真实进度开始。"""
    runtime = TuiRuntime("conversation-compact-superseded")
    runtime.enqueue_prompt("compact-superseded", "go", queued=False)
    turn = runtime.begin_turn("compact-superseded")
    base = {
        "schema": "conversation_compaction_progress.v1",
        "generation": 2,
        "before_tokens": 118_400,
        "after_tokens": 0,
        "trigger_tokens": 115_200,
        "source_messages": 60,
    }

    for phase, stage, percent in (
        ("started", "preparing", 5),
        ("progress", "measuring", 65),
        ("superseded", "candidate_discarded", 0),
    ):
        assert turn.write_conversation_compact_progress(
            {
                **base,
                "operation_id": "live-tool:attempt-a",
                "phase": phase,
                "stage": stage,
                "percent": percent,
            }
        )

    retired = runtime.store.snapshot()
    assert not any(block.role == "compact" for block in retired.active_blocks)
    assert not any(block.role == "compact" for block in retired.stable_blocks)
    assert turn.write_conversation_compact_progress(
        {
            **base,
            "operation_id": "transcript:attempt-b",
            "phase": "started",
            "stage": "preparing",
            "percent": 5,
        }
    )
    active = next(
        block for block in runtime.store.snapshot().active_blocks if block.role == "compact"
    )
    assert active.metadata["percent"] == 5, "同代 fallback 不得继承上一 operation 的进度"

    assert not turn.write_conversation_compact_progress(
        {
            **base,
            "operation_id": "live-tool:attempt-a",
            "phase": "progress",
            "stage": "checkpointing",
            "percent": 92,
        }
    )
    still_fallback = next(
        block for block in runtime.store.snapshot().active_blocks if block.role == "compact"
    )
    assert still_fallback.metadata["percent"] == 5

    assert turn.write_conversation_compact_progress(
        {
            **base,
            "operation_id": "transcript:attempt-b",
            "phase": "completed",
            "stage": "completed",
            "percent": 100,
            "after_tokens": 31_200,
        }
    )
    assert turn.write_conversation_compact_progress(
        {
            **base,
            "generation": 3,
            "operation_id": "transcript:attempt-c",
            "phase": "started",
            "stage": "preparing",
            "percent": 5,
        }
    )
    next_generation = next(
        block for block in runtime.store.snapshot().active_blocks if block.role == "compact"
    )
    assert next_generation.metadata["percent"] == 5, "新一代 Compact 应从自己的真实阶段开始"


def test_turn_activity_survives_stream_and_tools_until_structured_terminal() -> None:
    runtime = TuiRuntime("session-activity")
    runtime.enqueue_prompt("request-activity", "go", queued=False)
    turn = runtime.begin_turn("request-activity")

    turn.write_model("你好abc")
    streaming = runtime.store.snapshot()
    assert {block.role for block in streaming.active_blocks} == {"assistant"}
    assert streaming.status.output_tokens >= 3

    turn.write_progress(
        {"round": 1, "call_index": 1, "tool": "run_command", "phase": "started"}
    )
    using_tool = runtime.store.snapshot()
    assert {block.role for block in using_tool.active_blocks} == {"tool"}

    turn.write_progress(
        {
            "round": 1,
            "call_index": 1,
            "tool": "run_command",
            "phase": "finished",
            "ok": True,
        }
    )
    assert runtime.store.snapshot().active_blocks == ()

    runtime.complete_turn(
        "request-activity",
        TuiTurnSummary(response_text="你好abc", output_tokens=3),
    )
    completed = runtime.store.snapshot()
    assert completed.active_blocks == ()
    assert completed.status.output_tokens == 3


def test_gateway_typed_rows_are_consumed_without_legacy_text_duplication() -> None:
    runtime = TuiRuntime("session-2")
    runtime.enqueue_prompt("request-2", "gateway", queued=False)
    turn = runtime.begin_turn("request-2")
    assert turn.on_gateway_event(
        {"kind": "conversation_compacted", "compact_generation": 2}
    )
    assert turn.on_gateway_event(
        {"kind": "assistant_commentary", "text": "Checking first."}
    )
    assert turn.on_gateway_event(
        {
            "kind": "tool_progress",
            "text": "legacy must not render",
            "progress": {
                "round": 2,
                "call_index": 3,
                "tool": "read_file",
                "phase": "finished",
                "ok": False,
                "error_code": "READ_FAILED",
                "output": "denied",
            },
        }
    )
    compact = next(
        block
        for block in runtime.store.snapshot().stable_blocks
        if block.kind == "compact_boundary"
    )
    assert compact.metadata["compact_generation"] == 2
    assert runtime.store.snapshot().status.compact_count == 2
    runtime.complete_turn(
        "request-2",
        TuiTurnSummary(response_text="Gateway final", ok=True, tool_rounds=2),
    )
    snapshot = runtime.store.snapshot()
    visible = "\n".join(block.text + block.detail for block in snapshot.stable_blocks)
    assert "Checking first." in visible
    assert "Gateway final" in visible
    assert "legacy must not render" not in visible
    tool = next(block for block in snapshot.stable_blocks if block.role == "tool")
    assert tool.phase == "failed"
    assert tool.metadata["error_code"] == "READ_FAILED"


def test_gateway_provider_thinking_becomes_separate_collapsible_block() -> None:
    runtime = TuiRuntime("session-thinking")
    runtime.enqueue_prompt("request-thinking", "go", queued=False)
    turn = runtime.begin_turn("request-thinking")

    assert turn.on_gateway_event(
        {
            "kind": "assistant_thinking",
            "text": "先比较两个实现。",
            "duration_seconds": 3.25,
        }
    )

    thought = next(
        block
        for block in runtime.store.snapshot().stable_blocks
        if block.role == "thinking"
    )
    assert thought.text == "先比较两个实现。"
    assert thought.metadata["duration_seconds"] == 3.25


def test_gateway_provider_retry_progress_is_immediately_visible() -> None:
    runtime = TuiRuntime("session-retry")
    runtime.enqueue_prompt("request-retry", "continue", queued=False)
    turn = runtime.begin_turn("request-retry")

    assert turn.on_gateway_event(
        {
            "kind": "runtime_progress",
            "text": "模型服务暂时不可用，2 秒后自动重连（连接 1/3）",
            "verbose_level": "full",
            "retry": {
                "scope": "transport",
                "attempt": 1,
                "total": 3,
                "wait_seconds": 2.0,
            },
        }
    )

    retry = next(
        block
        for block in runtime.store.snapshot().stable_blocks
        if block.kind == "system_message"
    )
    assert retry.role == "system"
    assert retry.text == "模型服务暂时不可用，2 秒后自动重连（连接 1/3）"


def test_gateway_assistant_final_is_not_duplicated_by_response_finalize() -> None:
    runtime = TuiRuntime("session-final")
    runtime.enqueue_prompt("request-final", "gateway", queued=False)
    turn = runtime.begin_turn("request-final")
    assert turn.on_gateway_event({"kind": "assistant_final", "text": "only once"})

    runtime.complete_turn(
        "request-final",
        TuiTurnSummary(response_text="only once", ok=True),
    )

    assistant = [
        block for block in runtime.store.snapshot().stable_blocks if block.role == "assistant"
    ]
    assert [block.text for block in assistant] == ["only once"]


def test_queued_prompt_is_removed_when_its_turn_begins() -> None:
    runtime = TuiRuntime("session-3")
    runtime.enqueue_prompt("request-running", "one", queued=False)
    runtime.begin_turn("request-running")
    runtime.enqueue_prompt("request-queued", "two", queued=True)
    queued = runtime.store.snapshot()
    assert [item.text for item in queued.queued_inputs] == ["two"]
    assert [block.text for block in queued.stable_blocks if block.role == "user"] == ["one"]
    runtime.begin_turn("request-queued")
    promoted = runtime.store.snapshot()
    assert promoted.queued_inputs == ()
    assert [block.text for block in promoted.stable_blocks if block.role == "user"] == [
        "one",
        "two",
    ]


def test_active_turn_input_waits_for_exact_gateway_injection_confirmation() -> None:
    runtime = TuiRuntime("session-steer")
    runtime.enqueue_prompt("request-running", "原始任务", queued=False)
    turn = runtime.begin_turn("request-running")
    runtime.enqueue_active_turn_input("steer-local-1", "第一条补充")
    runtime.enqueue_active_turn_input("steer-local-2", "第二条补充")

    pending = runtime.store.snapshot()
    assert [item.text for item in pending.pending_steers] == ["第一条补充", "第二条补充"]
    assert [block.text for block in pending.stable_blocks if block.role == "user"] == [
        "原始任务"
    ]

    assert turn.on_gateway_event(
        {
            "kind": "active_turn_input_consumed",
            "client_message_ids": ["external-steer", "steer-local-1"],
        }
    )
    first = runtime.store.snapshot()
    assert [item.message_id for item in first.pending_steers] == ["steer-local-2"]
    assert [block.text for block in first.stable_blocks if block.role == "user"] == [
        "原始任务",
        "第一条补充",
    ]

    assert turn.on_gateway_event(
        {
            "kind": "active_turn_input_consumed",
            "client_message_ids": ["steer-local-2"],
        }
    )
    completed = runtime.store.snapshot()
    assert completed.pending_steers == ()
    assert [block.text for block in completed.stable_blocks if block.role == "user"] == [
        "原始任务",
        "第一条补充",
        "第二条补充",
    ]


def test_gateway_active_input_confirmation_does_not_duplicate_streamed_final() -> None:
    runtime = TuiRuntime("session-steer-final")
    runtime.enqueue_prompt("request-running", "原始任务", queued=False)
    turn = runtime.begin_turn("request-running")
    runtime.enqueue_active_turn_input("steer-local", "补充要求")
    turn.write_model("只显示一次")

    assert turn.on_gateway_event(
        {
            "kind": "active_turn_input_consumed",
            "client_message_ids": ["steer-local"],
        }
    )
    turn.finalize(TuiTurnSummary(response_text="只显示一次", ok=True))

    assistant = [
        block for block in runtime.store.snapshot().stable_blocks if block.role == "assistant"
    ]
    assert [block.text for block in assistant] == ["只显示一次"]


def test_late_gateway_active_input_confirmation_keeps_submit_order() -> None:
    runtime = TuiRuntime("session-steer-late-order")
    runtime.enqueue_prompt("request-running", "原始任务", queued=False)
    turn = runtime.begin_turn("request-running")
    runtime.enqueue_active_turn_input("steer-local", "补充要求")
    turn.on_gateway_event({"kind": "assistant_final", "text": "最终答复"})

    assert turn.on_gateway_event(
        {
            "kind": "active_turn_input_consumed",
            "client_message_ids": ["steer-local"],
        }
    )
    turn.finalize(TuiTurnSummary(response_text="最终答复", ok=True))

    visible = [
        (block.role, block.text)
        for block in runtime.store.snapshot().stable_blocks
        if block.role in {"user", "assistant"}
    ]
    assert visible == [
        ("user", "原始任务"),
        ("user", "补充要求"),
        ("assistant", "最终答复"),
    ]


def test_rejected_active_turn_input_can_be_removed_without_touching_queue() -> None:
    runtime = TuiRuntime("session-steer-rejected")
    runtime.enqueue_active_turn_input("steer-rejected", "补充")
    runtime.enqueue_prompt("request-next", "下一轮", queued=True)

    assert runtime.cancel_active_turn_input("steer-rejected") is True
    snapshot = runtime.store.snapshot()
    assert snapshot.pending_steers == ()
    assert [item.text for item in snapshot.queued_inputs] == ["下一轮"]


def test_queued_prompt_can_be_restored_before_worker_claim() -> None:
    runtime = TuiRuntime("session-restore")
    runtime.enqueue_prompt("request-queued", "two", queued=True)

    assert runtime.has_queued_prompt("request-queued")
    runtime.restore_prompts(("request-queued",))

    assert not runtime.has_queued_prompt("request-queued")
    assert runtime.store.snapshot().queued_inputs == ()


def test_failed_and_interrupted_turns_are_structured() -> None:
    failed = TuiRuntime("session-failed")
    failed.enqueue_prompt("failed", "x", queued=False)
    failed.begin_turn("failed")
    failed.complete_turn("failed", TuiTurnSummary(ok=False, error="boom"))
    failed_snapshot = failed.store.snapshot()
    assert failed_snapshot.status.phase == "failed"
    assert failed_snapshot.stable_blocks[-1].role == "error"

    interrupted = TuiRuntime("session-interrupted")
    interrupted.enqueue_prompt("interrupted", "x", queued=False)
    interrupted.begin_turn("interrupted")
    assert interrupted.request_interrupt() is True
    assert interrupted.store.snapshot().status.phase == "interrupting"
    assert interrupted.request_interrupt() is True
    interrupted.complete_turn("interrupted", TuiTurnSummary(ok=False, interrupted=True))
    interrupted_snapshot = interrupted.store.snapshot()
    assert interrupted_snapshot.status.phase == "interrupted"
    assert [
        block.text
        for block in interrupted_snapshot.stable_blocks
        if block.kind == "interrupt_notice"
    ] == ["已中断 · 接下来希望 my-agent 怎么做？"]
    assert interrupted.request_interrupt() is False


def test_local_permission_waits_for_typed_selection_and_keeps_feedback() -> None:
    runtime = TuiRuntime("session-permission")
    runtime.enqueue_prompt("request-permission", "run it", queued=False)
    turn = runtime.begin_turn("request-permission")
    turn.write_progress(
        {"round": 1, "call_index": 1, "tool": "run_command", "phase": "started"}
    )
    request = _approval_request("request-permission")
    result: dict[str, object] = {}

    def wait_for_permission() -> None:
        result.update(turn.request_permission(request.to_dict()))

    thread = threading.Thread(target=wait_for_permission)
    thread.start()
    deadline = time.monotonic() + 1.0
    while runtime.store.snapshot().permission is None and time.monotonic() < deadline:
        time.sleep(0.01)

    permission = runtime.store.snapshot().permission
    assert permission is not None
    assert permission.permission_id == request.permission_id
    assert runtime.toggle_permission_feedback()
    assert runtime.update_permission_feedback(
        request.permission_id,
        "only inspect the target",
    )
    assert runtime.resolve_permission(request.permission_id, "approved")
    thread.join(timeout=1.0)

    assert not thread.is_alive()
    assert result["decision"] == "approved"
    assert result["feedback"] == "only inspect the target"
    assert runtime.store.snapshot().permission is None


def test_gateway_permission_resolution_calls_exact_sink_once() -> None:
    runtime = TuiRuntime("session-gateway-permission")
    runtime.enqueue_prompt("request-gateway-permission", "run it", queued=False)
    turn = runtime.begin_turn("request-gateway-permission")
    turn.write_progress(
        {"round": 1, "call_index": 1, "tool": "run_command", "phase": "started"}
    )
    request = _approval_request("request-gateway-permission")
    captured: list[tuple[object, object]] = []
    turn.configure_gateway_permission_sink(
        lambda approval_request, decision: captured.append(
            (approval_request, decision)
        )
    )

    assert turn.on_gateway_event(
        {"kind": "permission_requested", "permission": request.to_dict()}
    )
    assert runtime.move_permission_selection(1)
    assert runtime.resolve_permission(request.permission_id, "denied")

    assert len(captured) == 1
    assert captured[0][0] == request
    assert captured[0][1].decision == "denied"
    assert runtime.store.snapshot().permission is None
    assert turn.on_gateway_event(
        {
            "kind": "permission_resolved",
            "permission_id": request.permission_id,
            "decision": "denied",
        }
    )


def test_concurrent_turn_permissions_are_fifo_and_never_replace_overlay() -> None:
    runtime = TuiRuntime("permission-fifo")
    first_turn = runtime.begin_turn("permission-fifo-first")
    second_turn = runtime.begin_turn("permission-fifo-second")
    first = _approval_request_for(
        "permission-fifo-first",
        "permission-call-first",
        "permission-run-first",
    )
    second = _approval_request_for(
        "permission-fifo-second",
        "permission-call-second",
        "permission-run-second",
    )
    results: dict[str, dict[str, object]] = {"first": {}, "second": {}}

    first_thread = threading.Thread(
        target=lambda: results["first"].update(
            first_turn.request_permission(first.to_dict())
        )
    )
    second_thread = threading.Thread(
        target=lambda: results["second"].update(
            second_turn.request_permission(second.to_dict())
        )
    )
    first_thread.start()
    deadline = time.monotonic() + 1.0
    while runtime.store.snapshot().permission is None and time.monotonic() < deadline:
        time.sleep(0.01)

    overlay = runtime.store.snapshot().permission
    assert overlay is not None
    assert overlay.permission_id == first.permission_id
    second_thread.start()
    assert runtime.resolve_permission(first.permission_id, "approved")
    # Thread.start 不保证第二线程已经入队；等待公开 overlay 出现，不能把调度延迟误判成 FIFO 丢失。
    deadline = time.monotonic() + 1.0
    while runtime.store.snapshot().permission is None and time.monotonic() < deadline:
        time.sleep(0.01)
    overlay = runtime.store.snapshot().permission
    assert overlay is not None and overlay.permission_id == second.permission_id
    assert runtime.resolve_permission(second.permission_id, "denied")
    first_thread.join(timeout=1.0)
    second_thread.join(timeout=1.0)

    assert results["first"]["decision"] == "approved"
    assert results["second"]["decision"] == "denied"
    assert runtime.store.snapshot().permission is None


def test_external_child_permissions_use_same_fifo_and_exact_writer() -> None:
    runtime = TuiRuntime("permission-child-fifo")
    first = _approval_request_for(
        "attempt-child-first",
        "child-call-first",
        "subagent-child-first",
    )
    second = _approval_request_for(
        "attempt-child-second",
        "child-call-second",
        "subagent-child-second",
    )
    written: list[tuple[str, object, object]] = []

    changed = runtime.sync_agent_permission_requests(
        [
            {
                "run_id": "subagent-child-first",
                "agent_name": "researcher-1",
                "request": first.to_dict(),
            },
            {
                "run_id": "subagent-child-second",
                "agent_name": "tester-2",
                "request": second.to_dict(),
            },
        ],
        decision_writer=lambda run_id, request, decision: (
            written.append((run_id, request, decision)) or {"ok": True}
        ),
    )

    assert changed is True
    overlay = runtime.store.snapshot().permission
    assert overlay is not None
    assert overlay.permission_id == first.permission_id
    assert overlay.title.startswith("子代理 researcher-1")
    assert runtime.resolve_permission(first.permission_id, "approved")
    assert runtime.store.snapshot().permission.permission_id == second.permission_id
    assert runtime.resolve_permission(second.permission_id, "cancelled")

    assert [(row[0], row[1]) for row in written] == [
        ("subagent-child-first", first),
        ("subagent-child-second", second),
    ]
    assert [row[2].decision for row in written] == ["approved", "cancelled"]
    assert runtime.store.snapshot().permission is None


def test_foreground_and_child_permissions_share_one_fifo() -> None:
    runtime = TuiRuntime("permission-mixed-fifo")
    foreground_turn = runtime.begin_turn("permission-mixed-foreground")
    foreground = _approval_request_for(
        "permission-mixed-foreground",
        "permission-call-foreground",
        "permission-run-foreground",
    )
    child = _approval_request_for(
        "permission-mixed-child",
        "permission-call-child",
        "subagent-mixed-child",
    )
    foreground_result: dict[str, object] = {}
    written: list[tuple[str, object, object]] = []

    foreground_thread = threading.Thread(
        target=lambda: foreground_result.update(
            foreground_turn.request_permission(foreground.to_dict())
        )
    )
    foreground_thread.start()
    deadline = time.monotonic() + 1.0
    while runtime.store.snapshot().permission is None and time.monotonic() < deadline:
        time.sleep(0.01)

    assert runtime.store.snapshot().permission.permission_id == foreground.permission_id
    assert runtime.sync_agent_permission_requests(
        [
            {
                "run_id": "subagent-mixed-child",
                "agent_name": "builder-1",
                "request": child.to_dict(),
            }
        ],
        decision_writer=lambda run_id, request, decision: (
            written.append((run_id, request, decision)) or {"ok": True}
        ),
    )
    assert runtime.store.snapshot().permission.permission_id == foreground.permission_id

    assert runtime.resolve_permission(foreground.permission_id, "approved")
    assert runtime.store.snapshot().permission.permission_id == child.permission_id
    assert runtime.resolve_permission(child.permission_id, "denied")
    foreground_thread.join(timeout=1.0)

    assert not foreground_thread.is_alive()
    assert foreground_result["decision"] == "approved"
    assert [(row[0], row[1], row[2].decision) for row in written] == [
        ("subagent-mixed-child", child, "denied")
    ]
    assert runtime.store.snapshot().permission is None


def _assistant_blocks(runtime) -> list:
    return [
        block
        for block in runtime.store.snapshot().stable_blocks
        if block.role == "assistant"
    ]


def test_gateway_model_delta_streams_into_live_assistant_block() -> None:
    runtime = TuiRuntime("session-delta")
    runtime.enqueue_prompt("request-delta", "stream", queued=False)
    turn = runtime.begin_turn("request-delta")

    assert turn.on_gateway_event({"kind": "model_delta", "text": "第一"})
    assert turn.on_gateway_event({"kind": "model_delta", "text": "段。"})

    active = [
        block for block in runtime.store.snapshot().active_blocks
        if block.role == "assistant"
    ]
    assert len(active) == 1
    assert active[0].text == "第一段。"
    assert _assistant_blocks(runtime) == []


def test_gateway_commentary_freezes_streamed_deltas_as_process_without_duplicate() -> None:
    runtime = TuiRuntime("session-commentary")
    runtime.enqueue_prompt("request-commentary", "stream", queued=False)
    turn = runtime.begin_turn("request-commentary")

    turn.on_gateway_event({"kind": "model_delta", "text": "先看"})
    turn.on_gateway_event({"kind": "model_delta", "text": "文件。"})
    assert turn.on_gateway_event(
        {"kind": "assistant_commentary", "text": "先看文件。"}
    )

    assistant = _assistant_blocks(runtime)
    assert len(assistant) == 1
    assert assistant[0].text == "先看文件。"  # 不重复追加
    assert assistant[0].metadata.get("process") is True


def test_gateway_commentary_without_delta_stream_falls_back_to_full_text() -> None:
    runtime = TuiRuntime("session-old-gateway")
    runtime.enqueue_prompt("request-old", "old", queued=False)
    turn = runtime.begin_turn("request-old")

    assert turn.on_gateway_event(
        {"kind": "assistant_commentary", "text": "旧 Gateway 整段正文"}
    )

    assistant = _assistant_blocks(runtime)
    assert len(assistant) == 1
    assert assistant[0].text == "旧 Gateway 整段正文"
    assert assistant[0].metadata.get("process") is True


def test_local_tool_progress_freezes_commentary_as_process() -> None:
    runtime = TuiRuntime("session-local-process")
    runtime.begin_turn("request-local")
    turn = runtime._turns["request-local"]

    turn.write_model("先检查")
    turn.write_progress({"tool": "read_file", "phase": "started", "status": "开始"}, "legacy")

    assistant = _assistant_blocks(runtime)
    assert len(assistant) == 1
    assert assistant[0].text == "先检查"
    assert assistant[0].metadata.get("process") is True


def test_finalize_overwrites_streamed_deltas_without_duplicate() -> None:
    runtime = TuiRuntime("session-final-stream")
    runtime.enqueue_prompt("request-final-stream", "stream", queued=False)
    turn = runtime.begin_turn("request-final-stream")

    turn.on_gateway_event({"kind": "model_delta", "text": "最终"})
    turn.on_gateway_event({"kind": "model_delta", "text": "答案"})
    runtime.complete_turn(
        "request-final-stream",
        TuiTurnSummary(response_text="最终答案", ok=True),
    )

    assistant = _assistant_blocks(runtime)
    assert len(assistant) == 1
    assert assistant[0].text == "最终答案"
    assert "process" not in assistant[0].metadata


def test_deferred_assistant_display_waits_for_terminal_summary() -> None:
    runtime = TuiRuntime("session-show-prompt-order")
    runtime.enqueue_prompt("request-show-prompt", "inspect prompt", queued=False)
    turn = runtime.begin_turn(
        "request-show-prompt",
        defer_assistant_display=True,
    )

    turn.on_gateway_event({"kind": "model_delta", "text": "最终"})
    turn.on_gateway_event({"kind": "assistant_final", "text": "最终答案"})

    assert _assistant_blocks(runtime) == []
    runtime.write_console("===== FINAL PROMPT =====\nprompt\n===== RESPONSE =====")
    runtime.complete_turn(
        "request-show-prompt",
        TuiTurnSummary(response_text="最终答案", ok=True),
    )

    blocks = runtime.store.snapshot().stable_blocks
    prompt_index = next(index for index, block in enumerate(blocks) if "FINAL PROMPT" in block.text)
    answer_index = next(index for index, block in enumerate(blocks) if block.text == "最终答案")
    assert prompt_index < answer_index


def test_gateway_thinking_delta_streams_into_active_block_with_timer() -> None:
    runtime = TuiRuntime("session-think-delta")
    runtime.enqueue_prompt("request-think-delta", "novel", queued=False)
    turn = runtime.begin_turn("request-think-delta")

    assert turn.on_gateway_event({"kind": "thinking_delta", "text": "先思考"})
    assert turn.on_gateway_event({"kind": "thinking_delta", "text": "再落笔"})

    active = [
        block for block in runtime.store.snapshot().active_blocks
        if block.role == "thinking"
    ]
    assert len(active) == 1
    assert active[0].text == "先思考再落笔"
    assert active[0].metadata.get("started_at", 0) > 0

    assert turn.on_gateway_event(
        {"kind": "assistant_thinking", "text": "完整思考", "duration_seconds": 12.0}
    )

    settled = [
        block for block in runtime.store.snapshot().stable_blocks
        if block.role == "thinking"
    ]
    assert len(settled) == 1
    assert settled[0].text == "完整思考"
    assert settled[0].metadata.get("duration_seconds") == 12.0


def test_late_full_thinking_after_answer_does_not_create_duplicate_bottom_block() -> None:
    runtime = TuiRuntime("session-late-thinking")
    runtime.enqueue_prompt("request-late-thinking", "verify", queued=False)
    turn = runtime.begin_turn("request-late-thinking")

    turn.on_gateway_event({"kind": "thinking_delta", "text": "先核对事实"})
    turn.on_gateway_event({"kind": "model_delta", "text": "最终答案"})
    assert not turn.on_gateway_event(
        {
            "kind": "assistant_thinking",
            "text": "先核对事实",
            "duration_seconds": 10.0,
        }
    )
    runtime.complete_turn(
        "request-late-thinking",
        TuiTurnSummary(response_text="最终答案", ok=True),
    )

    stable = runtime.store.snapshot().stable_blocks
    assert [block.role for block in stable] == ["user", "thinking", "assistant"]
    assert [block.text for block in stable if block.role == "thinking"] == ["先核对事实"]
    assert stable[-1].role == "assistant"


def test_gateway_tool_input_progress_is_transient_and_resettable() -> None:
    runtime = TuiRuntime("session-tool-input")
    runtime.enqueue_prompt("request-tool-input", "write", queued=False)
    turn = runtime.begin_turn("request-tool-input")

    assert turn.on_gateway_event(
        {
            "kind": "tool_input_progress",
            "progress": {
                "schema": "provider_tool_input_progress.v1",
                "phase": "started",
                "stream_index": 0,
                "tool": "write_file",
                "received_chars": 0,
            },
        }
    )
    assert turn.on_gateway_event(
        {
            "kind": "tool_input_progress",
            "progress": {
                "schema": "provider_tool_input_progress.v1",
                "phase": "streaming",
                "stream_index": 0,
                "tool": "write_file",
                "received_chars": 9_000,
            },
        }
    )
    active = [
        block
        for block in runtime.store.snapshot().active_blocks
        if block.role == "tool_input"
    ]
    assert len(active) == 1
    assert active[0].metadata["received_chars"] == 9_000

    assert turn.on_gateway_event({"kind": "tool_input_reset"})
    assert not any(
        block.role == "tool_input"
        for block in runtime.store.snapshot().active_blocks
    )
