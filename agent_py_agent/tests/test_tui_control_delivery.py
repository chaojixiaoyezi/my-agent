from __future__ import annotations

import json
import threading
import time

import pytest

from agent_py_agent.agent.conversation.control_commands import ConversationControlResult
from agent_py_agent.cli.chat_parts.tui_control_delivery import (
    TuiControlOperationEntry,
    TuiControlOperationReconciler,
    tui_control_operation_outbox_path,
)
from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime


def _reconciler(tmp_path, *, submit, status, callbacks=None):
    events = callbacks if callbacks is not None else []
    stop_event = threading.Event()
    stop_event.set()
    return TuiControlOperationReconciler(
        path=tmp_path / "control-outbox.json",
        submit=submit,
        status=status,
        on_restore=lambda entry: events.append(("restore", entry.message_id)),
        on_complete=lambda entry, result: events.append(
            ("complete", entry.message_id, result.operation_id)
        ),
        on_terminal_unknown=lambda entry, result: events.append(
            ("unknown", entry.message_id, result.operation_id)
        ),
        on_conflict=lambda entry: events.append(("conflict", entry.message_id)),
        on_error=lambda error: events.append(("error", type(error).__name__)),
        stop_event=stop_event,
        initial_delay=0.01,
        maximum_delay=0.02,
    )


def test_control_outbox_persists_before_transport_and_rejects_identity_drift(tmp_path) -> None:
    calls: list[str] = []
    reconciler = _reconciler(
        tmp_path,
        submit=lambda entry: calls.append(entry.message_id),
        status=lambda operation_id: calls.append(operation_id),
    )
    entry = TuiControlOperationEntry(
        message_id="control-one",
        command_text="/btw 先核对证据",
        command_kind="steer",
        expected_turn_id="turn-a",
    )

    reconciler.enqueue(entry)

    assert calls == []
    payload = json.loads((tmp_path / "control-outbox.json").read_text(encoding="utf-8"))
    assert payload["entries"]["control-one"]["expected_turn_id"] == "turn-a"
    with pytest.raises(ValueError, match="different input"):
        reconciler.enqueue(
            TuiControlOperationEntry(
                message_id="control-one",
                command_text="/btw 换成另一条内容",
                command_kind="steer",
                expected_turn_id="turn-a",
            )
        )


def test_control_outbox_publishes_start_after_persist_and_before_dispatch(tmp_path) -> None:
    reconciler = _reconciler(
        tmp_path,
        submit=lambda _entry: pytest.fail("stopped worker must not submit"),
        status=lambda _operation_id: pytest.fail("stopped worker must not poll"),
    )
    entry = TuiControlOperationEntry(
        message_id="control-compact-order",
        command_text="/compact",
        command_kind="compact",
    )
    order: list[str] = []

    def on_persisted(persisted: TuiControlOperationEntry) -> None:
        payload = json.loads(
            (tmp_path / "control-outbox.json").read_text(encoding="utf-8")
        )
        assert persisted.message_id in payload["entries"]
        order.append("start")

    def ensure_worker_started() -> None:
        assert order == ["start"]
        order.append("dispatch")

    reconciler._ensure_worker_started = ensure_worker_started  # type: ignore[method-assign]
    reconciler.enqueue(entry, on_persisted_before_dispatch=on_persisted)

    assert order == ["start", "dispatch"]


def test_control_outbox_fast_compact_receipt_closes_started_block(tmp_path) -> None:
    runtime = TuiRuntime("control-compact-fast-worker")
    completed = threading.Event()
    stop_event = threading.Event()
    errors: list[BaseException] = []

    def on_complete(
        entry: TuiControlOperationEntry,
        _result: ConversationControlResult,
    ) -> None:
        runtime.publish_manual_compact_terminal(entry.message_id, succeeded=True)
        completed.set()

    reconciler = TuiControlOperationReconciler(
        path=tmp_path / "control-outbox.json",
        submit=lambda _entry: ConversationControlResult(
            "compact",
            True,
            "done",
            operation_id="gwctl-fast",
            control_state="completed",
        ),
        status=lambda _operation_id: pytest.fail("completed submit must not poll"),
        on_restore=lambda _entry: None,
        on_complete=on_complete,
        on_terminal_unknown=lambda _entry, _result: None,
        on_conflict=lambda _entry: None,
        on_error=errors.append,
        stop_event=stop_event,
        initial_delay=0.01,
        maximum_delay=0.02,
    )
    entry = TuiControlOperationEntry(
        message_id="control-compact-fast",
        command_text="/compact",
        command_kind="compact",
    )
    reconciler.enqueue(
        entry,
        on_persisted_before_dispatch=lambda persisted: (
            runtime.publish_manual_compact_started(persisted.message_id)
        ),
    )

    assert completed.wait(1.0)
    stop_event.set()
    reconciler._wake.set()
    snapshot = runtime.store.snapshot()
    assert snapshot.active_blocks == ()
    assert all(
        item.get("code") != "COMPACT_TERMINAL_WITHOUT_START"
        for item in snapshot.diagnostics
    )
    assert reconciler._read_entries() == {}
    assert errors == []


def test_terminal_receipt_removes_outbox_when_display_callback_fails(tmp_path) -> None:
    errors: list[BaseException] = []
    reconciler = _reconciler(
        tmp_path,
        submit=lambda _entry: ConversationControlResult(
            "compact",
            True,
            "done",
            operation_id="gwctl-display-failure",
            control_state="completed",
        ),
        status=lambda _operation_id: pytest.fail("completed submit must not poll"),
        callbacks=errors,
    )
    reconciler.on_complete = lambda _entry, _result: (_ for _ in ()).throw(
        RuntimeError("redraw failed")
    )
    reconciler.on_error = errors.append
    entry = TuiControlOperationEntry(
        message_id="control-display-failure",
        command_text="/compact",
        command_kind="compact",
    )
    reconciler.enqueue(entry)

    reconciler._reconcile_one(entry)

    assert reconciler._read_entries() == {}
    assert len(errors) == 1
    assert isinstance(errors[0], RuntimeError)


def test_exact_compact_stop_dispatches_while_compact_post_is_blocked(tmp_path) -> None:
    compact_entered = threading.Event()
    compact_release = threading.Event()
    stop_sent = threading.Event()
    completed: list[str] = []
    stop_event = threading.Event()

    def submit(entry: TuiControlOperationEntry) -> ConversationControlResult:
        if entry.command_kind == "compact":
            compact_entered.set()
            assert compact_release.wait(timeout=2)
            return ConversationControlResult(
                "compact",
                False,
                "interrupted",
                operation_id="gwctl-compact-blocked",
                control_state="completed",
                error_code="COMPACT_INTERRUPTED",
            )
        stop_sent.set()
        return ConversationControlResult(
            "stop",
            True,
            "stopping",
            operation_id="gwctl-stop-urgent",
            control_state="completed",
        )

    reconciler = TuiControlOperationReconciler(
        path=tmp_path / "control-outbox.json",
        submit=submit,
        status=lambda _operation_id: pytest.fail("submit completes in one response"),
        on_restore=lambda _entry: None,
        on_complete=lambda entry, _result: completed.append(entry.command_kind),
        on_terminal_unknown=lambda _entry, _result: None,
        on_conflict=lambda _entry: None,
        on_error=lambda error: pytest.fail(str(error)),
        stop_event=stop_event,
        initial_delay=0.01,
        maximum_delay=0.02,
    )
    reconciler.enqueue(
        TuiControlOperationEntry(
            message_id="control-compact-blocked",
            command_text="/compact",
            command_kind="compact",
        )
    )
    assert compact_entered.wait(timeout=1)

    reconciler.enqueue(
        TuiControlOperationEntry(
            message_id="control-stop-urgent",
            command_text="/stop",
            command_kind="stop",
            target_control_message_id="control-compact-blocked",
        )
    )

    assert stop_sent.wait(timeout=0.5)
    assert completed == ["stop"]
    compact_release.set()
    deadline = time.monotonic() + 1
    while "compact" not in completed and time.monotonic() < deadline:
        time.sleep(0.01)
    stop_event.set()
    reconciler._wake.set()
    assert sorted(completed) == ["compact", "stop"]
    assert reconciler._read_entries() == {}


def test_control_outbox_switches_to_get_only_after_operation_id(tmp_path) -> None:
    submits: list[str] = []
    statuses: list[str] = []
    events: list[tuple[str, ...]] = []

    def submit(entry):
        submits.append(entry.message_id)
        return ConversationControlResult(
            "steer",
            True,
            "等待投递确认",
            request_id="turn-a",
            delivery_status="unknown",
            operation_id="gwctl-one",
            control_state="completed",
        )

    def status(operation_id):
        statuses.append(operation_id)
        return ConversationControlResult(
            "steer",
            True,
            "已投递",
            request_id="turn-a",
            delivery_status="accepted",
            operation_id=operation_id,
            control_state="completed",
        )

    reconciler = _reconciler(
        tmp_path,
        submit=submit,
        status=status,
        callbacks=events,
    )
    entry = TuiControlOperationEntry(
        message_id="control-one",
        command_text="/btw 先核对证据",
        command_kind="steer",
        expected_turn_id="turn-a",
    )
    reconciler.enqueue(entry)

    reconciler._reconcile_one(entry)
    stored = reconciler._read_entries()["control-one"]
    assert stored.operation_id == "gwctl-one"
    assert submits == ["control-one"]
    assert statuses == []

    reconciler._reconcile_one(stored)

    assert submits == ["control-one"]
    assert statuses == ["gwctl-one"]
    assert reconciler._read_entries() == {}
    assert events == [("complete", "control-one", "gwctl-one")]


def test_control_outbox_terminal_unknown_is_not_reposted(tmp_path) -> None:
    events: list[tuple[str, ...]] = []
    submit_calls: list[str] = []

    def submit(entry):
        submit_calls.append(entry.message_id)
        return ConversationControlResult(
            "compact",
            False,
            "副作用是否发生无法确认",
            operation_id="gwctl-unknown",
            control_state="terminal_unknown",
        )

    reconciler = _reconciler(
        tmp_path,
        submit=submit,
        status=lambda _operation_id: pytest.fail("terminal receipt must not be polled"),
        callbacks=events,
    )
    entry = TuiControlOperationEntry(
        message_id="control-compact",
        command_text="/compact 保留未完成事项",
        command_kind="compact",
    )
    reconciler.enqueue(entry)

    reconciler._reconcile_one(entry)

    assert submit_calls == ["control-compact"]
    assert reconciler._read_entries() == {}
    assert events == [("unknown", "control-compact", "gwctl-unknown")]


def test_btw_terminal_unknown_keeps_get_only_reconciliation(tmp_path) -> None:
    submits: list[str] = []
    statuses: list[str] = []
    events: list[tuple[str, ...]] = []

    def submit(entry):
        submits.append(entry.message_id)
        return ConversationControlResult(
            "steer",
            False,
            "effect boundary unknown",
            request_id="turn-a",
            delivery_status="unknown",
            operation_id="gwctl-steer",
            control_state="terminal_unknown",
        )

    def status(operation_id):
        statuses.append(operation_id)
        return ConversationControlResult(
            "steer",
            True,
            "accepted",
            request_id="turn-a",
            delivery_status="accepted",
            operation_id=operation_id,
            control_state="completed",
        )

    reconciler = _reconciler(
        tmp_path,
        submit=submit,
        status=status,
        callbacks=events,
    )
    entry = TuiControlOperationEntry(
        message_id="control-steer",
        command_text="/btw 继续核对",
        command_kind="steer",
        expected_turn_id="turn-a",
    )
    reconciler.enqueue(entry)

    reconciler._reconcile_one(entry)
    stored = reconciler._read_entries()["control-steer"]
    assert stored.operation_id == "gwctl-steer"
    assert submits == ["control-steer"]
    assert events == []

    reconciler._reconcile_one(stored)

    assert submits == ["control-steer"]
    assert statuses == ["gwctl-steer"]
    assert reconciler._read_entries() == {}
    assert events == [("complete", "control-steer", "gwctl-steer")]


def test_control_outbox_restart_restores_exact_pending_row(tmp_path) -> None:
    path = tmp_path / "control-outbox.json"
    first = _reconciler(tmp_path, submit=lambda _entry: None, status=lambda _id: None)
    first.enqueue(
        TuiControlOperationEntry(
            message_id="control-stop",
            command_text="/stop",
            command_kind="stop",
            expected_turn_id="turn-stop",
            operation_id="gwctl-stop",
        )
    )
    restored: list[tuple[str, ...]] = []
    stop_event = threading.Event()
    stop_event.set()

    TuiControlOperationReconciler(
        path=path,
        submit=lambda _entry: pytest.fail("constructor must not submit"),
        status=lambda _id: pytest.fail("constructor must not poll"),
        on_restore=lambda entry: restored.append(
            (entry.message_id, entry.expected_turn_id, entry.operation_id)
        ),
        on_complete=lambda _entry, _result: None,
        on_terminal_unknown=lambda _entry, _result: None,
        on_conflict=lambda _entry: None,
        on_error=lambda error: pytest.fail(str(error)),
        stop_event=stop_event,
        initial_delay=0.01,
        maximum_delay=0.02,
    )

    assert restored == [("control-stop", "turn-stop", "gwctl-stop")]


def test_control_outbox_path_does_not_embed_session_text(tmp_path) -> None:
    path = tui_control_operation_outbox_path(tmp_path, "../../another/session")

    assert path.parent == tmp_path / "client_outbox"
    assert path.name.startswith("tui-control-")
    assert "session" not in path.name
