from __future__ import annotations

import threading
from io import BytesIO
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from agent_py_agent.agent.concurrency.interrupt import register_interruptible, wait_interruptibly
from agent_py_agent.agent.conversation.control_commands import (
    ConversationControlCommand,
    ConversationTaskStatus,
    parse_conversation_control,
    render_conversation_task_status,
    session_task_interrupt_name,
)
from agent_py_agent.agent.gateway_parts.io import read_json_file
from agent_py_agent.agent.gateway_parts.queue_service import claim_request
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
from agent_py_agent.cli.chat_client_context import GatewayChatClientAgent, _http_error_json
from agent_py_agent.cli.chat_parts import (
    control_runtime,
    gateway_client,
    tui_actions,
    tui_threading,
)
from agent_py_agent.tests.test_conveyor_m21_turn_stop import _dispatch, _queued, _thread
from agent_py_agent.tests.test_conveyor_m21_turn_stop import gateway as gateway


def _execution(post):
    agent = SimpleNamespace(config=SimpleNamespace(gateway_port=18123), post_gateway_json=post)
    return control_runtime.ChatControlExecution(
        agent, True, control_runtime.ChatControlState(False, 0, "", 0.0, "m23-session")
    )


@pytest.mark.parametrize(("operation", "target", "scope"), [
    ("", "", "thread"), ("session", "", "session"), ("thread", "thread-old", "thread"),
])
def test_stop_retries_busy_response_with_same_identity(monkeypatch, operation, target, scope):
    calls = []
    sleeps = []
    monkeypatch.setattr(gateway_client.time, "sleep", sleeps.append)

    def post(path, body, *, timeout):
        calls.append((path, dict(body)))
        if len(calls) == 1:
            return 503, {"error": "busy"}
        return 200, {"ok": True, "kind": "stop", "control_state": "completed"}

    command = ConversationControlCommand("stop", operation=operation, target_id=target)
    result = control_runtime.execute_chat_control(_execution(post), command, message_id="m23-retry")
    assert calls[0][0] == "/client/turn-stop"
    assert len(calls) == 2 and calls[0] == calls[1]
    assert calls[0][1]["message_id"] == "m23-retry"
    assert calls[0][1]["scope"] == scope
    assert calls[0][1].get("thread_id", "") == target
    assert sleeps == [0.2] and result.ok


def test_status_503_is_silent_if_the_next_response_succeeds(monkeypatch):
    calls = []
    monkeypatch.setattr(gateway_client.time, "sleep", lambda _: None)

    def post(path, body, *, timeout):
        calls.append(path)
        if len(calls) == 1:
            return 503, {}
        return 200, {"ok": True, "message": "status loaded"}

    result = control_runtime.execute_chat_control(_execution(post), parse_conversation_control("/status"))
    assert len(calls) == 2 and result.ok
    assert result.message == "status loaded"


def test_status_warns_after_third_busy_response_and_clears_on_recovery(monkeypatch):
    calls, waits, notices = [], [], []
    monkeypatch.setattr(gateway_client.time, "sleep", waits.append)

    def post(path, body, *, timeout):
        calls.append(path)
        if len(calls) <= 3:
            return 503, {}
        return 200, {"ok": True, "message": "status loaded"}

    base = _execution(post)
    execution = control_runtime.ChatControlExecution(
        base.agent, True, base.state, on_status_sync=lambda ok: notices.append((len(calls), ok)),
    )
    result = control_runtime.execute_chat_control(execution, parse_conversation_control("/status"))
    assert result.ok and notices == [(3, False), (4, True)]
    assert waits == [0.2, 0.4, 0.8]


def test_notice_banner_waits_for_three_busy_responses():
    debounce = tui_threading._NoticeWarnDebounce()
    failures = [debounce.should_publish(False, debounce.last_success_at + 10, http_status=503) for _ in range(3)]
    assert failures == [False, False, True]
    assert debounce.should_publish(True, debounce.last_success_at + 10)


def test_status_text_renders_background_root_and_stop_hint():
    status = ConversationTaskStatus(background_roots=[{
        "thread_id": "thread-earlier", "title": "Earlier task", "kind": "dispatch", "started_at": 1764000000.0,
    }])
    text = render_conversation_task_status(status)
    assert all(value in text for value in ("thread-earlier", "Earlier task", "dispatch", "2025", "/stop all", "/stop thread"))


def _real_client(gateway):
    agent, _, server = gateway
    return GatewayChatClientAgent(
        None, SimpleNamespace(gateway_port=server.server.server_address[1]),
        agent.root, [agent.root], agent.home_paths, owner_identity=OwnerIdentity.provider_user("feishu", "u-1"),
    )


def _control(client, raw, message_id=""):
    command = parse_conversation_control(raw, allow_scoped_stop=True)
    assert command is not None and command.valid
    state = control_runtime.ChatControlState(False, 0, "", 0.0, "c-1")
    return control_runtime.execute_chat_control(control_runtime.ChatControlExecution(client, True, state),
                                                command, message_id=message_id)


@pytest.mark.parametrize("reported_status", [0, 503])
def test_real_lost_stop_response_replays_receipt_after_new_and_rejects_changed_target(gateway, monkeypatch, reported_status):
    client = _real_client(gateway)
    earlier = _thread(gateway)
    queued = _queued(gateway, earlier, "m23-queued-before-new")
    post = client.post_gateway_json
    attempts = []

    def lose_response(path, payload, *, timeout):
        status, result = post(path, payload, timeout=timeout)
        if path == "/client/turn-stop":
            attempts.append((dict(payload), status, result))
            if len(attempts) == 1:
                return reported_status, {}
        return status, result

    monkeypatch.setattr(client, "post_gateway_json", lose_response)
    first = _control(client, "/stop", "m23-same-target")
    assert first.ok and first.details["queued_cancelled"] == 1
    assert len(attempts) == 2 and attempts[0] == attempts[1]
    assert attempts[0][0]["message_id"] == "m23-same-target"
    paths = gateway[1]
    assert claim_request(paths, queued) is None
    assert read_json_file(paths.terminal / queued.name)["terminal_response"]["status"] == "cancelled"
    assert not (paths.processing / queued.name).exists()

    reset = _control(client, "/new", "m23-new")
    assert reset.ok and reset.details["view_reset"]
    current = _thread(gateway)
    assert current.thread_id != earlier.thread_id
    later = _queued(gateway, current, "m23-queued-after-new")
    replay = _control(client, "/stop", "m23-same-target")
    assert replay.operation_id == first.operation_id and replay.details == first.details
    conflict = _control(client, f"/stop thread {current.thread_id}", "m23-same-target")
    assert attempts[-1][1] == 409
    assert conflict.control_state == "conflict" and conflict.error_code == "IDEMPOTENCY_CONFLICT"
    assert conflict.message and not read_json_file(later).get("cancel_requested")
    previous_queued = _queued(gateway, earlier, "m23-new-work-on-previous-thread")
    targeted = _control(client, f"/stop thread {earlier.thread_id}")
    assert targeted.ok and targeted.details["target_thread_ids"] == [earlier.thread_id]
    assert claim_request(paths, previous_queued) is None
    assert not read_json_file(later).get("cancel_requested")


def test_real_background_thread_survives_plain_stop_and_stops_with_session_scope(gateway):
    client = _real_client(gateway)
    earlier = _thread(gateway)
    turn = _dispatch(gateway, earlier)
    ready, stopped, release = threading.Event(), threading.Event(), threading.Event()

    def run():
        with register_interruptible(session_task_interrupt_name(turn)):
            ready.set()
            try:
                while not release.is_set():
                    wait_interruptibly(0.02)
            except InterruptedError:
                stopped.set()

    thread = threading.Thread(target=run)
    thread.start()
    try:
        assert ready.wait(5)
        reset = _control(client, "/new")
        assert reset.ok
        assert sum("旧会话仍有 1 个后台根" in line for line in reset.message.splitlines()) == 1
        current = gateway[0].conversation_store.threads.require(reset.details["thread_id"])
        notices = [row for row in current.pending_host_notices if row["code"] == "BACKGROUND_ROOTS_CONTINUE"]
        assert len(notices) == 1
        assert _control(client, "/stop").details["target_thread_ids"] == [current.thread_id]
        assert not stopped.is_set()
        status = _control(client, "/status")
        assert status.ok and earlier.thread_id in status.message and "/stop all" in status.message
        assert status.status.background_roots[0]["thread_id"] == earlier.thread_id
        stop = _control(client, "/stop all")
        assert stop.ok and stop.details["stop_scope"] == "session"
        assert stop.details["roots_stopped"] == 1 and stopped.wait(5)
        assert not gateway[1].stop_request.exists()
    finally:
        release.set()
        thread.join(5)


@pytest.mark.parametrize(("raw", "routes"), [
    ("/stop", ["/client/turn-stop", "/control"]),
    ("/stop all", ["/client/turn-stop"]),
    ("/stop thread thread-previous", ["/client/turn-stop"]),
])
def test_old_gateway_reports_supported_scope_without_retargeting(raw, routes):
    calls = []

    def post(path, body, *, timeout):
        calls.append(path)
        if path == "/client/turn-stop":
            return 404, {"error": "not found"}
        return 200, {"ok": True, "message": "stopped"}

    result = _control(_execution(post).agent, raw)
    assert calls == routes and "当前线程" in result.message
    assert result.ok == (raw == "/stop")


def test_stop_retry_budget_is_bounded_and_unconfirmed_result_is_terminal(monkeypatch):
    clock = [0.0]
    waits = []
    attempts = []

    def sleep(seconds):
        waits.append(seconds)
        clock[0] += seconds

    time = SimpleNamespace(monotonic=lambda: clock[0], sleep=sleep)
    monkeypatch.setattr(gateway_client, "time", time)
    monkeypatch.setattr(control_runtime, "time", time)

    def post(path, payload, *, timeout):
        attempts.append((path, payload, timeout))
        clock[0] += min(3.0, timeout)
        return 503, {"error": "busy"}

    result = _control(_execution(post).agent, "/stop")
    assert result.control_state == "terminal_unknown" and result.delivery_status == "unknown"
    assert clock[0] <= 10 and [p for p, _, _ in attempts] == ["/client/turn-stop"] * 3
    assert waits == [0.2, 0.4]
    assert all(row[1] == attempts[0][1] for row in attempts)


def test_scoped_stops_are_only_parsed_as_explicit_opt_in_commands():
    assert parse_conversation_control("请解释 /stop all", allow_scoped_stop=True) is None
    assert not parse_conversation_control("/stop all").valid
    assert not parse_conversation_control("/stop thread", allow_scoped_stop=True).valid
    assert parse_conversation_control("/stop thread thread-old", allow_scoped_stop=True).target_id == "thread-old"


def test_stop_retries_missing_status_with_frozen_execution_snapshot(monkeypatch):
    from copy import deepcopy

    expected = {"thread_id": "thread-current", "turn_id": "turn-current", "runtime_authority": {"run_id": "run-original"}}
    sent = []
    monkeypatch.setattr(gateway_client.time, "sleep", lambda _: None)

    def post(path, payload, *, timeout):
        sent.append((path, deepcopy(payload)))
        if len(sent) == 1:
            expected["runtime_authority"]["run_id"] = "run-later"
            return 0, {}
        return 200, {"ok": True, "kind": "stop", "control_state": "completed"}

    execution = control_runtime.ChatControlExecution(
        _execution(post).agent, True,
        control_runtime.ChatControlState(True, 0, "", 0.0, "session", "turn-current", expected_execution=expected),
    )
    result = control_runtime.execute_chat_control(execution, parse_conversation_control("/stop"))
    assert result.ok and len(sent) == 2 and sent[0] == sent[1]
    assert sent[0][1]["expected_execution"]["runtime_authority"] == {"run_id": "run-original"}


def test_credential_denial_404_preserves_reason_and_does_not_use_legacy_stop():
    calls = []

    def post(path, payload, *, timeout):
        calls.append(path)
        return 404, {"error_code": "LOCAL_CREDENTIAL_REQUIRED", "error": "credentials required"}

    result = _control(_execution(post).agent, "/stop")
    assert calls == ["/client/turn-stop"]
    assert result.error_code == "LOCAL_CREDENTIAL_REQUIRED" and result.control_state == "rejected"


def test_gateway_stop_key_uses_same_slash_dispatcher(monkeypatch):
    from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime

    commands = []
    monkeypatch.setattr(tui_actions, "_tui_submit_control_operation", lambda _, text: commands.append(text))
    params = SimpleNamespace(use_gateway=True, tui_runtime=TuiRuntime("m23-stop-key"))
    event = SimpleNamespace(app=SimpleNamespace(invalidate=lambda: None))
    tui_actions._dispatch_active_interrupt(event, params)
    assert commands == ["/stop"]


@pytest.mark.parametrize("decode", [control_runtime._http_error_body, _http_error_json])
def test_busy_http_error_response_is_closed(decode):
    response = BytesIO(b'{"error": "busy"}')
    decode(HTTPError("http://127.0.0.1:18123", 503, "busy", None, response))
    assert response.closed


def test_notices_poll_retries_busy_responses_before_showing_one_banner(monkeypatch):
    from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime

    client = GatewayChatClientAgent(None, SimpleNamespace(gateway_port=18123), None, [], None)
    codes = iter((503, 200, 503, 503, 503, 503))
    calls, waits, published = [], [], []

    def post(path, body, *, timeout):
        calls.append((path, body))
        code = next(codes)
        valid = {
            "ok": True, "binding_state": "empty", "thread_id": "", "conversation_id": "notice-session",
            "notices": [], "cursor": 0, "transcript_events": [], "event_cursor": 0, "event_stream_id": "",
        }
        return code, valid if code == 200 else {}

    monkeypatch.setattr(client, "post_gateway_json", post)
    runtime = TuiRuntime("notice-session")
    monkeypatch.setattr(runtime, "publish_background_sync_status", lambda *, ok: published.append(ok))
    monkeypatch.setattr(tui_threading, "_apply_gateway_background_snapshot", lambda *args: args[-2].get("ok") is True)

    def wait(seconds):
        waits.append(seconds)
        return len(waits) == 6

    stop = SimpleNamespace(is_set=lambda: False, wait=wait)
    tui_threading._run_background_notice_loop(stop, client, "notice-session", runtime, [None])
    assert published == [True, False]
    assert waits[0] == 0.2 and waits[2:] == [0.2, 0.4, 0.8, 1.6]
    assert len(calls) == 6 and all(call == calls[0] for call in calls)


def test_gateway_readiness_does_not_publish_transient_503(monkeypatch):
    from agent_py_agent.cli.chat_parts.gateway_client import NetworkGatewayObserver

    observer = NetworkGatewayObserver(SimpleNamespace(), "http://127.0.0.1:18123")
    results = iter(((503, {}), (200, {"status": "running", "pid": 42})))
    waits, states = [], []
    monkeypatch.setattr(observer, "request_json", lambda *a, **k: next(results))
    monkeypatch.setattr(gateway_client.time, "sleep", waits.append)
    result = observer.wait_for_readiness(None, timeout=2.0, on_observation=lambda state: states.append(state.state))
    assert result.ready and waits == [0.2] and states == ["ready"]
