"""真实原global scope与JSON锁出生/追加叶合同；不创建替代authority。"""
from __future__ import annotations

import os
from contextvars import copy_context
from threading import Event, Thread, get_ident

import pytest

from agent_py_agent.agent.common import global_write_commit as commits
from agent_py_agent.agent.common import json_io, tool_write_context
from agent_py_agent.agent.common.global_write_guard import GlobalWriteInWorkerError


def test_required_scope_never_uses_tool_fallback(monkeypatch):
    def forbidden():
        raise AssertionError("explicit C must not create fallback scope")
    monkeypatch.setattr(tool_write_context, "original_tool_write_commit", forbidden)
    with pytest.raises(commits.GlobalWriteCommitUnavailable):
        commits.execute_global_write(lambda: pytest.fail("writer called"), require_scope=True)


def test_default_execute_keeps_original_direct_behavior(monkeypatch):
    monkeypatch.setattr(tool_write_context, "original_tool_write_commit", lambda: None)
    assert commits.execute_global_write(lambda: "original") == "original"


def test_required_scope_uses_same_original_nested_window():
    calls = []
    def commit(operation):
        calls.append(operation)
        return operation()
    with commits.global_write_commit_scope(commit):
        value = commits.execute_global_write(
            lambda: commits.execute_global_write(lambda: "same", require_scope=True), require_scope=True)
    assert value == "same"
    assert len(calls) == 1


def test_expired_required_scope_cannot_write():
    with commits.global_write_commit_scope(lambda operation: operation()):
        copied = copy_context()
    with pytest.raises(commits.GlobalWriteCommitUnavailable):
        copied.run(commits.execute_global_write, lambda: pytest.fail("late write"), require_scope=True)


def test_required_scope_cross_thread_cannot_write():
    errors = []
    with commits.global_write_commit_scope(lambda operation: operation()):
        copied = copy_context()
        def moved():
            try:
                copied.run(commits.execute_global_write, lambda: pytest.fail("foreign write"), require_scope=True)
            except BaseException as exc:
                errors.append(exc)
        thread = Thread(target=moved)
        thread.start()
        thread.join(timeout=2)
    assert not thread.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], commits.GlobalWriteCommitUnavailable)


def test_actual_global_guard_error_same_object(monkeypatch):
    error = GlobalWriteInWorkerError("original")
    def reject(resource):
        raise error
    monkeypatch.setattr(commits, "require_global_writer", reject)
    with pytest.raises(GlobalWriteInWorkerError) as caught:
        commits.execute_global_write(lambda: pytest.fail("writer called"), require_scope=True)
    assert caught.value is error


def test_source_error_same_object_without_new_error_type():
    error = commits.GlobalWriteCommitUnavailable("actual producer")
    def reject(operation):
        raise error
    with commits.global_write_commit_scope(reject), pytest.raises(commits.GlobalWriteCommitUnavailable) as caught:
        commits.execute_global_write(lambda: pytest.fail("writer called"), require_scope=True)
    assert caught.value is error


def test_lock_birth_rechecks_every_original_inode_retry(tmp_path, monkeypatch):
    path = tmp_path / "data.json"
    phases = []
    inside = False
    original_open = json_io._open_private_lock_descriptor
    original_flock = json_io._flock_descriptor
    original_matches = json_io._descriptor_matches_lock_path
    seen = []
    def birth(operation):
        nonlocal inside
        assert not inside
        inside = True
        try:
            return operation()
        finally:
            inside = False
    def opened(lock_path):
        assert inside
        fd = original_open(lock_path)
        phases.append("open")
        seen.append(fd)
        return fd
    def flock(fd, **kwargs):
        assert not inside
        phases.append("flock")
        return original_flock(fd, **kwargs)
    def matches(fd, lock_path):
        assert not inside
        phases.append("verify")
        return len(seen) > 1 and original_matches(fd, lock_path)
    monkeypatch.setattr(json_io, "_open_private_lock_descriptor", opened)
    monkeypatch.setattr(json_io, "_flock_descriptor", flock)
    monkeypatch.setattr(json_io, "_descriptor_matches_lock_path", matches)
    with json_io.locked_json_path(path, birth_commit=birth):
        assert not inside
    assert phases == ["open", "flock", "verify"] * 2
    for descriptor in seen:
        with pytest.raises(OSError):
            os.fstat(descriptor)


def test_lock_birth_revoke_on_reopen_does_not_reuse_old_fd(tmp_path, monkeypatch):
    error = commits.GlobalWriteCommitUnavailable("revoked")
    descriptors = []
    open_fd = json_io._open_private_lock_descriptor
    def opened(path):
        fd = open_fd(path)
        descriptors.append(fd)
        return fd
    calls = 0
    def birth(operation):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise error
        return operation()
    monkeypatch.setattr(json_io, "_open_private_lock_descriptor", opened)
    monkeypatch.setattr(json_io, "_descriptor_matches_lock_path", lambda *args: False)
    with pytest.raises(commits.GlobalWriteCommitUnavailable) as caught:
        with json_io.locked_json_path(tmp_path / "data.json", birth_commit=birth):
            pytest.fail("unverified lock")
    assert caught.value is error
    assert calls == 2
    for descriptor in descriptors:
        with pytest.raises(OSError):
            os.fstat(descriptor)


def test_lock_default_keeps_original_five_attempts(tmp_path, monkeypatch):
    calls = []
    target_lock_path = tmp_path / "data.json.lock"
    original = json_io._open_private_lock_descriptor
    original_matches = json_io._descriptor_matches_lock_path
    def opened(path):
        if path == target_lock_path:
            calls.append(path)
        return original(path)
    def matches(descriptor, path):
        # 只让本次目标锁身份失配；同进程外来锁必须保持原核验和功能。
        return False if path == target_lock_path else original_matches(descriptor, path)
    monkeypatch.setattr(json_io, "_open_private_lock_descriptor", opened)
    monkeypatch.setattr(json_io, "_descriptor_matches_lock_path", matches)
    with pytest.raises(BlockingIOError):
        with json_io.locked_json_path(tmp_path / "data.json"):
            pytest.fail("unverified lock")
    assert len(calls) == 5


def test_default_lock_retry_observation_does_not_capture_foreign_thread(tmp_path, monkeypatch):
    """外来锁首个FD先出生，再与目标测试交错；不让目标观测或mismatch影响其它锁。"""
    target_path = tmp_path / "data.json"
    foreign_path = tmp_path / "foreign.json"
    foreign_lock_path = tmp_path / "foreign.json.lock"
    first_foreign_open, release_foreign, foreign_finished = Event(), Event(), Event()
    main_thread = get_ident()
    foreign_acquired, foreign_errors, foreign_opens, foreign_identity_checks = [], [], [], []
    original_open = json_io._open_private_lock_descriptor
    original_matches = json_io._descriptor_matches_lock_path

    # LLM: 只安排合成本地锁的实际FD与原目标测试交错；异常时仍回收本次已打开FD，不起业务或网络线程。
    # 函数用途: 固定外来首open在目标hook之前，其余步骤在目标hook安装之后，避免依赖调度碰巧复现。
    def interleaved_open(path):
        descriptor = original_open(path)
        try:
            if path == foreign_lock_path:
                foreign_opens.append(path)
                if not first_foreign_open.is_set():
                    first_foreign_open.set()
                    assert release_foreign.wait(5), "目标测试未释放外来锁"
            elif path == target_path.with_name("data.json.lock") and get_ident() == main_thread:
                release_foreign.set()
                assert foreign_finished.wait(5), "外来锁未按期结束"
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor

    # LLM: 外来锁身份必须交原callee实际核验；此spy只记布尔结果，不替代文件系统事实。
    # 函数用途: 证明目标mismatch只拦目标，外来锁仍首轮按原inode身份进入临界区。
    def observed_matches(descriptor, path):
        matches = original_matches(descriptor, path)
        if path == foreign_lock_path:
            foreign_identity_checks.append(matches)
        return matches

    # LLM: 线程只获取这把合成本地锁；错误回主测试，finally显式通知，不能留下后台写者。
    # 函数用途: 用原locked_json_path检验外来锁功能，收尾由当前测试明确join。
    def foreign_lock():
        try:
            with json_io.locked_json_path(foreign_path):
                foreign_acquired.append(True)
        except BaseException as error:
            foreign_errors.append(error)
        finally:
            foreign_finished.set()

    monkeypatch.setattr(json_io, "_open_private_lock_descriptor", interleaved_open)
    monkeypatch.setattr(json_io, "_descriptor_matches_lock_path", observed_matches)
    worker = Thread(target=foreign_lock)
    worker.start()
    try:
        assert first_foreign_open.wait(5), "外来锁首FD未出生"
        with monkeypatch.context() as target_patch:
            # 直接运行原目标断言，旧全局hook确定性混入另外四次open，不能用镜像实现替代它。
            test_lock_default_keeps_original_five_attempts(tmp_path, target_patch)
    finally:
        release_foreign.set()
        worker.join(timeout=5)
    assert not worker.is_alive()
    assert foreign_errors == []
    assert foreign_acquired == [True]
    assert foreign_opens == [foreign_lock_path]
    assert foreign_identity_checks == [True]


def test_unlocked_jsonl_preserves_original_bytes_permissions_and_does_not_lock(tmp_path, monkeypatch):
    first = tmp_path / "normal.jsonl"
    second = tmp_path / "prelocked.jsonl"
    payload = [{"中文": "正文", "z": 1, "a": False}]
    json_io.append_private_jsonl_records(first, payload)
    with json_io.locked_json_path(second):
        def forbidden(*args, **kwargs):
            raise AssertionError("unlocked leaf must not acquire another lock")
        with monkeypatch.context() as patch:
            patch.setattr(json_io, "_locked_json_path", forbidden)
            json_io.append_private_jsonl_records_unlocked(second, payload)
    assert first.read_bytes() == second.read_bytes()
    assert second.stat().st_mode & 0o777 == 0o600


def test_unlocked_append_keeps_original_error_object(tmp_path, monkeypatch):
    error = PermissionError("physical")
    def reject(path):
        raise error
    monkeypatch.setattr(json_io, "_ensure_private_file", reject)
    with pytest.raises(PermissionError) as caught:
        json_io.append_private_text_unlocked(tmp_path / "log", "line\n")
    assert caught.value is error
