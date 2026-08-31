from __future__ import annotations

import threading
from types import SimpleNamespace

from agent_py_agent.cli.chat_parts import tui_preflight
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
