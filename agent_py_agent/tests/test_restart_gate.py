from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.concurrency import restart_gate
from agent_py_agent.agent.concurrency.interrupt import set_interrupt
from agent_py_agent.agent.tooling import tool_operation_coordinator as coordinator


@pytest.fixture(autouse=True)
def _open_gate():
    restart_gate.open_tool_gate()
    yield
    restart_gate.open_tool_gate()


def test_open_gate_admits_and_counts_executing_tools():
    with restart_gate.tool_execution_admission() as admitted:
        assert admitted is True
        assert restart_gate.executing_tool_count() == 1
    assert restart_gate.executing_tool_count() == 0


def test_closed_gate_parks_tool_until_reopened():
    restart_gate.close_tool_gate()
    admitted_at: list[float] = []

    def worker():
        with restart_gate.tool_execution_admission() as admitted:
            assert admitted is True
            admitted_at.append(time.monotonic())

    thread = threading.Thread(target=worker)
    thread.start()
    time.sleep(0.3)
    assert admitted_at == [], "关口关闭时工具必须停在领取之前"
    reopened = time.monotonic()
    restart_gate.open_tool_gate()
    thread.join(timeout=5)
    assert admitted_at and admitted_at[0] >= reopened


def test_interrupt_while_parked_gives_up_admission():
    restart_gate.close_tool_gate()
    outcome: list[bool] = []

    def worker():
        with restart_gate.tool_execution_admission() as admitted:
            outcome.append(admitted)

    thread = threading.Thread(target=worker)
    thread.start()
    time.sleep(0.2)
    set_interrupt(True, thread_id=thread.ident)
    try:
        thread.join(timeout=5)
    finally:
        set_interrupt(False, thread_id=thread.ident)
    assert outcome == [False]
    assert restart_gate.executing_tool_count() == 0


def test_wait_until_no_executing_tools_times_out_then_succeeds():
    release = threading.Event()

    def worker():
        with restart_gate.tool_execution_admission():
            release.wait(5)

    thread = threading.Thread(target=worker)
    thread.start()
    time.sleep(0.1)
    restart_gate.close_tool_gate()
    assert restart_gate.wait_until_no_executing_tools(0.3) is False
    release.set()
    assert restart_gate.wait_until_no_executing_tools(5) is True
    thread.join(timeout=5)


def test_coordinator_reports_not_started_when_parked_turn_is_interrupted(monkeypatch):
    calls: list[object] = []
    monkeypatch.setattr(coordinator, "_execute_admitted_tool_operation", lambda request: calls.append(request))
    request = SimpleNamespace(tool_name="write_file", operation_id="op-1", run_id="run-1", idempotency_scope="operation")
    restart_gate.close_tool_gate()
    results: list[object] = []

    def worker():
        results.append(coordinator.execute_tool_operation(request))

    thread = threading.Thread(target=worker)
    thread.start()
    time.sleep(0.2)
    set_interrupt(True, thread_id=thread.ident)
    try:
        thread.join(timeout=5)
    finally:
        set_interrupt(False, thread_id=thread.ident)
    assert calls == [], "被中断的等待不能执行工具"
    result = results[0]
    assert result.ok is False
    assert result.error_code == "CANCELLED"
    assert result.result_envelope["tool_operation"]["status"] == "not_started"
    assert result.result_envelope["tool_operation"]["action"] == "gateway_restart_drain"


def test_coordinator_runs_admitted_operation_when_gate_open(monkeypatch):
    monkeypatch.setattr(coordinator, "_execute_admitted_tool_operation", lambda request: ("ran", restart_gate.executing_tool_count()))
    assert coordinator.execute_tool_operation(SimpleNamespace()) == ("ran", 1)
    assert restart_gate.executing_tool_count() == 0
