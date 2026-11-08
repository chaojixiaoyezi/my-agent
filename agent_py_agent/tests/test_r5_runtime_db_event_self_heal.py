"""W18：权威准入保留三次同身份重读与结构化故障回执，延长 BUSY 退避。"""

from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.tooling import executor
from agent_py_agent.tests.test_plan_w4_storage_errors import (
    CountingTool,
    managed_store,
    run_tool,
    storage_error,
)


def _authority_request(store):
    return SimpleNamespace(operation_store=store, operation_owner_id="test-owner",
        write_boundary={}, trusted_run_context={"run_scope": {"task_id": "test-task"}})


def _call(number):
    return SimpleNamespace(run_id=f"run-{number}", operation_id=f"op-{number}",
        tool_name="probe", attempt_id=f"attempt-{number}")


@pytest.mark.parametrize("code", [5, 261])
def test_concurrent_authority_checks_preserve_three_read_budget(monkeypatch, code):
    barrier = threading.Barrier(22)
    lock = threading.Lock()
    reads, delays = {}, []

    class BusyStore:
        def require_authority(self, authority):
            with lock:
                attempts = reads.setdefault(authority.run_id, [])
                attempts.append(authority)
                count = len(attempts)
            if count < 3:
                raise storage_error(code)

    def sleep(delay):
        with lock:
            delays.append(delay)

    monkeypatch.setattr(executor, "time", SimpleNamespace(sleep=sleep))
    request = _authority_request(BusyStore())

    def check(number):
        barrier.wait(timeout=5)
        return executor._require_operation_authority(request, _call(number))

    with ThreadPoolExecutor(max_workers=22) as pool:
        assert list(pool.map(check, range(22))) == [None] * 22
    assert len(reads) == 22
    for calls in reads.values():
        assert len(calls) == 3 and all(authority is calls[0] for authority in calls)
        assert calls[0].owner_id == "test-owner" and calls[0].task_id == "test-task"
    assert sorted(delays) == sorted([0.060, 0.120] * 22)


@pytest.mark.parametrize("code", [5, 261])
def test_handler_runs_once_after_extended_authority_backoff(tmp_path, monkeypatch, code):
    _repo, store, attempt = managed_store(tmp_path)
    original = store.require_authority
    calls, delays = [], []

    def require(authority):
        calls.append(authority)
        if len(calls) < 3:
            raise storage_error(code)
        return original(authority)

    monkeypatch.setattr(store, "require_authority", require)
    monkeypatch.setattr(executor.time, "sleep", delays.append)
    tool = CountingTool()
    result = run_tool(tmp_path, store, tool, attempt=attempt)
    assert result.ok and result.handler_executed and tool.calls == 1
    assert len(calls) == 3 and all(authority is calls[0] for authority in calls)
    assert calls[0].attempt_id == attempt
    assert delays == [0.060, 0.120]


@pytest.mark.parametrize("code,cleanup_code,attempts,category", [
    (5, None, 3, "busy"), (261, None, 3, "busy"),
    (6, None, 1, "locked"), (11, None, 1, "structural"), (26, None, 1, "structural"),
    (1, None, 1, "other"), (None, None, 1, "other"), (5, 11, 1, "structural"),
])
def test_exhausted_or_nonbusy_authority_keeps_storage_error_and_blocks_handler(
    tmp_path, monkeypatch, code, cleanup_code, attempts, category,
):
    calls, delays = [], []
    error = storage_error(code) if code is not None else sqlite3.OperationalError("database is locked SQLITE_BUSY")
    if cleanup_code is not None:
        error.sqlite_cleanup_error = storage_error(cleanup_code)

    class BrokenStore:
        def require_authority(self, authority):
            calls.append(authority)
            raise error

    monkeypatch.setattr(executor.time, "sleep", delays.append)
    tool = CountingTool()
    result = run_tool(tmp_path, BrokenStore(), tool)
    assert not result.ok and not result.handler_executed and tool.calls == 0
    assert result.error_code == "TOOL_OPERATION_STORE_UNAVAILABLE"
    assert len(calls) == attempts and all(authority is calls[0] for authority in calls)
    assert delays == ([0.060, 0.120] if attempts == 3 else [])
    facts = result.metadata["handler_details"]["storage_error"]
    assert facts["sqlite_errorcode"] == code
    assert facts["sqlite_primary_code"] == (code & 0xFF if code is not None else None)
    assert facts["category"] == category
    if cleanup_code is not None:
        assert facts["cleanup_error"]["sqlite_primary_code"] == cleanup_code
