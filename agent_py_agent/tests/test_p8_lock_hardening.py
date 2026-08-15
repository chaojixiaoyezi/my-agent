"""T3 P8 收尾:task_lock 死锁根治+检测兜底、Windows 无 fcntl 文件锁降级告警。"""

import logging
import threading
import time

import agent_py_agent.agent.common.file_lock_support as fls
from agent_py_agent.agent.concurrency.exceptions import LockAcquisitionError
from agent_py_agent.agent.concurrency.task_lock import TaskLockManager


class _FastTimeoutConfig:
    concurrency_lock_enabled = True
    task_lock_timeout_seconds = 1  # 死锁检测兜底超时压到 1s,测试快


def _capture(sink: list) -> logging.Handler:
    class _H(logging.Handler):
        def emit(self, record):
            sink.append(record)
    handler = _H()
    handler.setLevel(logging.WARNING)
    return handler


# --- task_lock 死锁根治 ----------------------------------------------------


def test_read_write_interleave_no_deadlock():
    """旧 bug 复现:A 持读锁 → B 抢写锁(阻塞) → A 释放读锁。旧实现 acquire_write 持
    _write_lock 等 task 锁、release_read 又等 _write_lock,互等死锁。根治后必须全部完成。"""
    mgr = TaskLockManager()
    order: list[str] = []
    a_holds = threading.Event()
    b_blocking = threading.Event()

    def thread_a():
        mgr.acquire_read("t1")
        order.append("A_read")
        a_holds.set()
        b_blocking.wait(2)
        time.sleep(0.15)          # 让 B 卡进 acquire_write(旧实现此刻死锁)
        mgr.release_read("t1")    # 旧实现这里等 _write_lock(被 B 占)→死锁
        order.append("A_released")

    def thread_b():
        a_holds.wait(2)
        b_blocking.set()
        mgr.acquire_write("t1")   # 等 A 释放读锁
        order.append("B_write")
        mgr.release_write("t1")

    ta = threading.Thread(target=thread_a)
    tb = threading.Thread(target=thread_b)
    ta.start()
    tb.start()
    ta.join(5)
    tb.join(5)

    assert not ta.is_alive(), "线程A 卡死(死锁未根治)"
    assert not tb.is_alive(), "线程B 卡死(死锁未根治)"
    assert order == ["A_read", "A_released", "B_write"]


def test_acquire_timeout_raises_not_hangs():
    """死锁检测兜底:锁被别线程长期独占时,acquire 超时抛 LockAcquisitionError,不挂死。"""
    mgr = TaskLockManager(_FastTimeoutConfig())
    holder_ready = threading.Event()
    release = threading.Event()

    def holder():
        mgr.acquire_write("t2")
        holder_ready.set()
        release.wait(5)
        mgr.release_write("t2")

    th = threading.Thread(target=holder)
    th.start()
    holder_ready.wait(2)

    start = time.monotonic()
    raised = False
    try:
        mgr.acquire_read("t2")    # 被 holder 写锁独占,1s 超时
    except LockAcquisitionError as exc:
        raised = True
        assert exc.task_id == "t2"
    elapsed = time.monotonic() - start

    release.set()
    th.join(5)
    assert raised, "超时未抛错(可能仍在挂死)"
    assert elapsed < 4, f"超时耗时 {elapsed}s 异常(应 ~1s)"


def test_normal_lock_cycle_unaffected():
    """根治+兜底后,常规单线程读写锁周期语义不变(不误触发超时)。"""
    mgr = TaskLockManager()
    assert mgr.with_read_lock("t3", lambda: 7) == 7
    assert mgr.with_write_lock("t3", lambda: "ok") == "ok"
    mgr.acquire_write("t4")
    mgr.release_write("t4")  # 单线程 acquire/release 对称,不抛


# --- Windows 无 fcntl 文件锁降级告警 ---------------------------------------


def test_file_lock_warn_once():
    """无 fcntl 平台告警只发一次(不刷屏)。"""
    fls._warned = False
    records: list[logging.LogRecord] = []
    handler = _capture(records)
    logging.getLogger("agent.concurrency").addHandler(handler)
    try:
        fls.warn_file_lock_unavailable_once()
        fls.warn_file_lock_unavailable_once()
        fls.warn_file_lock_unavailable_once()
    finally:
        logging.getLogger("agent.concurrency").removeHandler(handler)
    warns = [r for r in records if "fcntl 不可用" in r.getMessage()]
    assert len(warns) == 1


def test_json_io_flock_warns_when_no_fcntl(monkeypatch, tmp_path):
    """模拟无 fcntl:locked_json_path → _flock_exclusive 走告警分支而非静默无锁。"""
    import agent_py_agent.agent.common.json_io as json_io
    fls._warned = False
    monkeypatch.setattr(json_io, "fcntl", None)
    records: list[logging.LogRecord] = []
    handler = _capture(records)
    logging.getLogger("agent.concurrency").addHandler(handler)
    try:
        with json_io.locked_json_path(tmp_path / "x.jsonl"):
            pass
    finally:
        logging.getLogger("agent.concurrency").removeHandler(handler)
    assert any("fcntl 不可用" in r.getMessage() for r in records)
