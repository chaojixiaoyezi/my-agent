"""NW2 Slice W3 regressions for TUI observer weak refs, bounded /expand, outbox backoff, and observer deadlines."""

from __future__ import annotations

import errno
import gc
import http.server
import json
import queue
import socketserver
import threading
import time
import urllib.request
import weakref
from types import SimpleNamespace

from agent_py_agent.agent.conversation.control_commands import ConversationControlResult
from agent_py_agent.cli.chat_parts import (
    gateway_client,
    tui,
    tui_actions,
    tui_control_delivery,
    tui_threading,
    tui_worker,
)
from agent_py_agent.cli.chat_parts.gateway_client import (
    GatewayChunkPollRequest,
    NetworkGatewayObserver,
)
from agent_py_agent.cli.chat_parts.plain_state import ChatJob
from agent_py_agent.cli.chat_parts.tui_agent_navigation import TuiAgentNavigationState
from agent_py_agent.cli.chat_parts.tui_control_delivery import (
    TuiControlOperationEntry,
    TuiControlOperationReconciler,
)
from agent_py_agent.cli.chat_parts.tui_events import TuiEventSequencer
from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime, TuiTurnSummary
from agent_py_agent.cli.chat_parts.tui_view_model import TuiStateStore, TuiViewModelReducer


class _ThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def test_nw06_navigation_eviction_releases_wake_and_cadence_stores() -> None:
    root = TuiRuntime("nw06-root")
    navigation = TuiAgentNavigationState(root, max_cached_runtimes=32)
    stop = threading.Event()
    wake = tui_threading._NoticeRefreshWake(root, navigation, stop)
    cadence = tui_threading._RefreshCadence(stop, navigation)

    store_refs: list[weakref.ref] = []
    runtime_refs: list[weakref.ref] = []
    try:
        rows = [{"run_id": f"run-{idx:03d}", "status": "RUNNING"} for idx in range(100)]
        batches = (rows[:50], rows[50:])
        for batch_num, batch in enumerate(batches):
            navigation.update_rows("", batch)
            for batch_idx, _row in enumerate(batch):
                assert navigation.move_selection(1) is True
                assert navigation.enter_selected() is True
                active = navigation.active_runtime()
                store_refs.append(weakref.ref(active.store))
                runtime_refs.append(weakref.ref(active))
                cadence.delay(active)
                if not (batch_num == 1 and batch_idx == len(batch) - 1):
                    assert navigation.back() is True

        active_view = navigation.active_runtime()
        active_store = active_view.store
        del active

        gc.collect()

        # Early evicted views beyond the 32-view LRU must be reclaimable by GC.
        reclaimed_stores = sum(1 for ref in store_refs[:60] if ref() is None)
        reclaimed_runtimes = sum(1 for ref in runtime_refs[:60] if ref() is None)
        assert reclaimed_stores >= 55
        assert reclaimed_runtimes >= 55

        # Retained entries stay bounded to root + 32 cached views.
        assert len(wake.states) <= 33
        assert len(cadence.subscribed) <= 33
        assert active_store in wake.states
        assert active_store in cadence.subscribed

        # Active view still triggers both wake and cadence refresh signals.
        wake.event.clear()
        cadence.signals.event.clear()
        seq = TuiEventSequencer("nw06-active", clock=lambda: 1.0)
        active_store.publish(seq.emit("user_message", "completed", "msg-active", {"text": "ping"}))
        assert wake.event.is_set()
        assert cadence.signals.event.is_set()
    finally:
        stop.set()
        wake.close()
        cadence.signals.close()


def _expand_params(runtime: TuiRuntime, outputs: list[str], user_cmd: str) -> SimpleNamespace:
    return SimpleNamespace(
        user=user_cmd,
        agent=SimpleNamespace(config=SimpleNamespace()),
        args=SimpleNamespace(memory_limit=10),
        runtime_inject=[],
        prompt_files=[],
        use_gateway=False,
        plugin_client=None,
        plugin_revision="",
        command_interaction=None,
        current_session_id="nw07-session",
        state_lock=threading.RLock(),
        is_running_ref=[False],
        pending_jobs_ref=[0],
        running_prompt_ref=[""],
        running_request_id_ref=[""],
        running_started_at_ref=[0.0],
        local_run_ref=[None],
        shutting_down_ref=[False],
        assistant_outputs=outputs,
        tui_runtime=runtime,
        stop_event=threading.Event(),
    )


def test_nw2_pd1_expand_bounded_window_and_monotonic_ordinals(capsys) -> None:
    runtime = TuiRuntime(
        "nw07-bounded",
        store=TuiStateStore(reducer=TuiViewModelReducer(max_stable_blocks=3)),
    )
    outputs: list[str] = []
    history: list[tuple[str, str]] = []
    cfg = tui_worker.TuiWorkerConfig(
        jobs=queue.Queue(),
        state_lock=threading.RLock(),
        is_running_ref=[False],
        pending_jobs_ref=[0],
        running_prompt_ref=[""],
        running_request_id_ref=[""],
        running_started_at_ref=[0.0],
        agent=SimpleNamespace(config=SimpleNamespace(conversation_history_max_turns=20)),
        args=SimpleNamespace(),
        paths=None,
        use_gateway=False,
        conversation_history=history,
        history_lock=threading.RLock(),
        build_history_context=lambda: "",
        assistant_outputs=outputs,
        last_token_estimate_ref=[0],
        stop_event=threading.Event(),
        current_session_id="nw07-session",
        tui_runtime=runtime,
    )

    for idx in range(1, 6):
        job = ChatJob(
            user=f"prompt-{idx}",
            show_prompt=False,
            inject=[],
            prompt_files=[],
            request_id=f"req-{idx}",
        )
        cfg.jobs.put(job)
        cfg.jobs.get_nowait()
        tui_worker._tui_cleanup_after_job(cfg, job, f"reply-{idx}", False)

    assert outputs == ["reply-3", "reply-4", "reply-5"]
    assert runtime._assistant_reply_first_number == 3

    capsys.readouterr()
    assert tui._tui_handle_command(params=_expand_params(runtime, outputs, "/expand last")) is True
    out_last = capsys.readouterr().out
    assert "===== 助手回复 #5 =====" in out_last
    assert "reply-5" in out_last

    assert tui._tui_handle_command(params=_expand_params(runtime, outputs, "/expand 3")) is True
    out_retained = capsys.readouterr().out
    assert "===== 助手回复 #3 =====" in out_retained
    assert "reply-3" in out_retained

    assert tui._tui_handle_command(params=_expand_params(runtime, outputs, "/expand 1")) is True
    out_evicted = capsys.readouterr().out
    assert "#3～#5" in out_evicted
    assert "会话历史" in out_evicted

    assert tui._tui_handle_command(params=_expand_params(runtime, outputs, "/expand 6")) is True
    out_future = capsys.readouterr().out
    assert "#3～#5" in out_future

    assert tui._tui_handle_command(params=_expand_params(runtime, outputs, "/expand 0")) is True
    out_zero = capsys.readouterr().out
    assert "#3～#5" in out_zero

    runtime.publish_conversation_reset("thread-reset-1")
    assert outputs == []
    assert runtime._assistant_reply_first_number == 1

    assert tui._tui_handle_command(params=_expand_params(runtime, outputs, "/expand last")) is True
    assert "当前没有可展开的助手回复。" in capsys.readouterr().out

    job_after = ChatJob(
        user="prompt-after-reset",
        show_prompt=False,
        inject=[],
        prompt_files=[],
        request_id="req-after",
    )
    cfg.jobs.put(job_after)
    cfg.jobs.get_nowait()
    tui_worker._tui_cleanup_after_job(cfg, job_after, "fresh-reply", False)

    assert outputs == ["fresh-reply"]
    assert runtime._assistant_reply_first_number == 1
    assert tui._tui_handle_command(params=_expand_params(runtime, outputs, "/expand 1")) is True
    out_fresh = capsys.readouterr().out
    assert "===== 助手回复 #1 =====" in out_fresh
    assert "fresh-reply" in out_fresh


def test_nw2_pd1_isolated_expand_and_cleanup_without_runtime(capsys) -> None:
    capsys.readouterr()
    assert tui._tui_handle_expand_command("/expand 9", ["only-one"]) is True
    assert "没有编号为 9 的助手回复；当前共有 1 条。" in capsys.readouterr().out

    isolated_outputs: list[str] = []
    jobs: queue.Queue = queue.Queue()
    job = ChatJob(user="u", show_prompt=False, inject=[], prompt_files=[], request_id="r")
    jobs.put(job)
    jobs.get_nowait()
    cfg = SimpleNamespace(
        jobs=jobs,
        state_lock=threading.RLock(),
        is_running_ref=[False],
        pending_jobs_ref=[0],
        running_prompt_ref=[""],
        running_request_id_ref=[""],
        running_started_at_ref=[0.0],
        agent=SimpleNamespace(config=SimpleNamespace(conversation_history_max_turns=10)),
        conversation_history=[],
        history_lock=threading.RLock(),
        assistant_outputs=isolated_outputs,
        local_run_ref=[None],
        tui_runtime=None,
    )
    tui_worker._tui_cleanup_after_job(cfg, job, "isolated-reply", False)
    assert isolated_outputs == ["isolated-reply"]


def test_nw2_pd1_steer_stop_new_then_next_turn_sequence(monkeypatch, capsys) -> None:
    from agent_py_agent.agent.gateway_parts.execution_target import GatewayExecutionTarget
    from agent_py_agent.cli.chat_parts import control_runtime

    runtime = TuiRuntime(
        "nw07-sequence",
        store=TuiStateStore(reducer=TuiViewModelReducer(max_stable_blocks=2)),
    )
    outputs: list[str] = []
    steers: list[object] = []
    controls: list[object] = []
    jobs: queue.Queue = queue.Queue()
    stop_event = threading.Event()
    turn1_started = threading.Event()
    turn1_release = threading.Event()
    turn2_done = threading.Event()

    cfg = tui_worker.TuiWorkerConfig(
        jobs=jobs,
        state_lock=threading.RLock(),
        is_running_ref=[False],
        pending_jobs_ref=[0],
        running_prompt_ref=[""],
        running_request_id_ref=[""],
        running_started_at_ref=[0.0],
        agent=SimpleNamespace(
            config=SimpleNamespace(conversation_history_max_turns=20, gateway_request_timeout=5.0),
        ),
        args=SimpleNamespace(gateway_timeout=None),
        paths=None,
        use_gateway=True,
        conversation_history=[],
        history_lock=threading.RLock(),
        build_history_context=lambda: "",
        assistant_outputs=outputs,
        last_token_estimate_ref=[0],
        stop_event=stop_event,
        current_session_id="nw07-sequence",
        tui_runtime=runtime,
    )
    params = _expand_params(runtime, outputs, "")
    params.agent = SimpleNamespace(
        config=SimpleNamespace(),
        request_active_turn_input=lambda *a, **kw: None,
    )
    params.use_gateway = True
    params.jobs = jobs
    params.state_lock = cfg.state_lock
    params.is_running_ref = cfg.is_running_ref
    params.pending_jobs_ref = cfg.pending_jobs_ref
    params.pending_jobs_ref_for_enqueue = cfg.pending_jobs_ref
    params.running_prompt_ref = cfg.running_prompt_ref
    params.running_request_id_ref = cfg.running_request_id_ref
    params.running_started_at_ref = cfg.running_started_at_ref
    params.local_run_ref = cfg.local_run_ref
    params.stop_event = stop_event
    params.active_input_reconciler = SimpleNamespace(enqueue=steers.append)
    params.control_operation_reconciler = SimpleNamespace(enqueue_background=controls.append)

    def fake_prepare(_cfg, job) -> None:
        if not getattr(job, "gateway_request_id", ""):
            job.gateway_request_id = f"gw-{job.request_id}"
        runtime.register_gateway_request(job.gateway_request_id)

    def fake_process(_cfg, job, _turn_adapter):
        if job.request_id == "turn-1":
            target = GatewayExecutionTarget("thread-1", "request", "gw-turn-1", "claim-1").to_dict()
            runtime.publish_execution_target(target)
            turn1_started.set()
            assert turn1_release.wait(timeout=3.0)
            return "", False, TuiTurnSummary(ok=False, interrupted=True)
        turn2_done.set()
        return "next-turn-reply", False, TuiTurnSummary(response_text="next-turn-reply", ok=True)

    monkeypatch.setattr(tui_worker, "_tui_prepare_gateway_job", fake_prepare)
    monkeypatch.setattr(tui_worker, "_tui_process_job", fake_process)
    monkeypatch.setattr(
        control_runtime,
        "execute_chat_control",
        lambda _exec, _cmd: ConversationControlResult(
            "new",
            True,
            "已创建新会话",
            details={"view_reset": True, "thread_id": "thread-next"},
        ),
    )

    worker_thread = threading.Thread(target=tui_worker._tui_worker_body, args=(cfg,), daemon=True)
    worker_thread.start()
    try:
        # Turn 1 + steer + /stop
        runtime.enqueue_prompt("turn-1", "first-prompt", queued=False)
        jobs.put(ChatJob(user="first-prompt", show_prompt=False, inject=[], prompt_files=[], request_id="turn-1"))
        assert turn1_started.wait(timeout=2.0)

        assert tui_actions._tui_submit_active_turn_input(params, "supplement", display_text="supplement") is True
        assert len(steers) == 1 and steers[0].expected_turn_id == "gw-turn-1"

        assert tui_actions._tui_submit_control_operation(params, "/stop") is True
        assert len(controls) == 1 and controls[0].command_kind == "stop"
        assert runtime.request_interrupt() is True
        turn1_release.set()

        # Wait for turn 1 to finish before issuing /new.
        deadline = time.monotonic() + 3.0
        while cfg.is_running_ref[0] and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not cfg.is_running_ref[0]

        # /new resets runtime, clears outputs, and resets reply ordinal to 1.
        params.user = "/new"
        assert tui._tui_handle_command(params=params) is True
        assert outputs == []
        assert runtime._assistant_reply_first_number == 1

        # Next turn runs cleanly and renders as #1.
        runtime.enqueue_prompt("turn-2", "second-prompt", queued=False)
        jobs.put(ChatJob(user="second-prompt", show_prompt=False, inject=[], prompt_files=[], request_id="turn-2"))
        assert turn2_done.wait(timeout=2.0)

        deadline = time.monotonic() + 3.0
        while (outputs != ["next-turn-reply"] or cfg.is_running_ref[0]) and time.monotonic() < deadline:
            time.sleep(0.01)

        snap = runtime.store.snapshot()
        assert outputs == ["next-turn-reply"]
        assert runtime._assistant_reply_first_number == 1
        assert snap.pending_steers == ()
        assert snap.queued_inputs == ()
        assert not runtime._turns
        assert not runtime._completing_turns
        assert not runtime._owned_gateway_requests

        capsys.readouterr()
        params.user = "/expand 1"
        assert tui._tui_handle_command(params=params) is True
        expanded = capsys.readouterr().out
        assert "===== 助手回复 #1 =====" in expanded
        assert "next-turn-reply" in expanded
    finally:
        stop_event.set()
        turn1_release.set()
        worker_thread.join(timeout=2.0)


def test_nw08_control_outbox_enospc_backoff_and_recovery_with_real_http(tmp_path, monkeypatch) -> None:
    status_hits: list[tuple[str, float]] = []
    disk_full = [False]
    hit_lock = threading.Lock()

    class _StatusHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            op_id = self.path.rsplit("/", 1)[-1]
            with hit_lock:
                status_hits.append((op_id, time.monotonic()))
                if len(status_hits) == 1:
                    disk_full[0] = True
            body = json.dumps({"ok": True, "operation_id": op_id, "control_state": "completed"}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

    server = _ThreadingServer(("127.0.0.1", 0), _StatusHandler)
    port = server.server_address[1]
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    outbox_path = tmp_path / "control-outbox.json"
    orig_write = tui_control_delivery.write_json_file_atomic

    def guarded_write(path, body_data):
        if disk_full[0] and str(path) == str(outbox_path):
            raise OSError(errno.ENOSPC, "No space left on device")
        return orig_write(path, body_data)

    monkeypatch.setattr(tui_control_delivery, "write_json_file_atomic", guarded_write)

    stop_event = threading.Event()
    completed: list[str] = []
    completed_event = threading.Event()
    errors: list[BaseException] = []
    first_error = threading.Event()

    def status_fn(operation_id: str) -> ConversationControlResult:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/status/{operation_id}", timeout=2.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return ConversationControlResult(
            "compact",
            bool(data["ok"]),
            "done",
            operation_id=data["operation_id"],
            control_state=data["control_state"],
        )

    def on_complete(_entry: TuiControlOperationEntry, result: ConversationControlResult) -> None:
        completed.append(result.operation_id)
        completed_event.set()

    def on_error(exc: BaseException) -> None:
        errors.append(exc)
        first_error.set()

    try:
        reconciler = TuiControlOperationReconciler(
            path=outbox_path,
            submit=lambda _entry: ConversationControlResult("compact", False, "unexpected"),
            status=status_fn,
            on_restore=lambda _entry: None,
            on_complete=on_complete,
            on_terminal_unknown=lambda _entry, _result: None,
            on_conflict=lambda _entry: None,
            on_error=on_error,
            stop_event=stop_event,
            initial_delay=0.05,
            maximum_delay=0.08,
        )
        reconciler.enqueue(
            TuiControlOperationEntry(
                message_id="control-nw08",
                command_text="/compact",
                command_kind="compact",
                operation_id="gwctl-nw08",
            )
        )

        assert first_error.wait(timeout=2.0)
        # While disk_full is True for 0.30s, outbox reads must not block on locks
        # and HTTP status attempts must stay bounded by maximum_delay (no hot spin).
        start = time.monotonic()
        read_entries = reconciler._read_entries()
        assert "control-nw08" in read_entries
        assert time.monotonic() - start < 0.15

        time.sleep(0.30)
        with hit_lock:
            hits_during_outage = len(status_hits)
        assert 1 <= hits_during_outage <= 6
        assert any(isinstance(exc, OSError) and exc.errno == errno.ENOSPC for exc in errors)

        # Restore disk and verify clean completion on the exact same operation_id.
        disk_full[0] = False
        assert completed_event.wait(timeout=2.0)
        assert completed[-1] == "gwctl-nw08"

        deadline = time.monotonic() + 2.0
        while reconciler._read_entries() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert reconciler._read_entries() == {}
    finally:
        stop_event.set()
        server.shutdown()
        server.server_close()


def test_nw08_stop_event_wakes_enospc_backoff_immediately(tmp_path, monkeypatch) -> None:
    outbox_path = tmp_path / "control-outbox-stop.json"
    orig_write = tui_control_delivery.write_json_file_atomic
    disk_full = [False]

    def guarded_write(path, body_data):
        if disk_full[0] and str(path) == str(outbox_path):
            raise OSError(errno.ENOSPC, "No space left on device")
        return orig_write(path, body_data)

    monkeypatch.setattr(tui_control_delivery, "write_json_file_atomic", guarded_write)

    stop_event = threading.Event()
    error_seen = threading.Event()

    def status_fn(operation_id: str) -> ConversationControlResult:
        disk_full[0] = True
        return ConversationControlResult(
            "compact",
            True,
            "done",
            operation_id=operation_id,
            control_state="completed",
        )

    reconciler = TuiControlOperationReconciler(
        path=outbox_path,
        submit=lambda _entry: ConversationControlResult("compact", False, "unexpected"),
        status=status_fn,
        on_restore=lambda _entry: None,
        on_complete=lambda _entry, _result: None,
        on_terminal_unknown=lambda _entry, _result: None,
        on_conflict=lambda _entry: None,
        on_error=lambda _exc: error_seen.set(),
        stop_event=stop_event,
        initial_delay=0.05,
        maximum_delay=5.0,
    )
    try:
        reconciler.enqueue(
            TuiControlOperationEntry(
                message_id="control-nw08-stop",
                command_text="/compact",
                command_kind="compact",
                operation_id="gwctl-nw08-stop",
            )
        )
        assert error_seen.wait(timeout=2.0)

        t0 = time.monotonic()
        stop_event.set()
        assert reconciler._worker is not None
        reconciler._worker.join(timeout=0.5)
        assert not reconciler._worker.is_alive()
        assert time.monotonic() - t0 < 0.5
    finally:
        stop_event.set()


def test_a01_network_gateway_observer_http_and_sse_deadlines(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        gateway_client,
        "_WAIT_POLL_STEPS_SECONDS",
        (0.02, 0.05, 0.05, 0.05),
        raising=False,
    )
    import agent_py_agent.agent.gateway_parts.request_worker as rw

    monkeypatch.setattr(rw, "_WAIT_POLL_STEPS_SECONDS", (0.02, 0.05, 0.05, 0.05))

    # Part 1: HTTP mode with on_wait_timeout set -> current_remaining_seconds advances with rolling_deadline.
    state_lock = threading.Lock()
    result_calls = [0]
    progress_calls = [0]

    class _HttpObserverHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path.startswith("/client/requests/") and "/events" in self.path:
                self.send_response(503)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if self.path.startswith("/result/"):
                with state_lock:
                    result_calls[0] += 1
                    call_idx = result_calls[0]
                if call_idx == 1:
                    time.sleep(0.15)
                elif call_idx == 2:
                    time.sleep(0.38)
                body_obj = (
                    {"status": "processing"}
                    if call_idx < 3
                    else {"ok": True, "status": "completed", "response": "http-done"}
                )
                status = 202 if call_idx < 3 else 200
            elif self.path.startswith("/progress/"):
                with state_lock:
                    progress_calls[0] += 1
                    p_idx = progress_calls[0]
                if p_idx == 1:
                    body_obj = {
                        "events": [{"kind": "assistant_text_delta", "text": "part-1", "seq": 0}],
                        "next": 1,
                    }
                else:
                    body_obj = {"events": [], "next": 1}
                status = 200
            else:
                body_obj = {}
                status = 404
            body = json.dumps(body_obj).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

    http_server = _ThreadingServer(("127.0.0.1", 0), _HttpObserverHandler)
    http_thread = threading.Thread(target=http_server.serve_forever, daemon=True)
    http_thread.start()
    try:
        agent = SimpleNamespace(
            config=SimpleNamespace(gateway_request_timeout=2.0),
            gateway_network_headers=lambda: {},
        )
        observer = NetworkGatewayObserver(agent, f"http://127.0.0.1:{http_server.server_address[1]}")
        recorded_deadlines: list[tuple[str, float, float]] = []
        orig_request_json = observer.request_json

        def spy_request_json(method: str, path: str, body_data: object = None, *, total_deadline_seconds: float = 0.0):
            recorded_deadlines.append((path, time.monotonic(), float(total_deadline_seconds)))
            return orig_request_json(method, path, body_data, total_deadline_seconds=total_deadline_seconds)

        observer.request_json = spy_request_json  # type: ignore[method-assign]
        timeouts: list[float] = []
        emitted: list[dict] = []
        t0 = time.monotonic()
        res = observer(
            GatewayChunkPollRequest(
                tmp_path / "req-http.chunks.jsonl",
                tmp_path / "req-http.json",
                time.time() + 0.10,
                lambda _text: None,
                [0],
                [0],
                inactivity_timeout_seconds=0.35,
                on_event=emitted.append,
                on_wait_timeout=lambda: timeouts.append(time.monotonic()),
                is_wait_cancelled=lambda: False,
            )
        )
        assert res["response"] == "http-done"
        assert len(timeouts) >= 1
        post_deadline_result_budgets = [
            rem for path, ts, rem in recorded_deadlines if path.startswith("/result/") and ts > t0 + 0.10
        ]
        post_timeout_budgets = [
            rem for _path, ts, rem in recorded_deadlines if ts > timeouts[0]
        ]
        assert post_deadline_result_budgets
        assert post_timeout_budgets
        assert all(rem > 0.18 for rem in post_deadline_result_budgets), post_deadline_result_budgets
        assert all(rem > 0.18 for rem in post_timeout_budgets), post_timeout_budgets
        assert len(emitted) == 1
    finally:
        http_server.shutdown()
        http_server.server_close()

    # Part 2: SSE mode with on_wait_timeout set -> progress frame advances hard_mono_deadline
    # so after the first short-timeout SSE socket recycles, reconnect gets the extended
    # socket budget (>0.24s) and completes over SSE without spurious timeouts or _http_only.
    class _SseAdvanceHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            last_id = self.headers.get("Last-Event-ID", "")
            try:
                if not last_id:
                    time.sleep(0.06)
                    frame1 = (
                        "id: v1:req-sse:a:1:1\n"
                        "event: progress\n"
                        f"data: {json.dumps({'event': {'kind': 'assistant_text_delta', 'text': 'hello', 'seq': 0}})}\n\n"
                    ).encode()
                    self.wfile.write(frame1)
                    self.wfile.flush()
                    time.sleep(0.24)
                    return
                time.sleep(0.24)
                frame2 = (
                    "id: v1:req-sse:a:2:2\n"
                    "event: terminal\n"
                    f"data: {json.dumps({'result': {'ok': True, 'response': 'sse-done'}})}\n\n"
                ).encode()
                self.wfile.write(frame2)
                self.wfile.flush()
            except OSError:
                return

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

    sse_server = _ThreadingServer(("127.0.0.1", 0), _SseAdvanceHandler)
    sse_thread = threading.Thread(target=sse_server.serve_forever, daemon=True)
    sse_thread.start()
    try:
        agent = SimpleNamespace(
            config=SimpleNamespace(gateway_request_timeout=2.0),
            gateway_network_headers=lambda: {},
        )
        observer = NetworkGatewayObserver(agent, f"http://127.0.0.1:{sse_server.server_address[1]}")
        sse_events: list[dict] = []
        sse_timeouts: list[float] = []
        res = observer(
            GatewayChunkPollRequest(
                tmp_path / "req-sse.chunks.jsonl",
                tmp_path / "req-sse.json",
                time.time() + 0.16,
                lambda _text: None,
                [0],
                [0],
                inactivity_timeout_seconds=1.5,
                on_event=sse_events.append,
                on_wait_timeout=lambda: sse_timeouts.append(time.monotonic()),
                is_wait_cancelled=lambda: False,
            )
        )
        assert res == {"ok": True, "response": "sse-done"}
        assert len(sse_events) == 1
        assert sse_timeouts == []
        assert not observer._http_only
    finally:
        sse_server.shutdown()
        sse_server.server_close()


def test_a01_network_gateway_observer_hard_deadline_stops_when_no_wait_callback(tmp_path) -> None:
    stream_release = threading.Event()

    class _SseHoldHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path.startswith("/client/requests/") and "/events" in self.path:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                frame1 = (
                    "id: v1:req-hard:a:1:1\n"
                    "event: progress\n"
                    f"data: {json.dumps({'event': {'kind': 'assistant_text_delta', 'text': 'pulse', 'seq': 0}})}\n\n"
                ).encode()
                try:
                    self.wfile.write(frame1)
                    self.wfile.flush()
                    stream_release.wait(timeout=3.0)
                except OSError:
                    return
                return
            if self.path.startswith("/result/"):
                body = json.dumps({"status": "processing", "lease_heartbeat_at": time.time()}).encode("utf-8")
                self.send_response(202)
            else:
                body = json.dumps({"events": [], "next": 0}).encode("utf-8")
                self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

    server = _ThreadingServer(("127.0.0.1", 0), _SseHoldHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    try:
        agent = SimpleNamespace(
            config=SimpleNamespace(gateway_request_timeout=2.0),
            gateway_network_headers=lambda: {},
        )
        observer = NetworkGatewayObserver(agent, f"http://127.0.0.1:{server.server_address[1]}")
        t0 = time.monotonic()
        res = observer(
            GatewayChunkPollRequest(
                tmp_path / "req-hard.chunks.jsonl",
                tmp_path / "req-hard.json",
                time.time() + 0.22,
                lambda _text: None,
                [0],
                [0],
                inactivity_timeout_seconds=2.5,
                on_event=lambda _ev: None,
                on_wait_timeout=None,
                is_wait_cancelled=lambda: False,
            )
        )
        elapsed = time.monotonic() - t0
        assert res == {}
        assert elapsed < 0.9
    finally:
        stream_release.set()
        server.shutdown()
        server.server_close()
