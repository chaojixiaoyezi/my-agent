"""Gateway 全部后台循环的退避/限流/记账覆盖，以及包装异常按原因链归类。

09-29 ENOSPC 真机验收里，scheduler_due 的 tick 在磁盘写满时打了 `SchedulerDueIndexError` 且被归为 programmer_bug；
盘点后发现只有派发循环和后台主循环接了 LoopErrorBackoff。本文件锁定：
1. 维护循环、调度器到期循环、孤儿恢复循环、心跳循环持续出错时：线程不死、打印限流、次数按 loop id 进 loop_health；
   出错等待永远不比循环自己的间隔快（max(interval, 退避)），心跳循环不退避只限流；
2. 派发 tick 的段内错误也进同一份账本与限流：空闲时退避，请求在流动时不减速；
3. runtime_error_report 对包装异常沿 __cause__/__context__ 找环境类根因（OSError、sqlite3.OperationalError）归为 io，
   按类型不按消息文本，报告带 cause_type；根因是 ValueError 或没有根因仍是 programmer_bug。
撤修复即 FAIL，见各用例注释；全部确定性构造。
"""

from __future__ import annotations

import errno
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.gateway_parts import request_worker as rw
from agent_py_agent.agent.gateway_parts.loop_health import loop_health
from agent_py_agent.agent.runtime_errors import runtime_error_report
from agent_py_agent.agent.scheduler.due_index import SchedulerDueIndex, SchedulerDueIndexError
from agent_py_agent.cli import gateway_loops
from agent_py_agent.cli.gateway_loop_backoff import LoopErrorBackoff
from agent_py_agent.tests.test_gateway_admission_wait import _paths
from agent_py_agent.tests.test_gateway_dispatcher_resilience import (
    _enqueue,
    _FailingStderr,
    _RecordingStop,
    _stub_dispatcher,
)

_ELEVEN_ERRORS_FROM_0_2 = [0.2, 0.4, 0.8, 1.6, 3.2, 6.4, 12.8, 25.6, 30.0, 30.0, 30.0]


@pytest.fixture(autouse=True)
def _fresh_loop_health():
    loop_health.reset()
    yield
    loop_health.reset()


# LLM: 数打印次数不能靠 fixture 里替换 sys.stderr——pytest 在 call 阶段恢复捕获时会把 sys.stderr 换回自己的文件，
#   setup 阶段的替换会被覆盖；这里包一层真实打印入口，既计数又照常打印到 pytest 捕获的 stderr。
# 函数用途: 记录每次真的走到 _print_gateway_loop_error 的上下文。
@pytest.fixture()
def printed(monkeypatch) -> list[str]:
    calls: list[str] = []
    real = gateway_loops._print_gateway_loop_error

    def record(context: str, worker_id: str, exc: BaseException) -> None:
        calls.append(context)
        real(context, worker_id, exc)

    monkeypatch.setattr(gateway_loops, "_print_gateway_loop_error", record)
    return calls


def _always_failing(calls: dict):
    def tick(*args, **kwargs):
        calls["n"] += 1
        raise RuntimeError("tick keeps failing")

    return tick


def test_owner_maintenance_loop_never_retries_faster_than_its_interval(printed) -> None:
    controller = object.__new__(gateway_loops._GatewayOwnerMaintenanceController)
    controller.interval = 60.0
    calls = {"n": 0}
    controller.tick = _always_failing(calls)
    stop = _RecordingStop(limit=11)

    controller.run(stop)  # 撤修复：RuntimeError 从这里炸出，或等待变成 0.2 秒重试

    assert calls["n"] == 11 and stop.waits == [60.0] * 11, "退避只能放慢，不能把 60 秒一次的维护变成 0.2 秒重试"
    assert len(printed) == 2, loop_health.snapshot()  # 第 1 次与第 10 次
    assert loop_health.snapshot()["loop_error_counts"] == {"owner-maintenance": 11}
    assert loop_health.snapshot()["last_loop_errors"]["owner-maintenance"]["context"] == "gateway_owner_maintenance.tick"


def test_scheduler_due_loop_backs_off_and_throttles_prints(printed) -> None:
    controller = object.__new__(gateway_loops._GatewaySchedulerDueController)
    controller.interval = 0.2
    controller._due_index = SimpleNamespace(repair_legacy_owner_ledgers=lambda owners_dir: {"scanned": 0, "errors": 0})
    controller._base_agent = SimpleNamespace(home_paths=SimpleNamespace(owners_dir="unused"))
    calls = {"n": 0}
    controller.tick = _always_failing(calls)
    stop = _RecordingStop(limit=11)

    controller.run(stop)  # 撤修复：每 0.2 秒打一条，或异常炸出

    assert calls["n"] == 11 and stop.waits == _ELEVEN_ERRORS_FROM_0_2
    assert len(printed) == 2, loop_health.snapshot()
    assert loop_health.snapshot()["loop_error_counts"] == {"scheduler-due": 11}
    assert loop_health.snapshot()["dispatch_tick_errors"] == 0, "调度器的错误不混进派发计数"


def test_orphan_reconcile_loop_backs_off_and_releases_executors(printed) -> None:
    reconciler = object.__new__(gateway_loops._GatewayOrphanReconciler)
    reconciler.interval = 2.0  # poll_interval = 0.2
    reconciler._owner_worker_limit = 1
    reconciler._base_executor = None
    reconciler._owner_executor = None
    calls = {"n": 0}
    reconciler._async_tick = _always_failing(calls)
    stop = _RecordingStop(limit=11)

    reconciler.run(stop)

    assert calls["n"] == 11 and stop.waits == _ELEVEN_ERRORS_FROM_0_2
    assert len(printed) == 2, loop_health.snapshot()
    assert loop_health.snapshot()["loop_error_counts"] == {"orphan-reconcile": 11}
    assert reconciler._base_executor is None and reconciler._owner_executor is None  # finally 仍回收线程池


def test_heartbeat_loop_keeps_cadence_but_throttles_prints(printed, monkeypatch) -> None:
    calls = {"n": 0}
    monkeypatch.setattr(gateway_loops, "_write_gateway_heartbeat", lambda paths, agent, *, status, pid: _always_failing(calls)())
    monkeypatch.setattr(gateway_loops, "GATEWAY_HEARTBEAT_INTERVAL_SECONDS", 5.0)
    context = SimpleNamespace(paths=SimpleNamespace(), agent=SimpleNamespace(config=SimpleNamespace()))
    stop = _RecordingStop(limit=11)

    gateway_loops._gateway_heartbeat_loop(context, stop)  # 撤修复：每 5 秒打一条不限流

    assert calls["n"] == 11 and stop.waits == [5.0] * 11, "心跳不退避：节奏本身就是限流，退避只会推迟恢复被看见"
    assert len(printed) == 2, loop_health.snapshot()
    assert loop_health.snapshot()["loop_error_counts"] == {"heartbeat": 11}


def test_dispatcher_segment_errors_back_off_when_idle_and_count(printed, monkeypatch, tmp_path) -> None:
    paths = _paths(tmp_path)
    admission = rw.GatewayAdmission()
    monkeypatch.setattr(rw, "admission", admission)
    monkeypatch.setattr(gateway_loops, "admission", admission)
    dispatcher = _stub_dispatcher(paths, [], threading.Event())
    calls = {"n": 0}
    monkeypatch.setattr(gateway_loops, "dispatch_pending_requests", lambda *args, **kwargs: _always_failing(calls)())
    stop = _RecordingStop(limit=11)

    gateway_loops._run_request_dispatch(dispatcher, stop)  # 撤修复：段内错误每 0.2 秒打一条且不退避

    assert calls["n"] == 11 and stop.waits == _ELEVEN_ERRORS_FROM_0_2
    assert len(printed) == 2, loop_health.snapshot()
    snapshot = loop_health.snapshot()
    assert snapshot["dispatch_tick_errors"] == 11 and snapshot["loop_error_counts"] == {"dispatcher": 11}
    assert snapshot["last_dispatch_tick_error"]["context"] == "gateway_request_dispatch.iteration"
    assert snapshot["dispatch_tick_count"] == 11, "段内出错的 tick 照样记起止"


def test_dispatcher_keeps_pace_while_requests_flow_despite_segment_error(printed, monkeypatch, tmp_path) -> None:
    paths = _paths(tmp_path)
    admission = rw.GatewayAdmission()
    monkeypatch.setattr(rw, "admission", admission)
    monkeypatch.setattr(gateway_loops, "admission", admission)
    claims: list[str] = []
    dispatcher = _stub_dispatcher(paths, claims, threading.Event())
    dispatcher._recover_throttle = SimpleNamespace(due=lambda: True)  # 恢复段每拍都跑、每拍都失败
    dispatcher.bootstrap_agent = SimpleNamespace(
        config=SimpleNamespace(gateway_request_max_attempts=3, gateway_processing_timeout_seconds=120.0)
    )
    for index in range(3):
        _enqueue(paths, f"req-flow-{index}", f"user-{index}")
    calls = {"n": 0}
    monkeypatch.setattr(gateway_loops, "recover_gateway_processing_requests", lambda *a, **k: _always_failing(calls)())

    waits = [gateway_loops._guarded_dispatch_tick(dispatcher) for _ in range(3)]

    assert claims == ["req-flow-0", "req-flow-1", "req-flow-2"] and calls["n"] == 3
    assert waits[0] == 0.0, "第一拍派发到 3 条请求：恢复段失败也不减速"
    assert waits[1:] == [0.4, 0.8], "后两拍空闲且恢复段仍失败：按 max(轮询, 退避) 等，退避从第一拍的失败起累计"
    assert loop_health.snapshot()["dispatch_tick_errors"] == 3
    assert len(printed) == 1, loop_health.snapshot()


def test_loop_site_and_wait_after_error_floor() -> None:
    backoff = LoopErrorBackoff()
    assert gateway_loops._wait_after_loop_error(backoff, 1.0) == 1.0
    for _ in range(4):
        backoff.record_failure("k")
    assert backoff.delay() == 1.6 and gateway_loops._wait_after_loop_error(backoff, 1.0) == 1.6
    assert gateway_loops._wait_after_loop_error(backoff, 60.0) == 60.0


def _wrapped(cause: BaseException | None) -> SchedulerDueIndexError:
    try:
        if cause is not None:
            raise cause
        raise SchedulerDueIndexError("scheduler due index claim failed")
    except SchedulerDueIndexError as direct:
        return direct
    except BaseException as exc:
        try:
            raise SchedulerDueIndexError("scheduler due index claim failed") from exc
        except SchedulerDueIndexError as wrapped:
            return wrapped


def test_wrapped_environment_causes_are_classified_io_by_type() -> None:
    enospc = _wrapped(OSError(errno.ENOSPC, "No space left on device"))
    report = runtime_error_report(enospc, context="gateway_scheduler_due.tick")
    assert report["category"] == "io" and report["error_type"] == "SchedulerDueIndexError"  # 撤修复：programmer_bug
    assert report["cause_type"] == "OSError" and report["recoverable"] is True

    permission = _wrapped(PermissionError(errno.EACCES, "Permission denied"))  # 不特判 ENOSPC，任何 OSError 都算
    assert runtime_error_report(permission, context="x")["category"] == "io"

    disk_full = _wrapped(sqlite3.OperationalError("database or disk is full"))
    report = runtime_error_report(disk_full, context="gateway_scheduler_due.tick")
    assert report["category"] == "io" and report["cause_type"] == "OperationalError"

    logic = _wrapped(ValueError("bad row"))
    report = runtime_error_report(logic, context="x")
    assert report["category"] == "programmer_bug" and "cause_type" not in report

    plain = _wrapped(None)
    assert runtime_error_report(plain, context="x")["category"] == "programmer_bug"


def test_real_due_index_failure_keeps_cause_chain_and_classifies_io(tmp_path) -> None:
    directory_as_db = tmp_path / "scheduler_due.sqlite3"
    directory_as_db.mkdir()
    with pytest.raises(SchedulerDueIndexError) as info:
        SchedulerDueIndex(directory_as_db)  # 库文件路径是个目录：真实的 sqlite 环境错误（root 下也一样），不注入
    assert isinstance(info.value.__cause__, sqlite3.OperationalError)
    report = runtime_error_report(info.value, context="gateway_scheduler_due.initialize")
    assert report["category"] == "io" and report["cause_type"]
