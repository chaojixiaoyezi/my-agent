# LLM: 使用原 outbox 和回执实现，显示回调与持久删除须分别等待真实证据。
# 模块用途: 检验控制投递的持久化、重启和终态合同。
from __future__ import annotations

import json
import threading
import time
from queue import Queue
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.control_commands import ConversationControlResult
from agent_py_agent.cli.chat_parts.tui_control_delivery import (
    TuiControlOperationEntry,
    TuiControlOperationReconciler,
    tui_control_operation_outbox_path,
)
from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime, TuiTurnSummary


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


# LLM: 完成回调先于原 outbox 删除，不能以 callback Event 冒充删除完成。
# 函数用途: 检验快速压缩收口显示且最终删除原持久消息。
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
    # 等待原 worker 在回调之后真正退出，保留下方原始空 outbox 断言。
    assert reconciler._worker is not None
    reconciler._worker.join(timeout=1.0)
    assert not reconciler._worker.is_alive()
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
    # _finish 先回调界面、再删 outbox 行；只等回调会在慢 runner 上读到尚未删除的行。
    deadline = time.monotonic() + 5
    while ("compact" not in completed or reconciler._read_entries()) and time.monotonic() < deadline:
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


def _request_target(turn_id="gwreq-turn-1", *, kind="request"):
    from agent_py_agent.agent.gateway_parts.execution_target import GatewayExecutionTarget

    return GatewayExecutionTarget("thread-1", kind, turn_id, "claim-1").to_dict()


def _post_turn_params(runtime, entries):
    return SimpleNamespace(
        use_gateway=True, tui_runtime=runtime, state_lock=threading.RLock(),
        is_running_ref=[True], running_request_id_ref=["gwreq-turn-1"],
        local_run_ref=[None], running_prompt_ref=["上一轮"], running_started_at_ref=[1.0],
        runtime_inject=[], prompt_files=[], args=SimpleNamespace(no_save=False, resume_context=None),
        current_session_id=runtime.session_id, jobs=Queue(), pending_jobs_ref_for_enqueue=[0],
        agent=SimpleNamespace(request_active_turn_input=lambda *_args, **_kwargs: pytest.fail("不应直接调用运输")),
        active_input_reconciler=SimpleNamespace(enqueue=entries.append),
    )


@pytest.mark.parametrize("late_poll", [False, True])
@pytest.mark.parametrize("reset_refs", [False, True])
@pytest.mark.parametrize("summary", [
    TuiTurnSummary(response_text="上一轮完成"),
    TuiTurnSummary(ok=False, error="执行失败"),
    TuiTurnSummary(ok=False, interrupted=True),
])
def test_post_turn_prompt_is_fresh_without_waiting_for_notice_poll(summary, late_poll, reset_refs):
    from agent_py_agent.cli.chat_parts import tui_actions
    from agent_py_agent.cli.chat_parts.tui_worker import _reset_worker_refs

    runtime, entries = TuiRuntime("post-turn-prompt"), []
    params = _post_turn_params(runtime, entries)
    runtime.register_gateway_request("gwreq-turn-1")
    runtime.begin_turn("chat-turn-1", task_progress_generation_id="gwreq-turn-1")
    polled_target = _request_target()
    runtime.publish_execution_target(polled_target)

    runtime.complete_turn("chat-turn-1", summary)
    if reset_refs:
        _reset_worker_refs(params)
    if late_poll:
        # 载荷在完成前已取得，完成后才发表；不推进时钟，也不等待下一次轮询。
        runtime.publish_execution_target(polled_target)
    text = "看下现在几点了"
    assert tui_actions._tui_submit_active_turn_input(params, text, display_text=text) is False
    assert entries == [] and runtime.store.snapshot().pending_steers == ()
    assert runtime.execution_target() is None
    tui_actions._tui_enqueue_job(params, text)
    job = params.jobs.get_nowait()
    assert job.user == text and job.request_id not in {"chat-turn-1", "gwreq-turn-1"}
    assert params.pending_jobs_ref_for_enqueue == [1]


def test_released_foreground_target_cannot_be_republished_without_response():
    runtime = TuiRuntime("released-target")
    target = _request_target()
    runtime.register_gateway_request("gwreq-turn-1")
    runtime.publish_execution_target(target)

    runtime.release_gateway_request("gwreq-turn-1")

    assert runtime.execution_target() is None
    assert runtime.publish_execution_target(target) is False
    assert runtime.execution_target() is None


@pytest.mark.parametrize("turn_id", ["chat-turn-1", "gwreq-turn-1"])
def test_complete_turn_retires_local_and_gateway_ids_without_ownership(turn_id):
    runtime = TuiRuntime("complete-without-ownership")
    runtime.begin_turn("chat-turn-1", task_progress_generation_id="gwreq-turn-1")
    target = _request_target(turn_id)
    runtime.publish_execution_target(target)

    runtime.complete_turn("chat-turn-1", TuiTurnSummary(ok=False, interrupted=True))

    assert runtime.execution_target() is None
    assert runtime.publish_execution_target(target) is False


@pytest.mark.parametrize("successor", [None, "request", "task"])
def test_enter_rechecks_completed_id_after_execution_snapshot_read(monkeypatch, successor):
    from agent_py_agent.cli.chat_parts import tui_actions
    from agent_py_agent.cli.chat_parts.tui_worker import _reset_worker_refs

    runtime, entries = TuiRuntime("enter-completion-race"), []
    params = _post_turn_params(runtime, entries)
    runtime.register_gateway_request("gwreq-turn-1")
    runtime.begin_turn("chat-turn-1", task_progress_generation_id="gwreq-turn-1")
    runtime.publish_execution_target(_request_target())
    read_target = runtime.execution_target
    replacement = _request_target("server-still-running", kind=successor) if successor else None

    def read_then_complete():
        captured = read_target()
        runtime.complete_turn("chat-turn-1", TuiTurnSummary(response_text="完成"))
        _reset_worker_refs(params)
        if replacement:
            runtime.publish_execution_target(replacement)
        return captured

    monkeypatch.setattr(runtime, "execution_target", read_then_complete)
    assert tui_actions._tui_submit_active_turn_input(params, "新消息", display_text="新消息") is False
    assert entries == [] and runtime.store.snapshot().pending_steers == ()
    assert read_target() == replacement, "清旧目标不能误清并发发表的新目标"


@pytest.mark.parametrize("kind", ["request", "task"])
def test_late_finished_request_does_not_replace_another_execution_target(kind):
    runtime = TuiRuntime("late-target-preserves-current")
    stale = _request_target()
    runtime.register_gateway_request("gwreq-turn-1")
    runtime.release_gateway_request("gwreq-turn-1")
    current = _request_target("remote-request" if kind == "request" else "gwreq-turn-1", kind=kind)
    runtime.publish_execution_target(current)

    assert runtime.publish_execution_target(stale) is False
    assert runtime.execution_target() == current


@pytest.mark.parametrize("kind", ["request", "task"])
def test_idle_local_worker_keeps_unfinished_server_target_steerable(kind):
    from agent_py_agent.cli.chat_parts import tui_actions

    runtime, entries = TuiRuntime("idle-server-target"), []
    params = _post_turn_params(runtime, entries)
    params.is_running_ref[0], params.running_request_id_ref[0] = False, ""
    runtime.register_gateway_request("gwreq-turn-1")
    runtime.release_gateway_request("gwreq-turn-1")
    current = _request_target("remote-request" if kind == "request" else "gwreq-turn-1", kind=kind)
    runtime.publish_execution_target(current)

    assert tui_actions._tui_submit_active_turn_input(params, "补充消息", display_text="补充消息") is True
    assert len(entries) == 1 and entries[0].expected_execution == current
    assert entries[0].expected_turn_id == current["turn_id"]
    assert [entry.text for entry in runtime.store.snapshot().pending_steers] == ["补充消息"]
    assert params.jobs.empty()


def test_completed_foreground_identity_window_is_bounded():
    from agent_py_agent.cli.chat_parts.tui_identity_window import TuiIdentityWindow

    runtime = TuiRuntime("bounded-completed-targets")
    assert isinstance(getattr(runtime, "_completed_gateway_requests", None), TuiIdentityWindow)
    runtime._completed_gateway_requests = TuiIdentityWindow(maximum=2)
    for turn_id in ("gwreq-one", "gwreq-two", "gwreq-three"):
        runtime.register_gateway_request(turn_id)
        runtime.release_gateway_request(turn_id)
    assert tuple(runtime._completed_gateway_requests) == ("gwreq-two", "gwreq-three")
    assert runtime.publish_execution_target(_request_target("gwreq-three")) is False


def test_notice_refresh_wakes_when_active_work_ends_without_phase_change():
    from agent_py_agent.cli.chat_parts.tui_threading import _NoticeRefreshWake

    runtime = TuiRuntime("completion-notice-wake")
    runtime.update_background_activity(1)
    before = runtime.store.snapshot()
    assert before.has_active_work
    wake = _NoticeRefreshWake(runtime, None)
    try:
        runtime.update_background_activity(0)
        after = runtime.store.snapshot()
        assert not after.has_active_work and after.status.phase == before.status.phase
        assert wake.event.is_set(), "活动结束必须打断通知等待，而非继续睡满空闲间隔"
    finally:
        wake.close()


def test_unknown_foreground_observation_does_not_retire_live_server_request():
    from agent_py_agent.cli.chat_parts import tui_actions
    from agent_py_agent.cli.chat_parts.tui_worker import _reset_worker_refs

    runtime, entries = TuiRuntime("unknown-foreground-observation"), []
    params = _post_turn_params(runtime, entries)
    runtime.register_gateway_request("gwreq-turn-1")
    runtime.begin_turn("chat-turn-1", task_progress_generation_id="gwreq-turn-1")
    target = _request_target()
    runtime.publish_execution_target(target)
    runtime.complete_turn("chat-turn-1", TuiTurnSummary(ok=False, outcome_unknown=True))
    _reset_worker_refs(params)

    runtime.publish_execution_target(target)
    assert tui_actions._tui_submit_active_turn_input(params, "继续核对", display_text="继续核对") is True
    assert len(entries) == 1 and entries[0].expected_execution == target
    assert runtime.execution_target() == target


def test_gateway_attachment_rollback_does_not_retire_live_server_request():
    from agent_py_agent.cli.chat_parts import tui_actions

    runtime, entries = TuiRuntime("attachment-rollback"), []
    params = _post_turn_params(runtime, entries)
    params.is_running_ref[0], params.running_request_id_ref[0] = False, ""
    target = _request_target()
    runtime.publish_execution_target(target)

    def reject_queue(_job):
        raise RuntimeError("local observer queue unavailable")

    params.jobs = SimpleNamespace(put=reject_queue)
    with pytest.raises(RuntimeError, match="local observer queue unavailable"):
        tui_actions._tui_attach_gateway_job(params, "已入队消息", display_text="已入队消息",
            gateway_request_id="gwreq-turn-1", client_message_id="input-1")
    assert params.pending_jobs_ref_for_enqueue == [0]
    runtime.publish_execution_target(target)
    assert tui_actions._tui_submit_active_turn_input(params, "继续核对", display_text="继续核对") is True
    assert len(entries) == 1 and entries[0].expected_execution == target
    params.jobs = Queue()
    tui_actions._tui_attach_gateway_job(params, "已入队消息", display_text="已入队消息",
        gateway_request_id="gwreq-turn-1", client_message_id="input-1")
    assert params.jobs.get_nowait().gateway_request_id == "gwreq-turn-1"


def test_steer_stop_new_then_next_prompt_has_no_stale_foreground_target():
    from agent_py_agent.cli.chat_parts import tui_actions
    from agent_py_agent.cli.chat_parts.tui_worker import _reset_worker_refs

    runtime, steers, controls = TuiRuntime("steer-stop-new-sequence"), [], []
    params = _post_turn_params(runtime, steers)
    params.control_operation_reconciler = SimpleNamespace(enqueue_background=controls.append)
    runtime.register_gateway_request("gwreq-turn-1")
    runtime.begin_turn("chat-turn-1", task_progress_generation_id="gwreq-turn-1")
    runtime.publish_execution_target(_request_target())
    assert tui_actions._tui_submit_active_turn_input(params, "补充证据", display_text="补充证据") is True
    assert len(steers) == 1 and steers[0].expected_turn_id == "gwreq-turn-1"
    assert tui_actions._tui_submit_control_operation(params, "/stop") is True
    assert len(controls) == 1 and controls[0].expected_turn_id == "gwreq-turn-1"
    assert controls[0].command_kind == "stop" and runtime.request_interrupt()
    runtime.complete_turn("chat-turn-1", TuiTurnSummary(ok=False, interrupted=True))
    _reset_worker_refs(params)
    # 模拟宿主已确认/new后的原显示入口，不以本地显示动作证明服务端真的执行了/new。
    runtime.publish_conversation_reset("thread-next")
    assert runtime.publish_execution_target(_request_target()) is False
    assert tui_actions._tui_submit_active_turn_input(params, "下一轮对话", display_text="下一轮对话") is False
    tui_actions._tui_enqueue_job(params, "下一轮对话")
    assert params.jobs.get_nowait().user == "下一轮对话"
    snapshot = runtime.store.snapshot()
    assert snapshot.pending_steers == () and not snapshot.has_active_work
    assert runtime.execution_target() is None
    assert not runtime._turns and not runtime._completing_turns
    assert not runtime._foreground_gateway_bindings and not runtime._owned_gateway_requests
