"""线程出生、请求上下文和取消后的真实复用探针。

生产 daemon 池的隔离失败属于 S2-W5B；stdlib 对照只核解释器的真实继承语义。
-X thread_inherit_context 在独立解释器中切换，不能用 monkeypatch 模拟 flags。
"""
from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from contextvars import copy_context
from pathlib import Path

import pytest

from agent_py_agent.agent.io import sqlite_scope
from agent_py_agent.agent.scheduler.monitor_pool import MonitorPool

CONTEXT_WORKER_COUNT = 1
WAIT_TIMEOUT_SECONDS = 15
SUBPROCESS_TIMEOUT_SECONDS = 45
REPO_ROOT = Path(__file__).resolve().parents[3]
HAS_THREAD_CONTEXT_FLAG = hasattr(sys.flags, "thread_inherit_context")

# 子进程同时导入生产池和 stdlib 池，保留真实线程默认出生上下文。
_POOL_PROBE = textwrap.dedent('''\
    import json, sys, threading
    from contextvars import ContextVar, copy_context
    from concurrent.futures import ThreadPoolExecutor
    from agent_py_agent.agent.concurrency.daemon_executor import DurableDaemonThreadPoolExecutor
    owner = ContextVar("ft_owner", default=None)
    cls = {"stdlib": ThreadPoolExecutor, "durable": DurableDaemonThreadPoolExecutor}[sys.argv[1]]
    cancelled = sys.argv[2] == "cancelled"
    entered, release = threading.Event(), threading.Event()
    ran_cancelled = threading.Event()
    def task():
        entered.set()
        assert release.wait(15)
        return threading.get_ident(), owner.get()
    with cls(max_workers=1) as pool:
        token = owner.set("owner-A")
        try:
            first = pool.submit(task)  # owner A 的请求触发 0 -> 1 扩容
        finally:
            owner.reset(token)
        assert entered.wait(15)
        if cancelled:
            token = owner.set("cancelled-owner")
            try:
                future = pool.submit(copy_context().run, ran_cancelled.set)
            finally:
                owner.reset(token)
            assert future.cancel()
        release.set()
        first_ident, first_value = first.result(15)
        next_ident, next_value = pool.submit(lambda: (threading.get_ident(), owner.get())).result(15)
        assert first_ident == next_ident, "test did not reuse the same worker"
        assert not ran_cancelled.is_set()
    print(json.dumps({"inherited": next_value, "first": first_value,
                      "thread_inherit_context": getattr(sys.flags, "thread_inherit_context", None)}))
''')


def _probe_executor(pool_kind, case, inherit):
    if inherit is not None and not HAS_THREAD_CONTEXT_FLAG:
        pytest.skip("解释器未提供 thread_inherit_context；默认出生路径仍实测")
    flags = [] if inherit is None else ["-X", f"thread_inherit_context={inherit}"]
    result = subprocess.run(
        [sys.executable, *flags, "-c", _POOL_PROBE, pool_kind, case],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT_SECONDS,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    observed = json.loads(result.stdout.splitlines()[-1])
    if inherit is not None:
        assert observed["thread_inherit_context"] == inherit
    return observed


@pytest.mark.parametrize("case", ["next_owner", "cancelled"])
@pytest.mark.parametrize("inherit", [None, 0, 1])
def test_stdlib_thread_birth_reports_interpreter_context_semantics(case, inherit):
    observed = _probe_executor("stdlib", case, inherit)
    expected = "owner-A" if observed["thread_inherit_context"] else None
    assert observed["first"] == expected
    assert observed["inherited"] == expected


@pytest.mark.parametrize("case", ["next_owner", "cancelled"])
@pytest.mark.parametrize("inherit", [None, 0, 1])
def test_durable_executor_reuse_does_not_retain_first_owner(case, inherit):
    observed = _probe_executor("durable", case, inherit)
    assert observed["inherited"] is None, (
        f"S2-W5B thread-reuse: durable/{case} leaked first owner's context: {observed}"
    )


# LLM: Preserve the inventory's named raw-pool case and verify empty birth as well as reuse.
# 函数用途: 通过原生子进程证明新工作线程从出生起就不继承触发扩容的owner。
def test_raw_executor_reuse_does_not_retain_first_owner():
    observed = _probe_executor("durable", "next_owner", None)
    assert observed["first"] is None
    assert observed["inherited"] is None


# LLM: Exercise the two inventory targets through real thread birth, reuse and queued cancellation.
# 函数用途: 分别核验执行器类的取消路径和扩容方法的后续owner复用路径。
@pytest.mark.parametrize("case", ["cancelled", "next_owner"], ids=[
    "agent/concurrency/daemon_executor.py:DurableDaemonThreadPoolExecutor",
    "agent/concurrency/daemon_executor.py:_adjust_thread_count",
])
def test_thread_submission_target(case):
    observed = _probe_executor("durable", case, None)
    assert observed["first"] is None
    assert observed["inherited"] is None


def test_monitor_submit_binds_each_request_context_and_restores_worker(tmp_path):
    from contextvars import ContextVar
    from threading import Event, get_ident

    owner = ContextVar("ft_monitor_owner", default=None)
    pool = MonitorPool(max_workers=CONTEXT_WORKER_COUNT)
    (tmp_path / "A").mkdir()
    (tmp_path / "B").mkdir()
    entered, release, cancelled_ran = Event(), Event(), Event()

    def first_task():
        entered.set()
        assert release.wait(WAIT_TIMEOUT_SECONDS)
        return get_ident(), owner.get()

    try:
        token = owner.set("owner-A")
        try:
            first = pool.submit(tmp_path / "A", "monitor", "run-A", first_task)
        finally:
            owner.reset(token)
        assert entered.wait(WAIT_TIMEOUT_SECONDS)
        token = owner.set("cancelled-owner")
        try:
            cancelled = pool.submit(tmp_path / "A", "cancelled", "run-cancelled", cancelled_ran.set)
        finally:
            owner.reset(token)
        assert cancelled.cancel()
        release.set()
        first_thread, first_owner = first.result(WAIT_TIMEOUT_SECONDS)
        token = owner.set("owner-B")
        try:
            second = pool.submit(tmp_path / "B", "monitor", "run-B", lambda: (get_ident(), owner.get()))
        finally:
            owner.reset(token)
        second_thread, second_owner = second.result(WAIT_TIMEOUT_SECONDS)
        assert first_thread == second_thread
        assert (first_owner, second_owner) == ("owner-A", "owner-B")
        assert not cancelled_ran.is_set()
        assert cancelled.cancelled()

        def failing_task():
            owner.set("task-local-mutation")
            raise ValueError("test task failure")

        failed = pool.submit(tmp_path / "A", "failure", "run-failed", failing_task)
        with pytest.raises(ValueError, match="test task failure"):
            failed.result(WAIT_TIMEOUT_SECONDS)
        empty = pool.submit(tmp_path / "B", "empty", "run-empty", lambda: (get_ident(), owner.get()))
        assert empty.result(WAIT_TIMEOUT_SECONDS) == (first_thread, None)
        assert owner.get() is None
    finally:
        release.set()
        pool.shutdown()


def test_ended_sqlite_scope_cannot_be_revived_in_copied_context(tmp_path):
    import sqlite3
    from concurrent.futures import ThreadPoolExecutor

    path = tmp_path / "scope.sqlite3"
    with sqlite_scope.sqlite_connection_scope(enabled=True):
        scope = sqlite_scope._SQLITE_ACTIVE_SCOPE.get()
        copied = copy_context()
        with sqlite_scope.scoped_connection(path, False, lambda: sqlite3.connect(path)) as connection:
            connection.execute("SELECT 1")
        assert scope is not None and not scope.closed
    assert scope.closed
    assert not scope.entries

    def use_copy():
        assert sqlite_scope._SQLITE_ACTIVE_SCOPE.get() is scope
        assert not sqlite_scope._scope_owned(scope)
        assert sqlite_scope.sqlite_connection_factory() is sqlite3.Connection
        with sqlite_scope.scoped_connection(path, False, lambda: sqlite3.connect(path)) as fresh:
            assert fresh.execute("SELECT 23").fetchone() == (23,)
            assert type(fresh) is sqlite3.Connection
        assert scope.closed and not scope.entries

    with ThreadPoolExecutor(max_workers=CONTEXT_WORKER_COUNT) as executor:
        executor.submit(copied.run, use_copy).result(WAIT_TIMEOUT_SECONDS)
    # 原线程再进入副本同样不能复活；物理缓存及旧租期都已结束。
    copied.run(use_copy)
    assert sqlite_scope._SQLITE_ACTIVE_SCOPE.get() is None
