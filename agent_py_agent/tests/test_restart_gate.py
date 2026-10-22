from __future__ import annotations

import json
import threading
import time
from contextlib import ExitStack, nullcontext
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.concurrency import restart_gate
from agent_py_agent.agent.concurrency.interrupt import set_interrupt
from agent_py_agent.agent.tooling import tool_operation_coordinator as coordinator
from agent_py_agent.agent.tooling.models import ToolHandlerOutcome


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


# LLM: 保留关门等待/协作中断合同与CANCELLED兼容分类，另以宿主reported码区分重启排空和用户停止。
# 函数用途: 核验等待中断从未调用工具，并携带可诊断的重启排空结构事实。
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
    assert result.reported_error_code == "GATEWAY_RESTART_DRAINING"
    assert result.handler_executed is False and result.failure_stage == "runtime_gate"
    assert result.result_envelope["tool_operation"]["status"] == "not_started"
    assert result.result_envelope["tool_operation"]["action"] == "gateway_restart_drain"


def test_coordinator_runs_admitted_operation_when_gate_open(monkeypatch):
    monkeypatch.setattr(coordinator, "_execute_admitted_tool_operation", lambda request: ("ran", restart_gate.executing_tool_count()))
    assert coordinator.execute_tool_operation(SimpleNamespace()) == ("ran", 1)
    assert restart_gate.executing_tool_count() == 0


# LLM: 缺少新接口时直接给功能断言失败，不让导入错误掩盖缺失的快照能力。
# 函数用途: 取得真实快照入口供本文件新增回归调用。
def _snapshotter():
    snapshot = getattr(restart_gate, "executing_tool_snapshot", None)
    assert callable(snapshot), "缺少执行中工具快照接口"
    return snapshot


# LLM: 输入标签与返回行只做合同要求的浅拷贝；宿主时间覆盖同名标签，异常退出也不能漏占门记录。
# 函数用途: 核验快照登记、标签隔离以及正常/异常退出清理。
@pytest.mark.parametrize("raise_error", [False, True])
def test_snapshot_lifecycle_copies_labels_and_cleans_on_exit(monkeypatch, raise_error):
    snapshot = _snapshotter()
    monkeypatch.setattr(restart_gate, "time", SimpleNamespace(time=lambda: 1234.5, monotonic=time.monotonic))
    label = {"tool_name": "run_command", "operation_id": "op-1", "admitted_at": -1}
    with pytest.raises(ValueError, match="synthetic") if raise_error else nullcontext():
        with restart_gate.tool_execution_admission(label) as admitted:
            assert admitted is True
            assert snapshot() == [{"tool_name": "run_command", "operation_id": "op-1", "admitted_at": 1234.5}]
            label["tool_name"] = "changed-input"
            snapshot()[0]["operation_id"] = "changed-output"
            assert snapshot()[0]["tool_name"] == "run_command"
            assert snapshot()[0]["operation_id"] == "op-1"
            assert restart_gate.executing_tool_count() == len(snapshot()) == 1
            if raise_error:
                raise ValueError("synthetic")
    assert snapshot() == []
    assert restart_gate.executing_tool_count() == 0


# LLM: 无参调用仍准入并计数；只给未命名记录添加宿主时间，不伪造工具/owner身份。
# 函数用途: 保留历史无标签调用合同。
def test_snapshot_supports_unlabeled_admission():
    snapshot = _snapshotter()
    with restart_gate.tool_execution_admission() as admitted:
        assert admitted is True
        assert set(snapshot()[0]) == {"admitted_at"}
    assert snapshot() == []


# LLM: 只模拟墙钟排序，不改condition及实际token生命周期；负limit视为空，不以Python负切片泄出多行。
# 函数用途: 核验按准入时刻排序、默认20条上限及空/指定上限。
def test_snapshot_orders_admission_time_and_limits_rows(monkeypatch):
    snapshot = _snapshotter()
    ticks = iter([30.0, 10.0, 20.0, *range(40, 62)])
    monkeypatch.setattr(restart_gate, "time", SimpleNamespace(time=lambda: next(ticks)))
    with ExitStack() as stack:
        for index in range(25):
            stack.enter_context(restart_gate.tool_execution_admission({"operation_id": f"op-{index}"}))
        assert [row["operation_id"] for row in snapshot(3)] == ["op-1", "op-2", "op-0"]
        assert len(snapshot()) == 20 and len(snapshot(100)) == 25
        assert snapshot(0) == [] and snapshot(-1) == []
    assert snapshot() == []


# LLM: 重开门只影响等待者；同名操作的两个准入凭独立token清理，不能覆盖或误删另一占用。
# 函数用途: 验证重开期间仍能看见未退出工具，嵌套同名工具各自退场。
def test_reopening_preserves_live_rows_and_duplicate_operation_ids():
    snapshot = _snapshotter()
    label = {"operation_id": "same-op"}
    with restart_gate.tool_execution_admission(label):
        original = snapshot()
        restart_gate.close_tool_gate()
        restart_gate.open_tool_gate()
        assert snapshot() == original
        with restart_gate.tool_execution_admission(label):
            assert len(snapshot()) == restart_gate.executing_tool_count() == 2
        assert snapshot() == original
    assert snapshot() == []


# LLM: 以Condition.wait事件证明线程已停在关门处，再协作中断；未准入的标签不得出现在快照中。
# 函数用途: 核验等待/拒绝准入不被错误登记为正在执行。
def test_parked_interrupted_operation_never_enters_snapshot(monkeypatch):
    snapshot = _snapshotter()
    parked = threading.Event()
    outcomes = []
    wait = restart_gate._condition.wait

    # LLM: 观察原condition等待边界，不替换等待结果或缩短生产轮询周期。
    # 函数用途: 在工作线程真的进入关口等待时通知测试主线程。
    def observed_wait(timeout=None):
        parked.set()
        return wait(timeout)

    # LLM: 工作线程只尝试一次准入，结果交由主线程断言，避免线程内断言异常被忽略。
    # 函数用途: 记录一次被关门阻塞的工具准入结果。
    def worker():
        with restart_gate.tool_execution_admission({"operation_id": "parked"}) as admitted:
            outcomes.append(admitted)

    monkeypatch.setattr(restart_gate._condition, "wait", observed_wait)
    restart_gate.close_tool_gate()
    thread = threading.Thread(target=worker)
    thread.start()
    try:
        assert parked.wait(5)
        assert snapshot() == [] and restart_gate.executing_tool_count() == 0
        set_interrupt(True, thread_id=thread.ident)
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert outcomes == [False] and snapshot() == []
    finally:
        restart_gate.open_tool_gate()
        thread.join(timeout=5)
        set_interrupt(False, thread_id=thread.ident)


# LLM: 真实协调器及invoke入口都执行，只省略持久store；快照仅含四项身份和准入时刻，不含参数/幂等键/输出。
# 函数用途: 验证协调器在实际调用期间透传身份，正常及异常退出均清理快照。
@pytest.mark.parametrize("raise_error", [False, True])
def test_coordinator_registers_structured_identity_during_invocation(raise_error):
    snapshot = _snapshotter()
    observed = []
    result = ToolHandlerOutcome("run_command", True, "synthetic-output-must-not-enter-snapshot")

    # LLM: 调用真实快照函数观察协调器已登记内容；异常按原合同上抛，不模拟准入或计数。
    # 函数用途: 从工具实际执行点记录占门快照。
    def invoke():
        observed.extend(snapshot())
        if raise_error:
            raise ValueError("synthetic-invoke")
        return result

    request = coordinator.ToolOperationExecutionRequest(store=None, store_required=False, owner_id="owner-1",
        run_id="run-1", task_id="task-1", operation_id="op-1", tool_name="run_command",
        args_hash="synthetic-args-must-not-enter-snapshot", idempotency_key="private-key", idempotency_scope="operation",
        idempotency_namespace="synthetic", timeout_seconds=1, invoke=invoke)
    with pytest.raises(ValueError, match="synthetic-invoke") if raise_error else nullcontext():
        returned = coordinator.execute_tool_operation(request)
        assert returned is result
    assert len(observed) == 1
    row = dict(observed[0])
    assert isinstance(row.pop("admitted_at"), float)
    assert row == {"tool_name": "run_command", "operation_id": "op-1", "run_id": "run-1", "owner_id": "owner-1"}
    assert snapshot() == [] and restart_gate.executing_tool_count() == 0


# LLM: 结果对象reported码不会自动进入默认模型渲染；只检查宿主排空JSON的诊断字段，不改变规范CANCELLED控制码。
# 函数用途: 防止专用排空结构事实在结果渲染边界丢失。
def test_restart_draining_fact_survives_model_projection():
    request = SimpleNamespace(tool_name="write_file", operation_id="op-1", run_id="run-1", idempotency_scope="operation")
    result = coordinator._restart_gate_not_started(request)
    payload = json.loads(result.output)
    assert payload["error_code"] == result.error_code == "CANCELLED"
    assert payload.get("reported_error_code") == result.reported_error_code == "GATEWAY_RESTART_DRAINING"
    assert "GATEWAY_RESTART_DRAINING" in result.render_for_prompt()
