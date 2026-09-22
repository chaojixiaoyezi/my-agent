"""原目录锁的可取消等待；测试自己的线程和子进程，不修改产品运行时。"""

import subprocess
import sys
import threading
import time

import pytest

from agent_py_agent.agent.common.directory_lock import locked_private_directory
from agent_py_agent.tests.test_mcp_lifecycle import _run_thread


def test_thread_lock_wait_cancellation_releases_only_waiter(tmp_path):
    cancelled, entered = threading.Event(), threading.Event()
    def check():
        entered.set()
        if cancelled.is_set():
            raise TimeoutError("isolated cancellation")
    def waiter():
        with locked_private_directory(tmp_path, lock_name=".same.lock", wait_check=check):
            pytest.fail("waiter acquired held lock")
    with locked_private_directory(tmp_path, lock_name=".same.lock"):
        thread, values, errors = _run_thread(waiter)
        assert entered.wait(2)
        cancelled.set()
        thread.join(2)
        assert not thread.is_alive() and not values and len(errors) == 1
        assert isinstance(errors[0], TimeoutError)
    with locked_private_directory(tmp_path, lock_name=".same.lock", wait_check=lambda: None):
        assert (tmp_path / ".same.lock").exists()


def test_file_lock_wait_deadline_does_not_block_or_steal_other_process_lock(tmp_path):
    code = """import sys
from pathlib import Path
from agent_py_agent.agent.common.directory_lock import locked_private_directory
with locked_private_directory(Path(sys.argv[1]), lock_name='.same.lock'):
    print('held', flush=True)
    sys.stdin.read()
"""
    process = subprocess.Popen([sys.executable, "-c", code, str(tmp_path)],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline().strip() == "held"
        deadline = time.monotonic() + 0.1
        def check():
            if time.monotonic() >= deadline:
                raise TimeoutError("isolated deadline")
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            with locked_private_directory(tmp_path, lock_name=".same.lock", wait_check=check):
                pytest.fail("stole another process lock")
        assert time.monotonic() - started < 2 and process.poll() is None
        process.communicate(timeout=5)
        assert process.returncode == 0
        with locked_private_directory(tmp_path, lock_name=".same.lock", wait_check=lambda: None):
            pass
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()


def test_check_failure_just_after_thread_lock_acquisition_releases_mutex(tmp_path):
    checks = []
    def check():
        checks.append(1)
        if len(checks) == 2:
            raise TimeoutError("cancelled after thread lock acquired")
    with pytest.raises(TimeoutError):
        with locked_private_directory(tmp_path, lock_name=".same.lock", wait_check=check):
            pytest.fail("entered cancelled lock")
    with locked_private_directory(tmp_path, lock_name=".same.lock", wait_check=lambda: None):
        pass


def test_store_recovery_and_commit_are_not_interrupted_after_lock_admission(tmp_path, monkeypatch):
    from agent_py_agent.agent.tooling import process_session_store as stores
    from agent_py_agent.agent.tooling.background_process_launch import _reservation
    from agent_py_agent.tests._managed_process_harness import managed_request
    cancelled = threading.Event()
    recover = stores.recover_process_commit
    def recovery(root):
        cancelled.set()
        return recover(root)
    def check():
        if cancelled.is_set():
            raise TimeoutError("cancel during transaction")
    monkeypatch.setattr(stores, "recover_process_commit", recovery)
    request = managed_request(tmp_path)
    store = stores.ProcessSessionStore(request.store_root)
    with store.transaction(wait_check=check) as transaction:
        committed = transaction.write(_reservation(request, "bg-test-commit"))
    assert cancelled.is_set() and store.load("bg-test-commit").record == committed
