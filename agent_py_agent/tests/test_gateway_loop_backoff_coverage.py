"""Gateway 全部后台循环的退避/限流/记账覆盖，以及包装异常按原因链归类。

09-29 ENOSPC 真机验收里，scheduler_due 的 tick 在磁盘写满时打了 `SchedulerDueIndexError` 且被归为 programmer_bug；
盘点后发现只有派发循环和后台主循环接了 LoopErrorBackoff。本文件锁定：
1. 维护循环、调度器到期循环、孤儿恢复循环、心跳循环持续出错时：线程不死、打印限流、次数按 loop id 进 loop_health；
   出错等待永远不比循环自己的间隔快（max(interval, 退避)），心跳循环不退避只限流；
2. 派发 tick 的四个段都进同一份账本与限流；只有派发段失败（没请求可派时）让循环退避，恢复/终态投影/输入回执调和
   三个后台修复段失败只推迟各自的下次到期，派发轮询保持 0.2 秒（一张坏回执不能让新消息等 30 秒才被认领）；
3. runtime_error_report 对包装异常只沿显式 __cause__ 找环境类根因（OSError、sqlite3.OperationalError），category 不变
   （programmer_bug），另加 cause_type/cause_category 给循环错误打印和账本用；except/finally 里顺带发生的程序错误不沿
   __context__ 误归；取消工具的"确实不存在"只认 FileNotFoundError，不再把 category == "io" 当不存在。
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


def test_lock_sidecar_cleanup_segment_runs_when_due_then_waits_a_hour(monkeypatch, tmp_path) -> None:
    """锁 sidecar 清扫段：到点扫全部登记目录，下一拍按 1 小时节拍不再扫。"""
    paths = _paths(tmp_path)
    admission = rw.GatewayAdmission()
    monkeypatch.setattr(rw, "admission", admission)
    monkeypatch.setattr(gateway_loops, "admission", admission)
    dispatcher = _stub_dispatcher(paths, [], threading.Event())
    dispatcher._next_lock_cleanup_at = 0.0
    monkeypatch.setattr(gateway_loops, "dispatch_pending_requests", lambda *args, **kwargs: 0)
    scanned: list = []
    monkeypatch.setattr(
        gateway_loops,
        "gateway_lock_sidecar_dirs",
        lambda paths_: (tmp_path / "inbox", tmp_path / "processing"),
    )
    monkeypatch.setattr(
        gateway_loops,
        "cleanup_orphan_lock_files",
        lambda directory, **kwargs: scanned.append(directory) or 0,
    )

    dispatcher.tick()
    assert scanned == [tmp_path / "inbox", tmp_path / "processing"], "到点的清扫段必须扫全部登记目录"

    scanned.clear()
    dispatcher.tick()
    assert scanned == [], "清扫节拍 1 小时：紧接着的下一拍不再扫"


def test_recovery_segment_failures_never_slow_dispatch_polling(printed, monkeypatch, tmp_path) -> None:
    paths = _paths(tmp_path)
    admission = rw.GatewayAdmission()
    monkeypatch.setattr(rw, "admission", admission)
    monkeypatch.setattr(gateway_loops, "admission", admission)
    claims: list[str] = []
    dispatcher = _stub_dispatcher(paths, claims, threading.Event())
    deferred: list[float] = []
    dispatcher._recover_throttle = SimpleNamespace(due=lambda: True, defer=deferred.append)  # 恢复段每拍都跑、每拍都失败
    dispatcher.bootstrap_agent = SimpleNamespace(
        config=SimpleNamespace(gateway_request_max_attempts=3, gateway_processing_timeout_seconds=120.0)
    )
    for index in range(3):
        _enqueue(paths, f"req-flow-{index}", f"user-{index}")
    calls = {"n": 0}
    monkeypatch.setattr(gateway_loops, "recover_gateway_processing_requests", lambda *a, **k: _always_failing(calls)())

    waits = [gateway_loops._guarded_dispatch_tick(dispatcher) for _ in range(3)]

    assert claims == ["req-flow-0", "req-flow-1", "req-flow-2"] and calls["n"] == 3
    assert waits == [0.0, 0.2, 0.2], "恢复段失败只推迟恢复自己：派发轮询仍是 0.2 秒；撤修复：后两拍 0.4/0.8"
    assert deferred == [0.2, 0.4, 0.8], "恢复段自己的下次到期按该段退避推迟"
    assert loop_health.snapshot()["dispatch_tick_errors"] == 3
    assert len(printed) == 1, loop_health.snapshot()


def test_projection_segment_failures_never_slow_dispatch_polling(printed, monkeypatch, tmp_path) -> None:
    paths = _paths(tmp_path)
    dispatcher = _stub_dispatcher(paths, [], threading.Event())
    clock = {"t": 100.0}
    monkeypatch.setattr(gateway_loops.time, "monotonic", lambda: clock["t"])
    calls = {"n": 0}
    monkeypatch.setattr(gateway_loops, "repair_gateway_terminal_projections", lambda *a, **k: _always_failing(calls)())
    dispatcher._next_terminal_projection_at = 0.0

    waits, due_gaps = [], []
    for _ in range(6):
        clock["t"] += 1.0
        waits.append(gateway_loops._guarded_dispatch_tick(dispatcher))
        due_gaps.append(round(dispatcher._next_terminal_projection_at - clock["t"], 6))

    assert waits == [0.2] * 6, "一张坏回执让投影段每次都失败，派发轮询仍是 0.2 秒；撤修复：0.2/0.4/0.8…"
    assert calls["n"] == 5 and due_gaps == [0.75, 0.75, 0.8, 1.6, 0.6, 3.2], "投影段只推迟自己：max(0.75, 该段退避)"
    assert loop_health.snapshot()["dispatch_tick_errors"] == 5 and len(printed) == 1


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


def test_wrapped_environment_causes_keep_category_and_add_cause_fields() -> None:
    enospc = _wrapped(OSError(errno.ENOSPC, "No space left on device"))
    report = runtime_error_report(enospc, context="gateway_scheduler_due.tick")
    assert report["category"] == "programmer_bug" and report["error_type"] == "SchedulerDueIndexError"  # category 不变
    assert report["cause_type"] == "OSError" and report["cause_category"] == "io"  # 撤修复：没有归因字段

    permission = _wrapped(PermissionError(errno.EACCES, "Permission denied"))  # 不特判 ENOSPC，任何 OSError 都算
    assert runtime_error_report(permission, context="x")["cause_category"] == "io"

    disk_full = _wrapped(sqlite3.OperationalError("database or disk is full"))
    report = runtime_error_report(disk_full, context="gateway_scheduler_due.tick")
    assert report["cause_type"] == "OperationalError" and report["cause_category"] == "io"

    logic = _wrapped(ValueError("bad row"))
    report = runtime_error_report(logic, context="x")
    assert report["category"] == "programmer_bug" and "cause_type" not in report and "cause_category" not in report

    plain = _wrapped(None)
    assert "cause_type" not in runtime_error_report(plain, context="x")


def _raised_in_except_branch() -> BaseException:
    try:
        try:
            raise FileNotFoundError("ledger missing")
        except OSError:
            return {}["fallback"]  # except 分支里的程序错误：__context__ 是那个 OSError，__cause__ 为空
    except KeyError as exc:
        return exc


def _raised_in_finally() -> BaseException:
    try:
        try:
            raise FileNotFoundError("ledger missing")
        finally:
            None.attribute  # noqa: B018 - finally 里的程序错误，__context__ 是前面的 OSError
    except AttributeError as exc:
        return exc


def test_context_only_environment_errors_are_not_attributed() -> None:
    for exc in (_raised_in_except_branch(), _raised_in_finally()):
        assert isinstance(exc.__context__, OSError) and exc.__cause__ is None
        report = runtime_error_report(exc, context="x")  # 撤修复（沿 __context__）：这里会带 cause_category=io
        assert report["category"] == "programmer_bug" and "cause_category" not in report and "cause_type" not in report


def test_cancel_absent_target_only_trusts_file_not_found() -> None:
    from agent_py_agent.agent.agent_core.orchestration.tools import cancel as cancel_tool
    from agent_py_agent.tests.test_tool_scope_resolution import _cancel_agent

    assert cancel_tool._explicit_target_is_absent(_cancel_agent([]), "ghost") is True  # 真的不存在：FileNotFoundError
    locked = _wrapped(sqlite3.OperationalError("database is locked"))
    assert cancel_tool._explicit_target_is_absent(_cancel_agent([], load_error=locked), "child-1") is False
    denied = PermissionError(errno.EACCES, "Permission denied")  # category 是 io，但 run 存在；撤修复：判成不存在
    assert cancel_tool._explicit_target_is_absent(_cancel_agent([], load_error=denied), "child-1") is False


def test_real_due_index_failure_keeps_cause_chain_and_classifies_io(tmp_path) -> None:
    directory_as_db = tmp_path / "scheduler_due.sqlite3"
    directory_as_db.mkdir()
    with pytest.raises(SchedulerDueIndexError) as info:
        SchedulerDueIndex(directory_as_db)  # 库文件路径是个目录：真实的 sqlite 环境错误（root 下也一样），不注入
    assert isinstance(info.value.__cause__, sqlite3.OperationalError)
    report = runtime_error_report(info.value, context="gateway_scheduler_due.initialize")
    assert report["category"] == "programmer_bug" and report["cause_category"] == "io"
    assert report["cause_type"] == "OperationalError"
    assert loop_health.snapshot()["loop_error_counts"] == {}
    loop_health.note_loop_error("scheduler-due", "gateway_scheduler_due.initialize", info.value)
    assert loop_health.snapshot()["last_loop_errors"]["scheduler-due"]["cause_category"] == "io"  # 账本也带归因
