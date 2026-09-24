"""批3 per-thread 协作中断钉子(长期助手 interrupt.py 蓝本)。

钉死契约:中断按线程隔离;register finally 清旗(线程复用安全);
工具循环安全点看旗收工;cancel 对线程形态按登记名递旗。
"""

from __future__ import annotations

import threading
import time
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

from agent_py_agent.agent.agent_core import _tool_loop_service as tool_loop_module
from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams
from agent_py_agent.agent.agent_core._tool_loop_service import execute_tool_loop
from agent_py_agent.agent.agent_core.tool_loop.round_execution import (
    ToolRoundExecutionRequest,
    execute_tool_round,
)
from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.concurrency.interrupt import (
    InterruptHandle,
    interrupt_by_name,
    is_interrupted,
    is_interruptible_registered,
    register_interrupt_callback,
    register_interrupt_wakeup,
    register_interruptible,
    set_interrupt,
    wait_interruptibly,
)
from agent_py_agent.agent.conversation import background_claim as claim_module
from agent_py_agent.agent.conversation.runtime import (
    BackgroundMainAgentScheduler,
    _background_claim_dependencies,
)
from agent_py_agent.agent.subagents.cancellation_hosts import signal_runner_attempt
from agent_py_agent.agent.tooling.shell import ShellTool, ShellToolOptions
from agent_py_agent.tests._tool_runtime_harness import (
    canonical_history_call,
    make_test_protocol_snapshot,
)


def test_interrupt_is_thread_scoped():
    seen: dict[str, bool] = {}
    ready = threading.Event()
    release = threading.Event()

    def worker():
        ready.set()
        release.wait(timeout=5)
        seen["worker"] = is_interrupted()

    thread = threading.Thread(target=worker)
    thread.start()
    ready.wait(timeout=5)
    set_interrupt(True)  # 只立当前(主)线程的旗
    release.set()
    thread.join(timeout=5)
    assert is_interrupted() is True, "主线程自己看得到旗"
    assert seen["worker"] is False, "别的线程不受影响"
    set_interrupt(False)


def test_register_scope_clears_flag_and_name():
    with register_interruptible("t-clean"):
        assert interrupt_by_name("t-clean") is True
        assert is_interrupted() is True
    assert is_interrupted() is False, "退出 finally 必清旗(线程复用安全)"
    assert interrupt_by_name("t-clean") is False, "名字已注销"


def test_precancelled_handle_preserves_fact_after_bind_and_ordinary_clear():
    handle = InterruptHandle()
    closed = threading.Event()
    handle.cancel()
    assert not is_interrupted()
    with register_interruptible("precancelled-handle", handle=handle):
        assert is_interrupted()
        set_interrupt(False)
        assert is_interrupted()
        wake = threading.Event()
        with register_interrupt_wakeup(wake):
            assert wake.is_set()
        with register_interrupt_callback(closed.set):
            assert closed.wait(1)
    assert not is_interrupted()
    handle._cleanup_thread.join(1)
    assert not handle.cleanup_pending


def test_cleanup_construction_failure_keeps_unknown_and_does_not_retry(monkeypatch):
    handle = InterruptHandle()
    callback = Mock()
    constructor = Mock(side_effect=RuntimeError("cannot create cleanup thread"))
    monkeypatch.setattr(threading, "Thread", constructor)
    with register_interruptible("cleanup-construction-failed", handle=handle):
        with register_interrupt_callback(callback):
            handle.cancel()
            handle.cancel()
    assert handle.cleanup_pending
    constructor.assert_called_once()
    callback.assert_not_called()


def test_handle_late_callback_does_not_wait_for_slow_close():
    handle = InterruptHandle()
    started, release = threading.Event(), threading.Event()

    def cleanup():
        started.set()
        release.wait(3)

    try:
        with register_interruptible("late-slow-hook", handle=handle):
            handle.cancel()
            before = time.monotonic()
            with register_interrupt_callback(cleanup):
                assert time.monotonic() - before < 0.2
                assert started.wait(1)
            assert handle.cleanup_pending
    finally:
        release.set()
        if handle._cleanup_thread is not None:
            handle._cleanup_thread.join(1)
    assert not handle.cleanup_pending


def test_interrupt_invokes_blocking_transport_callback_once_per_signal():
    ready = threading.Event()
    released = threading.Event()

    def worker():
        with register_interruptible("provider-stop-test"):
            with register_interrupt_callback(released.set):
                ready.set()
                released.wait(timeout=5)

    thread = threading.Thread(target=worker)
    thread.start()
    assert ready.wait(timeout=2)
    assert interrupt_by_name("provider-stop-test") is True
    thread.join(timeout=2)

    assert released.is_set()
    assert not thread.is_alive()
    assert interrupt_by_name("provider-stop-test") is False


def test_interrupt_does_not_wait_for_slow_transport_cleanup():
    ready = threading.Event()
    release_cleanup = threading.Event()
    worker_done = threading.Event()

    def worker():
        with register_interruptible("provider-slow-stop-test"):
            with register_interrupt_callback(release_cleanup.wait):
                ready.set()
                while not is_interrupted():
                    time.sleep(0.01)
            worker_done.set()

    thread = threading.Thread(target=worker)
    thread.start()
    assert ready.wait(timeout=2)

    started = time.monotonic()
    assert interrupt_by_name("provider-slow-stop-test") is True
    elapsed = time.monotonic() - started

    assert elapsed < 0.5
    assert worker_done.wait(timeout=2)
    release_cleanup.set()
    thread.join(timeout=2)
    assert not thread.is_alive()


def test_interrupt_wakes_retry_backoff_without_waiting_for_full_delay():
    ready = threading.Event()
    outcome: list[str] = []

    def worker():
        with register_interruptible("provider-backoff-stop-test"):
            ready.set()
            try:
                wait_interruptibly(30.0)
            except InterruptedError:
                outcome.append("interrupted")

    thread = threading.Thread(target=worker)
    thread.start()
    assert ready.wait(timeout=2)
    assert interrupt_by_name("provider-backoff-stop-test") is True
    thread.join(timeout=2)

    assert outcome == ["interrupted"]
    assert not thread.is_alive()


def test_same_control_name_interrupts_foreground_and_background_scopes():
    ready = threading.Barrier(3)
    release = threading.Event()
    seen: list[bool] = []

    def worker():
        with register_interruptible("conversation-request:req-shared"):
            ready.wait(timeout=3)
            release.wait(timeout=3)
            seen.append(is_interrupted())

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    ready.wait(timeout=3)

    assert interrupt_by_name("conversation-request:req-shared") is True
    release.set()
    for thread in threads:
        thread.join(timeout=3)

    assert seen == [True, True]
    assert interrupt_by_name("conversation-request:req-shared") is False


def test_background_main_run_registers_the_durable_task_control_name(monkeypatch):
    observed: dict[str, object] = {}

    class Heartbeat:
        def stop(self):
            observed["heartbeat_stopped"] = True

    class Runtime:
        agent = SimpleNamespace()

        def run_once(self, _kwargs):
            observed["signaled"] = interrupt_by_name("conversation-request:req-background")
            observed["interrupted"] = is_interrupted()
            return "done"

    class Claims:
        def acquire(self, _payload):
            observed["registered_before_claim"] = is_interruptible_registered(
                "conversation-request:req-background"
            )
            return {"claim_id": "claim-1"}

        def finish(self, payload):
            observed["finish"] = payload

    class Store:
        claims = Claims()
        wakes = SimpleNamespace(pending_one=lambda _signal_id: None)

    scheduler = BackgroundMainAgentScheduler({"runtime": Runtime(), "store": Store()})
    monkeypatch.setattr(
        claim_module, "_start_heartbeat",
        lambda _dependencies, _claim, _thread, *, claim_scope_id="": Heartbeat(),
    )
    scheduler._runtime_facts = lambda: {}

    dependencies = replace(
        _background_claim_dependencies(scheduler), child_owns_task=lambda _task: False,
        claim_scope_id=lambda _thread, _task: "lane", terminal_task=lambda _kwargs: False,
        recovery_block=lambda _task: None,
    )
    result = claim_module.run_claimed(
        dependencies,
        {"thread_id": "thread-1", "task_id": "req-background"},
    )

    assert result == "done"
    assert observed["registered_before_claim"] is True
    assert observed["signaled"] is True
    assert observed["interrupted"] is True
    assert observed["heartbeat_stopped"] is True
    assert observed["finish"]["status"] == "finished"
    assert interrupt_by_name("conversation-request:req-background") is False


def test_background_main_user_interrupt_closes_claim_without_runtime_failure(monkeypatch):
    observed: dict[str, object] = {}

    class Heartbeat:
        def stop(self):
            observed["heartbeat_stopped"] = True

    class Runtime:
        agent = SimpleNamespace()

        def run_once(self, _kwargs):
            raise InterruptedError("user stopped the current task")

    class Claims:
        def finish(self, payload):
            observed["finish"] = payload

    class Store:
        claims = Claims()
        wakes = SimpleNamespace(pending_one=lambda _signal_id: None)

    scheduler = BackgroundMainAgentScheduler({"runtime": Runtime(), "store": Store()})
    monkeypatch.setattr(
        claim_module, "_start_heartbeat",
        lambda _dependencies, _claim, _thread, *, claim_scope_id="": Heartbeat(),
    )
    scheduler._runtime_facts = lambda: {}

    result = claim_module.run_with_heartbeat(
        _background_claim_dependencies(scheduler),
        "claim-stop",
        {"thread_id": "thread-stop", "task_id": "req-stop"},
    )

    assert result is None
    assert observed["heartbeat_stopped"] is True
    assert observed["finish"]["status"] == "cancelled"
    assert observed["finish"]["error"] is None
    assert interrupt_by_name("conversation-request:req-stop") is False


def test_tool_round_stops_at_interrupt_safe_point():
    executed: list[str] = []
    records: list[tuple[str, str, str]] = []

    def execute_one(request):
        executed.append(request.call.tool_name)
        raise AssertionError("中断安全点之后不得进入执行器")

    def record_one(record):
        records.append((record.call.tool_name, record.result.output, record.result.error_code or ""))

    set_interrupt(True)
    try:
        execute_tool_round(
            ToolRoundExecutionRequest(
                agent=SimpleNamespace(),
                params=SimpleNamespace(
                    tool_context=[],
                    tool_protocol_snapshot=make_test_protocol_snapshot(),
                ),
                tool_rounds=1,
                response=ModelResponse(text="round", backend="test"),
                calls=[
                    canonical_history_call(
                        "read_file",
                        {"path": "/tmp/a.md"},
                        call_id="interrupt-a",
                        source_protocol="native",
                    ),
                    canonical_history_call(
                        "read_file",
                        {"path": "/tmp/b.md"},
                        call_id="interrupt-b",
                        source_protocol="native",
                    ),
                ],
                execute_one=execute_one,
                record_one=record_one,
            )
        )
    finally:
        set_interrupt(False)
    assert executed == [], "中断旗下不再开新工具"
    assert [item[0] for item in records] == ["read_file", "read_file"]
    assert all(item[2] == "CANCELLED" for item in records), (
        "每个已接纳但未启动的调用都必须有配对的取消结果"
    )
    assert all("已被取消" in item[1] for item in records)


def test_cancel_signals_only_exact_runner_attempt():
    with register_interruptible("subagent-runner-attempt:run-a:attempt-a"):
        assert signal_runner_attempt("run-a", "") == "not_found"
        assert signal_runner_attempt("run-a", "attempt-b") == "not_found"
        assert not is_interrupted()
        assert signal_runner_attempt("run-a", "attempt-a") == "attempt_signaled"
        assert is_interrupted() is True
    assert signal_runner_attempt("run-a", "attempt-a") == "not_found"


def test_foreground_shell_stops_when_conversation_is_interrupted(tmp_path):
    result: dict[str, object] = {}
    ready = threading.Event()

    def worker():
        tool = ShellTool(tmp_path, options=ShellToolOptions(default_timeout=30))
        with register_interruptible("shell-stop-test"):
            ready.set()
            result["value"] = tool.execute({"command": "python3 -c 'import time; time.sleep(20)'"})

    thread = threading.Thread(target=worker)
    thread.start()
    assert ready.wait(timeout=5)
    time.sleep(0.2)
    assert interrupt_by_name("shell-stop-test") is True
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert result["value"].error_code == "CANCELLED"


def test_interrupt_arriving_during_model_generation_discards_final_response(monkeypatch):
    agent = SimpleNamespace(backend=SimpleNamespace(name="test"))
    params = ToolLoopExecuteParams(
        user_prompt="long task",
        memories=[],
        runtime_injections=[],
        prompt_files=[],
        tool_catalog_section="",
        tool_recommendations_section="",
        tool_context=[],
        effective_on_chunk=None,
        allowed_tools=None,
        write_boundary=None,
        task_attributes={},
        request_id="req-stop-during-model",
        run_id="",
        task_id="",
        one_shot_tool_calls=set(),
        executed_tools=[],
        archive_tool_calls=[],
    )

    # LLM: 在主循环请求模型期间设置真实线程中断旗，返回旧响应以验证采样后的安全检查。
    # 函数用途: 模拟生成中途收到停止，让真实循环负责丢弃旧回复并给出取消状态。
    def model_turn(loop_agent, loop_params, _tool_rounds, _provider_response_repairs):
        assert loop_agent is agent and loop_params is params
        set_interrupt(True)
        return tool_loop_module._ModelTurnOutcome(
            "prompt", ModelResponse(text="stale final", backend="test"), False, False, 0, loop_params,
        )

    with monkeypatch.context() as loop_patch:
        loop_patch.setattr(tool_loop_module, "_model_turn_or_retry", model_turn)
        try:
            response = execute_tool_loop(agent, params).final_response
        finally:
            set_interrupt(False)

    assert response.text == ""
    assert response.runtime_status == "cancelled"
