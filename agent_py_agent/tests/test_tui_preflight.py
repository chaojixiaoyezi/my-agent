from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.gateway_parts.daemon_metadata import _get_process_start_time
from agent_py_agent.agent.gateway_parts.paths import GatewayPaths
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


# LLM: 受控验收要求等待结局可控，这里伪造唯一权威的返回值；真实文件驱动的用例不 patch 任何判据。
# 函数用途: 构造一个与 GatewayReadinessWait 字段兼容的等待结局替身。
def _outcome(*, ready: bool, reason: str, state: str = "", pid: int | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        ready=ready,
        reason=reason,
        state=state or ("ready" if ready else "stopped"),
        pid=pid,
        last=None,
    )


def _patch_readiness(monkeypatch, outcome: SimpleNamespace) -> None:
    monkeypatch.setattr(
        tui_preflight,
        "wait_for_gateway_readiness",
        lambda _paths, *, timeout, on_observation=None: outcome,
    )


# LLM: 真实 GatewayPaths 合同 + 真实 tmp 状态文件；不启动真 Gateway、不碰用户状态目录。
# 函数用途: 构造测试专用 Gateway 路径合同。
def _gateway_paths(tmp_path: Path) -> GatewayPaths:
    return GatewayPaths(
        root=tmp_path / "gateway",
        pid=tmp_path / "gateway" / "gateway.pid",
        adapter_pid=tmp_path / "gateway" / "adapter.pid",
        state=tmp_path / "gateway" / "state.json",
        heartbeat=tmp_path / "gateway" / "heartbeat.json",
        stop_request=tmp_path / "gateway" / "stop.request",
        log=tmp_path / "gateway" / "gateway.log",
        inbox=tmp_path / "gateway" / "requests" / "pending",
        processing=tmp_path / "gateway" / "requests" / "processing",
        done=tmp_path / "gateway" / "requests" / "done",
        failed=tmp_path / "gateway" / "requests" / "failed",
        responses=tmp_path / "gateway" / "responses",
        history=tmp_path / "gateway" / "gateway_requests.jsonl",
    )


# LLM: PID 记录必须带真实进程出生时间，否则 get_running_pid_report 会按 PID 复用把它当陈旧清掉。
# 函数用途: 为指定进程写一份可核验的 PID 记录，updated_at 充当本次代际锚点。
def _write_pid_record(paths: GatewayPaths, pid: int, *, anchor_at: float | None = None) -> None:
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.pid.write_text(
        json.dumps(
            {
                "pid": int(pid),
                "kind": "my-agent-gateway",
                "start_time": _get_process_start_time(int(pid)),
                "updated_at": datetime.fromtimestamp(
                    anchor_at if anchor_at is not None else time.time(),
                    timezone.utc,
                ).isoformat(),
            }
        ),
        encoding="utf-8",
    )


# LLM: state/heartbeat 是判据唯一读取的结构化来源；测试只写这两个真实文件，不注入任何替身。
# 函数用途: 写一份 Gateway 状态记录（pid/status/started_at）。
def _write_status(
    paths: GatewayPaths,
    *,
    pid: int,
    status: str,
    started_at: float | None = None,
    path: Path | None = None,
) -> None:
    paths.root.mkdir(parents=True, exist_ok=True)
    target = path or paths.state
    target.write_text(
        json.dumps(
            {
                "pid": int(pid),
                "status": status,
                "started_at": time.time() if started_at is None else started_at,
            }
        ),
        encoding="utf-8",
    )


# LLM: 采样 footer notice，证明"启动中"确实对用户可见；采样线程只读 runtime 既有接口。
# 函数用途: 起一个后台采样线程收集 notice 文本，返回 (stop_event, texts)。
def _sample_notices(runtime: TuiRuntime) -> tuple[threading.Event, list[str]]:
    stop_event = threading.Event()
    texts: list[str] = []

    def sample() -> None:
        while not stop_event.is_set():
            text = runtime.notice()
            if text:
                texts.append(text)
            time.sleep(0.02)

    threading.Thread(target=sample, daemon=True).start()
    return stop_event, texts


def test_gateway_preflight_starts_worker_only_after_real_ready(monkeypatch) -> None:
    application = _application()
    runtime = TuiRuntime("preflight-ready")
    starts: list[str] = []
    _patch_readiness(monkeypatch, _outcome(ready=True, reason="GATEWAY_READY", pid=4242))

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
    _patch_readiness(monkeypatch, _outcome(ready=False, reason="GATEWAY_NOT_READY"))

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
    _patch_readiness(monkeypatch, _outcome(ready=True, reason="GATEWAY_READY", pid=4242))

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

    def prepare():
        assert threading.current_thread() is not threading.main_thread()
        order.append("history")
        return ""

    _patch_readiness(monkeypatch, _outcome(ready=True, reason="GATEWAY_READY", pid=4242))
    thread = start_tui_gateway_preflight(TuiGatewayPreflight(
        application=application, runtime=runtime, paths=object(), timeout_seconds=3,
        stop_event=threading.Event(), on_ready=lambda: order.append("worker"),
        prepare_session=prepare,
    ))
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert order == ["history"]
    application.loop.drain()
    assert order == ["history", "worker"]


@pytest.mark.parametrize("ready,error", [(False, "GATEWAY_NOT_READY"), (True, "GATEWAY_HISTORY_UNAVAILABLE")])
def test_gateway_preflight_history_failure_never_starts_worker(monkeypatch, ready, error) -> None:
    application = _application()
    runtime = TuiRuntime("preflight-history-error")
    starts: list[str] = []
    _patch_readiness(
        monkeypatch,
        _outcome(ready=ready, reason="GATEWAY_NOT_READY" if not ready else "GATEWAY_READY"),
    )
    thread = start_tui_gateway_preflight(TuiGatewayPreflight(
        application=application, runtime=runtime, paths=object(), timeout_seconds=3,
        stop_event=threading.Event(), on_ready=lambda: starts.append("worker"),
        prepare_session=lambda: starts.append("history") or "GATEWAY_HISTORY_UNAVAILABLE",
    ))
    thread.join(timeout=2)
    assert starts == (["history"] if ready else [])
    assert application.result == 2
    assert runtime.store.snapshot().stable_blocks[0].metadata["error_code"] == error


def test_gateway_preflight_late_result_cannot_restart_closed_tui(monkeypatch) -> None:
    application = _application()
    application.loop = _DeferredLoop()
    runtime = TuiRuntime("preflight-closed")
    stop = threading.Event()
    starts: list[str] = []
    _patch_readiness(monkeypatch, _outcome(ready=True, reason="GATEWAY_READY", pid=4242))
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


# LLM: 竞态回归核心用例：进程已活但 state 仍 starting 时，preflight 必须继续等待并显示"启动中"，
#   绝不能在 HTTP 未就绪前 prepare_session。判据不 patch，走真实 tmp 状态文件。
# 函数用途: 验证 starting → ready 的时序里历史准备只发生在真就绪之后。
def test_preflight_waits_through_starting_and_prepares_only_when_ready(tmp_path: Path) -> None:
    paths = _gateway_paths(tmp_path)
    pid = os.getpid()
    _write_pid_record(paths, pid)
    _write_status(paths, pid=pid, status="starting")

    application = _application()
    runtime = TuiRuntime("preflight-starting")
    prepared_at_state: list[str] = []
    workers: list[str] = []
    stop_sampler, notices = _sample_notices(runtime)

    def prepare() -> str:
        # 只有真 ready 才允许读历史：此刻 canonical state 必须已经是 running。
        prepared_at_state.append(json.loads(paths.state.read_text(encoding="utf-8"))["status"])
        return ""

    def flip_to_ready() -> None:
        time.sleep(0.4)
        _write_status(paths, pid=pid, status="running")

    flipper = threading.Thread(target=flip_to_ready, daemon=True)
    flipper.start()
    try:
        thread = start_tui_gateway_preflight(TuiGatewayPreflight(
            application=application, runtime=runtime, paths=paths, timeout_seconds=3.0,
            stop_event=threading.Event(), on_ready=lambda: workers.append("worker"),
            prepare_session=prepare,
        ))
        thread.join(timeout=6)
        flipper.join(timeout=2)
    finally:
        stop_sampler.set()

    assert not thread.is_alive()
    assert prepared_at_state == ["running"]
    assert workers == ["worker"]
    assert application.result is None
    assert any("启动中" in text for text in notices)


# LLM: 真失败分型之一：进程消退必须立刻以退出码 2 结束，而不是等到超时才报"未就绪"。
# 函数用途: 验证启动中的 Gateway 进程死亡时 preflight 不再 prepare_session 并以 GATEWAY_PROCESS_EXITED 退出。
def test_preflight_exits_two_when_gateway_process_dies_while_starting(tmp_path: Path) -> None:
    paths = _gateway_paths(tmp_path)
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    prepared: list[str] = []
    runtime = TuiRuntime("preflight-died")
    application = _application()
    thread: threading.Thread | None = None
    try:
        _write_pid_record(paths, child.pid)
        _write_status(paths, pid=child.pid, status="starting")

        def kill_child() -> None:
            time.sleep(0.4)
            child.kill()
            child.wait(timeout=5)

        threading.Thread(target=kill_child, daemon=True).start()
        thread = start_tui_gateway_preflight(TuiGatewayPreflight(
            application=application, runtime=runtime, paths=paths,
            timeout_seconds=8.0, stop_event=threading.Event(), on_ready=lambda: None,
            prepare_session=lambda: prepared.append("history") or "",
        ))
        thread.join(timeout=10)
    finally:
        child.kill()
        child.wait(timeout=5)

    assert thread is not None and not thread.is_alive()
    assert prepared == []
    assert application.result == 2
    assert runtime.store.snapshot().stable_blocks[0].metadata["error_code"] == "GATEWAY_PROCESS_EXITED"


# LLM: 真失败分型之二：超时但仍在启动 ≠ 进程已死，必须给出不同结构化错误码，且同样不 prepare_session。
# 函数用途: 验证"还在启动"的超时被呈现为 GATEWAY_START_TIMEOUT。
def test_preflight_timeout_while_still_starting_is_typed(tmp_path: Path) -> None:
    paths = _gateway_paths(tmp_path)
    pid = os.getpid()
    _write_pid_record(paths, pid)
    _write_status(paths, pid=pid, status="starting")

    application = _application()
    runtime = TuiRuntime("preflight-starting-timeout")
    prepared: list[str] = []
    thread = start_tui_gateway_preflight(TuiGatewayPreflight(
        application=application, runtime=runtime, paths=paths, timeout_seconds=0.4,
        stop_event=threading.Event(), on_ready=lambda: None,
        prepare_session=lambda: prepared.append("history") or "",
    ))
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert prepared == []
    assert application.result == 2
    # 结构化分型：超时且仍处于 starting，与"从未启动"和"进程已死"都不同。
    assert runtime.store.snapshot().stable_blocks[0].metadata["error_code"] == "GATEWAY_START_TIMEOUT"


# LLM: 真失败分型之三：完全没在运行时保留既有 GATEWAY_NOT_READY 呈现，不与超时混为一谈。
# 函数用途: 验证无 PID 记录时 preflight 报 GATEWAY_NOT_READY。
def test_preflight_not_running_reports_not_ready(tmp_path: Path) -> None:
    paths = _gateway_paths(tmp_path)
    application = _application()
    runtime = TuiRuntime("preflight-not-running")
    thread = start_tui_gateway_preflight(TuiGatewayPreflight(
        application=application, runtime=runtime, paths=paths, timeout_seconds=0.3,
        stop_event=threading.Event(), on_ready=lambda: None,
    ))
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert application.result == 2
    assert runtime.store.snapshot().stable_blocks[0].metadata["error_code"] == "GATEWAY_NOT_READY"


# LLM: 代际语义回归：同一 PID 的 running 记录若 started_at 早于本次启动（PID 记录锚点），不算就绪。
# 函数用途: 验证陈旧 running 记录不会让 TUI 提前 prepare_session。
def test_preflight_ignores_stale_running_state_from_earlier_generation(tmp_path: Path) -> None:
    from agent_py_agent.agent.gateway_parts.status_rendering import _gateway_ready_for_pid

    paths = _gateway_paths(tmp_path)
    pid = os.getpid()
    anchor = time.time()
    _write_pid_record(paths, pid, anchor_at=anchor)
    _write_status(paths, pid=pid, status="running", started_at=anchor - 600)

    outcome = tui_preflight.wait_for_gateway_readiness(paths, timeout=0.3)

    assert outcome.ready is False
    assert outcome.reason == "GATEWAY_START_TIMEOUT"
    assert outcome.last.state == "starting"
    # 只按记录层看它确实是 running（说明拒绝它的正是代际判据，而不是别的条件）。
    assert _gateway_ready_for_pid(paths, pid) is True
    assert outcome.pid == pid


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
        conversation_history=[], restore_session_history=True,
    )
    error = _prepare_gateway_session(params, runtime, stop)
    assert reads == [("exact-session", 20)]
    assert error == ("GATEWAY_HISTORY_UNAVAILABLE" if outcome in {"failed", "raised"} else "")
    assert params.conversation_history == ([("之前的问题", "之前的答复")] if outcome == "ready" else [])
    snapshot = runtime.store.snapshot()
    assert len(snapshot.stable_blocks) == (2 if outcome == "ready" else 0)
    assert snapshot.active_blocks == ()
