from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from agent_py_agent.cli.chat_parts import tui_preflight
from agent_py_agent.cli.chat_parts.tui import _prepare_gateway_session
from agent_py_agent.cli.chat_parts.tui_preflight import (
    TuiGatewayPreflight,
    start_tui_gateway_preflight,
)
from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime


class _ImmediateLoop:
    def call_soon_threadsafe(self, callback) -> None:
        callback()

    def is_closed(self) -> bool:
        return False


class _DeferredLoop:
    def __init__(self) -> None:
        self.callbacks: list[object] = []

    def call_soon_threadsafe(self, callback) -> None:
        self.callbacks.append(callback)

    def is_closed(self) -> bool:
        return False

    def drain(self) -> None:
        callbacks, self.callbacks = self.callbacks, []
        for callback in callbacks:
            callback()


class _FakeFuture:
    def __init__(self) -> None:
        self.completed = False

    def done(self) -> bool:
        return self.completed


def _application() -> SimpleNamespace:
    future = _FakeFuture()

    def exit_app(*, result) -> None:
        future.completed = True
        application.result = result

    application = SimpleNamespace(
        loop=_ImmediateLoop(),
        future=future,
        result=None,
        invalidations=0,
        invalidate=lambda: setattr(
            application,
            "invalidations",
            application.invalidations + 1,
        ),
        exit=exit_app,
    )
    return application


def test_gateway_preflight_starts_worker_only_after_real_ready(monkeypatch) -> None:
    application = _application()
    runtime = TuiRuntime("preflight-ready")
    starts: list[str] = []
    monkeypatch.setattr(
        tui_preflight,
        "wait_for_gateway_running",
        lambda _paths, timeout: ({"status": "running"}, timeout == 3.0),
    )

    thread = start_tui_gateway_preflight(
        TuiGatewayPreflight(
            application=application,
            runtime=runtime,
            paths=object(),
            timeout_seconds=3.0,
            stop_event=threading.Event(),
            on_ready=lambda: starts.append("worker"),
        )
    )
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert starts == ["worker"]
    assert application.result is None
    assert runtime.store.snapshot().active_blocks == ()


def test_gateway_preflight_failure_exits_with_typed_error(monkeypatch) -> None:
    application = _application()
    runtime = TuiRuntime("preflight-failed")
    stop_event = threading.Event()
    monkeypatch.setattr(
        tui_preflight,
        "wait_for_gateway_running",
        lambda _paths, timeout: ({"status": "stopped"}, False),
    )

    thread = start_tui_gateway_preflight(
        TuiGatewayPreflight(
            application=application,
            runtime=runtime,
            paths=object(),
            timeout_seconds=1.0,
            stop_event=stop_event,
            on_ready=lambda: None,
        )
    )
    thread.join(timeout=2)

    snapshot = runtime.store.snapshot()
    assert stop_event.is_set()
    assert application.result == 2
    assert snapshot.active_blocks == ()
    assert snapshot.stable_blocks[0].metadata["error_code"] == "GATEWAY_NOT_READY"


def test_gateway_preflight_commits_fast_result_on_application_loop(monkeypatch) -> None:
    application = _application()
    deferred_loop = _DeferredLoop()
    application.loop = deferred_loop
    runtime = TuiRuntime("preflight-fast-result")
    starts: list[str] = []
    monkeypatch.setattr(
        tui_preflight,
        "wait_for_gateway_running",
        lambda _paths, timeout: ({"status": "running"}, timeout == 0.1),
    )

    thread = start_tui_gateway_preflight(
        TuiGatewayPreflight(
            application=application,
            runtime=runtime,
            paths=object(),
            timeout_seconds=0.1,
            stop_event=threading.Event(),
            on_ready=lambda: starts.append("worker"),
        )
    )
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert starts == []
    assert application.invalidations == 0
    assert runtime.store.snapshot().active_blocks[0].kind == "connection_started"

    deferred_loop.drain()

    assert starts == ["worker"]
    assert application.invalidations == 1
    assert runtime.store.snapshot().active_blocks == ()


def test_gateway_preflight_prepares_session_after_ready_before_worker(monkeypatch) -> None:
    application = _application()
    application.loop = _DeferredLoop()
    runtime = TuiRuntime("preflight-resume")
    order: list[str] = []

    def ready(_paths, *, timeout):
        order.append("ready")
        return {}, True

    def prepare():
        assert threading.current_thread() is not threading.main_thread()
        order.append("history")
        return ""

    monkeypatch.setattr(tui_preflight, "wait_for_gateway_running", ready)
    thread = start_tui_gateway_preflight(TuiGatewayPreflight(
        application=application, runtime=runtime, paths=object(), timeout_seconds=3,
        stop_event=threading.Event(), on_ready=lambda: order.append("worker"),
        prepare_session=prepare,
    ))
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert order == ["ready", "history"]
    application.loop.drain()
    assert order == ["ready", "history", "worker"]


@pytest.mark.parametrize("alive,error", [(False, "GATEWAY_NOT_READY"), (True, "GATEWAY_HISTORY_UNAVAILABLE")])
def test_gateway_preflight_history_failure_never_starts_worker(monkeypatch, alive, error) -> None:
    application = _application()
    runtime = TuiRuntime("preflight-history-error")
    starts: list[str] = []
    monkeypatch.setattr(tui_preflight, "wait_for_gateway_running", lambda *_args, **_kwargs: ({}, alive))
    thread = start_tui_gateway_preflight(TuiGatewayPreflight(
        application=application, runtime=runtime, paths=object(), timeout_seconds=3,
        stop_event=threading.Event(), on_ready=lambda: starts.append("worker"),
        prepare_session=lambda: starts.append("history") or "GATEWAY_HISTORY_UNAVAILABLE",
    ))
    thread.join(timeout=2)
    assert starts == (["history"] if alive else [])
    assert application.result == 2
    assert runtime.store.snapshot().stable_blocks[0].metadata["error_code"] == error


def test_gateway_preflight_late_result_cannot_restart_closed_tui(monkeypatch) -> None:
    application = _application()
    application.loop = _DeferredLoop()
    runtime = TuiRuntime("preflight-closed")
    stop = threading.Event()
    starts: list[str] = []
    monkeypatch.setattr(tui_preflight, "wait_for_gateway_running", lambda *_args, **_kwargs: ({}, True))
    thread = start_tui_gateway_preflight(TuiGatewayPreflight(
        application=application, runtime=runtime, paths=object(), timeout_seconds=3,
        stop_event=stop, on_ready=lambda: starts.append("worker"),
        prepare_session=lambda: "",
    ))
    thread.join(timeout=2)
    snapshot = runtime.store.snapshot()
    stop.set()
    application.exit(result=0)
    application.loop.drain()
    assert starts == []
    assert application.result == 0
    assert application.invalidations == 0
    assert runtime.store.snapshot() == snapshot


@pytest.mark.parametrize("outcome", ["ready", "failed", "closed", "raised"])
def test_resume_preparation_publishes_only_successful_live_session(outcome) -> None:
    stop = threading.Event()
    runtime = TuiRuntime("exact-session")
    reads: list[tuple[str, int]] = []

    def request_history(session_id, *, max_turns):
        reads.append((session_id, max_turns))
        if outcome == "closed":
            stop.set()
        if outcome == "raised":
            raise OSError("private path must not escape")
        return {
            "ok": outcome != "failed", "thread_id": "exact-thread",
            "turns": [{"user_message": "之前的问题", "assistant_message": "之前的答复"}],
        }

    params = SimpleNamespace(
        agent=SimpleNamespace(
            gateway_client_only=True,
            config=SimpleNamespace(chat_history_max_turns=20),
            request_chat_history=request_history,
        ),
        current_session_id="exact-session", history_lock=threading.Lock(),
        conversation_history=[],
    )
    error = _prepare_gateway_session(params, runtime, stop)
    assert reads == [("exact-session", 20)]
    assert error == ("GATEWAY_HISTORY_UNAVAILABLE" if outcome in {"failed", "raised"} else "")
    assert params.conversation_history == ([("之前的问题", "之前的答复")] if outcome == "ready" else [])
    snapshot = runtime.store.snapshot()
    assert len(snapshot.stable_blocks) == (2 if outcome == "ready" else 0)
    assert snapshot.active_blocks == ()
