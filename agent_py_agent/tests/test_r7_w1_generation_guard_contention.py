"""R7-W1：同进程线程同时建连只等待代次门的进程内互斥，不得误报 generation_busy；EX 排空语义不变。"""
import os
import sqlite3
import threading
from contextlib import closing
from pathlib import Path

import pytest

from agent_py_agent.agent.common import json_io
from agent_py_agent.agent.local_storage.store import LocalStore
from agent_py_agent.agent.runtime_db import recovery_guard as guard


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "runtime.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE facts(value)")
    connection.close()
    yield path
    # 所有共享门都已随连接物理关闭归还时，EX 才能成功。
    with guard.runtime_db_generation_guard(path, exclusive=True):
        pass


def _mutex_for(path: Path):
    key = str(Path(os.path.abspath(path)).with_name(path.name + ".generation.lock"))
    with json_io._JSON_FILE_LOCKS_GUARD:
        handle = json_io._JSON_FILE_LOCKS.get(key)
        if handle is None:
            handle = json_io._PathLock()
            json_io._JSON_FILE_LOCKS[key] = handle
    return handle


def test_shared_admission_waits_for_in_process_mutex_instead_of_busy(database):
    handle = _mutex_for(database)  # 强引用保持弱字典中的同一把锁
    started, results = threading.Event(), []

    def admit():
        started.set()
        try:
            with guard.runtime_db_generation_guard(database):
                results.append("admitted")
        except BaseException as error:  # noqa: BLE001 记录真实失败类型供断言
            results.append(error)

    assert handle.lock.acquire(timeout=5)
    thread = threading.Thread(target=admit)
    try:
        thread.start()
        assert started.wait(5)
        thread.join(0.2)
        assert results == [], f"共享准入不得因进程内互斥竞争立即失败: {results!r}"
    finally:
        handle.lock.release()
    thread.join(5)
    assert not thread.is_alive()
    assert results == ["admitted"]


def test_concurrent_guarded_connects_never_report_generation_busy(database):
    errors, barrier = [], threading.Barrier(12)

    def worker():
        try:
            barrier.wait(5)
            for _ in range(40):
                conn = guard.guarded_runtime_connect(database, lambda: sqlite3.connect(database))
                try:
                    conn.execute("SELECT count(*) FROM facts").fetchone()
                finally:
                    guard.close_runtime_connection(conn)
        except BaseException as error:  # noqa: BLE001 工作线程的异常必须传回测试线程
            errors.append(error)

    threads = [threading.Thread(target=worker) for _ in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert not any(thread.is_alive() for thread in threads)
    assert errors == []


def test_local_store_reads_under_thread_contention_stay_indexed(tmp_path):
    store = LocalStore(tmp_path / "local_store" / "local.db", enable_fts=False)
    errors, barrier = [], threading.Barrier(8)

    def worker():
        try:
            barrier.wait(5)
            for _ in range(25):
                store.list_agent_tree("task-root")
        except BaseException as error:  # noqa: BLE001 任何异常都会让活动面退回历史全扫
            errors.append(type(error).__name__ + ":" + str(error))

    # R7-A-W1 的零等待只读连接不负责创建 WAL 侧车；保留一个真实读者，隔离最后读者关闭时的侧车回收竞争。
    with closing(sqlite3.connect(store.db_path)) as keeper:
        keeper.execute("SELECT count(*) FROM legacy_agent_runs").fetchone()
        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        assert not any(thread.is_alive() for thread in threads)
        assert errors == []


def test_exclusive_recovery_still_refuses_while_a_shared_connection_is_open(database):
    conn = guard.guarded_runtime_connect(database, lambda: sqlite3.connect(database))
    try:
        with pytest.raises(guard.RuntimeDatabaseDeferred, match="generation_busy"):
            with guard.runtime_db_generation_guard(database, exclusive=True):
                pytest.fail("共享连接未关闭时 EX 不能证明排空")
    finally:
        guard.close_runtime_connection(conn)
