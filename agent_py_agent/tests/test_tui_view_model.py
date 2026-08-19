from __future__ import annotations

from agent_py_agent.cli.chat_parts.tui_events import TuiEventSequencer
from agent_py_agent.cli.chat_parts.tui_view_model import (
    TuiStateStore,
    TuiViewModelReducer,
)


def _sequencer() -> TuiEventSequencer:
    return TuiEventSequencer(
        "test:turn",
        session_id="session-1",
        request_id="request-1",
        clock=lambda: 10.0,
    )


def test_assistant_stream_freezes_once_without_duplicate_final() -> None:
    store = TuiStateStore()
    seq = _sequencer()
    block_id = "assistant:request-1"
    store.publish(seq.emit("assistant_started", "started", block_id))
    store.publish(seq.emit("assistant_delta", "delta", block_id, {"text": "你"}))
    store.publish(seq.emit("assistant_delta", "delta", block_id, {"text": "好"}))
    terminal = seq.emit(
        "assistant_completed",
        "completed",
        block_id,
        {"text": "你好"},
    )
    assert store.publish(terminal).accepted
    assert store.publish(terminal).status == "duplicate"
    snapshot = store.snapshot()
    assert snapshot.active_blocks == ()
    assert len(snapshot.stable_blocks) == 1
    assert snapshot.stable_blocks[0].text == "你好"
    assert snapshot.stable_blocks[0].phase == "completed"


def test_delta_without_start_and_unknown_kind_fail_closed() -> None:
    store = TuiStateStore()
    seq = _sequencer()
    store.publish(seq.emit("assistant_delta", "delta", "missing", {"text": "secret internal"}))
    store.publish(seq.emit("future_private_event", "updated", "future", {"text": "do not render"}))
    snapshot = store.snapshot()
    assert snapshot.stable_blocks == ()
    assert snapshot.active_blocks == ()
    assert [item.code for item in snapshot.diagnostics] == [
        "DELTA_WITHOUT_ACTIVE_BLOCK",
        "UNKNOWN_EVENT_KIND",
    ]


def test_terminal_response_file_can_restore_complete_block() -> None:
    store = TuiStateStore()
    seq = _sequencer()
    store.publish(
        seq.emit(
            "assistant_completed",
            "completed",
            "assistant:restored",
            {"text": "从响应文件恢复的完整正文"},
        )
    )
    snapshot = store.snapshot()
    assert [block.text for block in snapshot.stable_blocks] == ["从响应文件恢复的完整正文"]
    assert snapshot.diagnostics == ()


def test_tool_permission_and_result_keep_one_block_identity() -> None:
    store = TuiStateStore()
    seq = _sequencer()
    block_id = "tool:request-1:1:1"
    store.publish(
        seq.emit(
            "tool_started",
            "started",
            block_id,
            {"tool": "run_command", "detail": "printf fixture"},
        )
    )
    store.publish(
        seq.emit(
            "permission_requested",
            "waiting_permission",
            block_id,
            {
                "permission_id": "approval-1",
                "title": "Bash command",
                "description": "printf fixture",
                "options": [
                    {"id": "allow", "label": "Yes"},
                    {"id": "deny", "label": "No"},
                ],
            },
        )
    )
    waiting = store.snapshot()
    assert waiting.permission is not None
    assert waiting.active_blocks[0].phase == "waiting_permission"
    store.publish(
        seq.emit(
            "permission_resolved",
            "updated",
            block_id,
            {"permission_id": "approval-1", "decision": "allow"},
        )
    )
    store.publish(
        seq.emit(
            "tool_completed",
            "completed",
            block_id,
            {"tool": "run_command", "detail": "fixture-tool-ok", "ok": True},
        )
    )
    completed = store.snapshot()
    assert completed.permission is None
    assert completed.active_blocks == ()
    assert len(completed.stable_blocks) == 1
    assert completed.stable_blocks[0].block_id == block_id
    assert completed.stable_blocks[0].detail == "fixture-tool-ok"


def test_permission_selection_and_feedback_are_typed_overlay_state() -> None:
    store = TuiStateStore()
    seq = _sequencer()
    block_id = "tool:request-1:2:1"
    store.publish(
        seq.emit(
            "tool_started",
            "started",
            block_id,
            {"tool": "run_command"},
        )
    )
    store.publish(
        seq.emit(
            "permission_requested",
            "waiting_permission",
            block_id,
            {
                "permission_id": "approval-feedback",
                "options": [
                    {
                        "id": "allow",
                        "label": "Yes",
                        "decision": "approved",
                        "feedback_type": "accept",
                        "feedback_placeholder": "tell my-agent what to do next",
                    },
                    {
                        "id": "deny",
                        "label": "No",
                        "decision": "denied",
                        "feedback_type": "reject",
                    },
                ],
            },
        )
    )
    store.publish(
        seq.emit(
            "permission_feedback_toggled",
            "updated",
            block_id,
            {
                "permission_id": "approval-feedback",
                "selected_index": 0,
                "feedback_mode": True,
                "feedback": "",
                "feedback_placeholder": "tell my-agent what to do next",
            },
        )
    )
    store.publish(
        seq.emit(
            "permission_feedback_changed",
            "updated",
            block_id,
            {
                "permission_id": "approval-feedback",
                "selected_index": 0,
                "feedback": "only inspect the target",
            },
        )
    )
    editing = store.snapshot().permission
    assert editing is not None
    assert editing.feedback_mode is True
    assert editing.feedback == "only inspect the target"

    store.publish(
        seq.emit(
            "permission_selection_changed",
            "updated",
            block_id,
            {
                "permission_id": "approval-feedback",
                "selected_index": 1,
                "feedback_mode": False,
                "feedback": "",
            },
        )
    )
    selected = store.snapshot().permission
    assert selected is not None
    assert selected.selected_index == 1
    assert selected.feedback_mode is False

    store.publish(
        seq.emit(
            "permission_resolved",
            "completed",
            block_id,
            {"permission_id": "approval-feedback", "decision": "denied"},
        )
    )
    store.publish(
        seq.emit(
            "tool_failed",
            "failed",
            block_id,
            {
                "tool": "run_command",
                "detail": "printf fixture",
                "ok": False,
                "error_code": "APPROVAL_REJECTED",
            },
        )
    )
    rejected = store.snapshot().stable_blocks[-1]
    assert rejected.phase == "failed"
    assert rejected.detail == "User rejected tool use"


def test_queue_is_priority_then_fifo_and_restore_removes_item() -> None:
    store = TuiStateStore()
    seq = _sequencer()
    for queue_id, priority in (("later-1", "later"), ("next-1", "next"), ("now-1", "now"), ("next-2", "next")):
        store.publish(
            seq.emit(
                "queue_added",
                "queued",
                queue_id,
                {"queue_id": queue_id, "text": queue_id, "priority": priority},
            )
        )
    assert [item.queue_id for item in store.snapshot().queued_inputs] == [
        "now-1",
        "next-1",
        "next-2",
        "later-1",
    ]
    store.publish(
        seq.emit(
            "queue_restored",
            "restored",
            "next-2",
            {"queue_id": "next-2"},
        )
    )
    assert [item.queue_id for item in store.snapshot().queued_inputs] == [
        "now-1",
        "next-1",
        "later-1",
    ]


def test_queue_promote_atomically_creates_user_block() -> None:
    store = TuiStateStore()
    seq = _sequencer()
    store.publish(
        seq.emit(
            "queue_added",
            "queued",
            "queue:request-1",
            {"queue_id": "queue:request-1", "text": "queued text", "priority": "next"},
        )
    )

    store.publish(
        seq.emit(
            "queue_promoted",
            "removed",
            "user:request-1",
            {"queue_id": "queue:request-1", "text": "queued text"},
        )
    )

    snapshot = store.snapshot()
    assert snapshot.queued_inputs == ()
    assert [(block.role, block.text) for block in snapshot.stable_blocks] == [
        ("user", "queued text")
    ]


def test_turn_and_status_fields_are_structured_not_text_parsed() -> None:
    store = TuiStateStore()
    seq = _sequencer()
    store.publish(seq.emit("turn_started", "started", "turn:1", {"activity": "Thinking"}))
    store.publish(
        seq.emit(
            "status_updated",
            "updated",
            "status:1",
            {"context_tokens": 8600, "tool_rounds": 2, "text": "ignore 999 tokens"},
        )
    )
    running = store.snapshot().status
    assert running.phase == "running"
    assert running.context_tokens == 8600
    assert running.tool_rounds == 2
    store.publish(
        seq.emit(
            "turn_interrupt_requested",
            "updated",
            "turn:1",
            {"activity": "Interrupting"},
        )
    )
    interrupting = store.snapshot().status
    assert interrupting.phase == "interrupting"
    assert interrupting.activity == "Interrupting"
    store.publish(seq.emit("turn_interrupted", "interrupted", "turn:1"))
    assert store.snapshot().status.phase == "interrupted"


def test_running_status_activity_time_advances_on_typed_stream_events() -> None:
    timestamps = iter((100.0, 101.0, 105.0, 109.0))
    seq = TuiEventSequencer("activity-clock", clock=lambda: next(timestamps))
    store = TuiStateStore()

    store.publish(seq.emit("turn_started", "started", "turn:activity"))
    store.publish(seq.emit("assistant_started", "started", "assistant:activity"))
    store.publish(
        seq.emit(
            "assistant_delta",
            "delta",
            "assistant:activity",
            {"text": "chunk"},
        )
    )
    assert store.snapshot().status.last_event_at == 105.0

    store.publish(
        seq.emit(
            "tool_started",
            "started",
            "tool:activity",
            {"tool": "read_file"},
        )
    )
    assert store.snapshot().status.last_event_at == 109.0


def test_journal_rejection_is_visible_as_bounded_diagnostic() -> None:
    store = TuiStateStore()
    seq = _sequencer()
    accepted = seq.emit("user_message", "completed", "user:1", {"text": "first"})
    assert store.publish(accepted).accepted
    conflict = type(accepted)(
        event_id=accepted.event_id,
        seq=accepted.seq + 1,
        stream_id=accepted.stream_id,
        block_id=accepted.block_id,
        kind=accepted.kind,
        phase=accepted.phase,
        payload={"text": "changed"},
    )
    result = store.publish(conflict)
    assert result.status == "rejected"
    assert store.snapshot().diagnostics[-1].code == "EVENT_ID_CONFLICT"


def test_stable_block_projection_is_bounded_without_reaccepting_old_identity() -> None:
    reducer = TuiViewModelReducer(max_stable_blocks=2)
    store = TuiStateStore(reducer=reducer)
    seq = TuiEventSequencer("bounded", clock=lambda: 9.0)
    first = seq.emit("user_message", "completed", "user-1", {"text": "one"})
    store.publish(first)
    store.publish(seq.emit("user_message", "completed", "user-2", {"text": "two"}))
    store.publish(seq.emit("user_message", "completed", "user-3", {"text": "three"}))

    assert [block.block_id for block in store.snapshot().stable_blocks] == [
        "user-2",
        "user-3",
    ]
    reducer.apply(first)
    assert [block.block_id for block in reducer.snapshot().stable_blocks] == [
        "user-2",
        "user-3",
    ]
