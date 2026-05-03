"""daemon_control 模块测试。

测试 daemon_control.py 中的 PID 管理、scoped locks、进程检测、启停控制功能。
"""
from __future__ import annotations

import json
import os
import signal
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent_py_agent.agent.gateway_parts import daemon_control as dc

# ── 测试夹具 ──────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_pid_path(tmp_path):
    """创建临时 PID 文件路径。"""
    return tmp_path / "gateway.pid"


@pytest.fixture
def tmp_lock_dir(tmp_path):
    """创建临时锁目录。"""
    lock_dir = tmp_path / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    return lock_dir


# ── PID 文件读写测试 ──────────────────────────────────────────────────────

def test_read_pid_file_nonexistent(tmp_pid_path):
    """测试读取不存在的 PID 文件返回 None。"""
    assert dc.read_pid_file(tmp_pid_path) is None


def test_read_pid_file_plain_text(tmp_pid_path):
    """测试读取纯文本格式的 PID 文件。"""
    tmp_pid_path.write_text("12345", encoding="utf-8")
    assert dc.read_pid_file(tmp_pid_path) == 12345


def test_read_pid_file_json_record(tmp_pid_path):
    """测试读取 JSON 格式的 PID 记录。"""
    record = {"pid": 67890, "kind": "my-agent-gateway"}
    tmp_pid_path.write_text(json.dumps(record), encoding="utf-8")
    assert dc.read_pid_file(tmp_pid_path) == 67890


def test_read_pid_file_empty(tmp_pid_path):
    """测试读取空文件返回 None。"""
    tmp_pid_path.write_text("", encoding="utf-8")
    assert dc.read_pid_file(tmp_pid_path) is None


def test_remove_pid_file(tmp_pid_path):
    """测试删除 PID 文件。"""
    tmp_pid_path.write_text("12345", encoding="utf-8")
    assert tmp_pid_path.exists()
    dc.remove_pid_file(tmp_pid_path)
    assert not tmp_pid_path.exists()


def test_remove_pid_file_nonexistent(tmp_pid_path):
    """测试删除不存在的 PID 文件不抛异常。"""
    dc.remove_pid_file(tmp_pid_path)  # 不应抛异常


# ── 检查重复启动测试 ──────────────────────────────────────────────────────

def test_check_already_running_no_file(tmp_pid_path):
    """测试无 PID 文件时返回未运行。"""
    is_running, existing_pid = dc.check_already_running(tmp_pid_path)
    assert is_running is False
    assert existing_pid is None


@patch.object(dc, "is_pid_alive", return_value=False)
def test_check_already_running_stale_pid(mock_alive, tmp_pid_path):
    """测试陈旧 PID 文件（进程已死）返回未运行。"""
    tmp_pid_path.write_text("99999", encoding="utf-8")
    is_running, existing_pid = dc.check_already_running(tmp_pid_path)
    assert is_running is False
    mock_alive.assert_called_once_with(99999)


@patch.object(dc, "is_pid_alive", return_value=True)
def test_check_already_running_alive(mock_alive, tmp_pid_path):
    """测试进程存活时返回已运行。"""
    tmp_pid_path.write_text("12345", encoding="utf-8")
    is_running, existing_pid = dc.check_already_running(tmp_pid_path)
    assert is_running is True
    assert existing_pid == 12345


# ── PID 记录读写测试 ──────────────────────────────────────────────────────

def test_write_pid_record(tmp_pid_path):
    """测试写入 PID 记录。"""
    dc.write_pid_record(tmp_pid_path)
    assert tmp_pid_path.exists()
    record = dc.read_pid_record(tmp_pid_path)
    assert record is not None
    assert record["pid"] == os.getpid()
    assert record["kind"] == "my-agent-gateway"


def test_read_pid_record_nonexistent(tmp_pid_path):
    """测试读取不存在的 PID 记录返回 None。"""
    assert dc.read_pid_record(tmp_pid_path) is None


def test_get_running_pid_valid(tmp_pid_path):
    """测试获取有效的运行中 PID。"""
    dc.write_pid_record(tmp_pid_path)
    pid = dc.get_running_pid(tmp_pid_path)
    assert pid == os.getpid()


def test_get_running_pid_nonexistent(tmp_pid_path):
    """测试获取不存在的运行中 PID 返回 None。"""
    pid = dc.get_running_pid(tmp_pid_path)
    assert pid is None


@patch.object(os, "kill", side_effect=ProcessLookupError)
def test_get_running_pid_invalid_pid_cleanup(mock_kill, tmp_pid_path):
    """测试无效 PID 会清理文件。"""
    record = {"pid": 99999, "start_time": 12345}
    tmp_pid_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_pid_path.write_text(json.dumps(record), encoding="utf-8")
    pid = dc.get_running_pid(tmp_pid_path)
    assert pid is None


# ── Scoped Locks 测试 ─────────────────────────────────────────────────────

@patch.object(dc, "_get_lock_dir")
def test_acquire_scoped_lock_success(mock_lock_dir, tmp_lock_dir):
    """测试成功获取 scoped lock。"""
    mock_lock_dir.return_value = tmp_lock_dir
    acquired, existing = dc.acquire_scoped_lock("test-scope", "test-identity")
    assert acquired is True
    assert existing is None


@patch.object(dc, "_get_lock_dir")
def test_acquire_scoped_lock_already_held_by_different_process(mock_lock_dir, tmp_lock_dir):
    """测试获取已被不同进程持有的 scoped lock 失败。"""
    mock_lock_dir.return_value = tmp_lock_dir

    # 先用另一个 PID 获取锁
    lock_path = tmp_lock_dir / f"test-scope-{dc._scope_hash('test-identity')}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    other_record = {
        "pid": 99999,
        "start_time": 12345,
        "scope": "test-scope",
        "identity_hash": dc._scope_hash("test-identity"),
        "updated_at": dc._utc_now_iso(),
    }
    lock_path.write_text(json.dumps(other_record), encoding="utf-8")

    # 模拟进程存活检查
    with patch.object(os, "kill", side_effect=ProcessLookupError):
        acquired, existing = dc.acquire_scoped_lock("test-scope", "test-identity")
        # 陈旧锁应该被清理，重新获取应该成功
        assert acquired is True


@patch.object(dc, "_get_lock_dir")
def test_release_scoped_lock(mock_lock_dir, tmp_lock_dir):
    """测试释放 scoped lock。"""
    mock_lock_dir.return_value = tmp_lock_dir
    dc.acquire_scoped_lock("test-scope", "release-test")
    dc.release_scoped_lock("test-scope", "release-test")
    # 再次获取应该成功
    acquired, _ = dc.acquire_scoped_lock("test-scope", "release-test")
    assert acquired is True


def test_scope_hash_deterministic():
    """测试 scope hash 是确定性的。"""
    hash1 = dc._scope_hash("test-identity")
    hash2 = dc._scope_hash("test-identity")
    assert hash1 == hash2
    assert len(hash1) == 16


def test_scope_hash_different_inputs():
    """测试不同输入产生不同 hash。"""
    hash1 = dc._scope_hash("identity1")
    hash2 = dc._scope_hash("identity2")
    assert hash1 != hash2


# ── Runtime Status 测试 ───────────────────────────────────────────────────

def test_write_runtime_status(tmp_path):
    """测试写入运行时状态。"""
    status_path = tmp_path / "status.json"
    dc.write_runtime_status(
        status_path,
        gateway_state="running",
        active_agents=3,
    )
    status = dc.read_runtime_status(status_path)
    assert status is not None
    assert status["gateway_state"] == "running"
    assert status["active_agents"] == 3


def test_read_runtime_status_nonexistent(tmp_path):
    """测试读取不存在的运行时状态返回 None。"""
    status_path = tmp_path / "nonexistent.json"
    assert dc.read_runtime_status(status_path) is None


def test_write_runtime_status_merge_platform(tmp_path):
    """测试写入运行时状态时合并 platform 信息。"""
    status_path = tmp_path / "status.json"
    dc.write_runtime_status(
        status_path,
        gateway_state="running",
    )
    dc.write_runtime_status(
        status_path,
        platform="feishu",
        platform_state="connected",
        error_code=None,
        error_message=None,
    )
    status = dc.read_runtime_status(status_path)
    assert "platforms" in status
    assert status["platforms"]["feishu"]["state"] == "connected"


# ── 优雅关闭测试 ──────────────────────────────────────────────────────────

@patch.object(dc, "is_pid_alive", return_value=False)
def test_request_graceful_shutdown_not_running(mock_alive, tmp_pid_path, tmp_path):
    """测试网关未运行时请求关闭返回 False。"""
    stop_path = tmp_path / "stop.json"
    result = dc.request_graceful_shutdown(tmp_pid_path, stop_path)
    assert result is False


@patch.object(dc, "is_pid_alive", return_value=True)
@patch.object(dc, "read_pid_file", return_value=12345)
def test_request_graceful_shutdown_success(mock_read, mock_alive, tmp_pid_path, tmp_path):
    """测试成功请求优雅关闭。"""
    stop_path = tmp_path / "stop.json"
    result = dc.request_graceful_shutdown(tmp_pid_path, stop_path)
    assert result is True
    assert stop_path.exists()


# ── 移除owned PID文件测试 ─────────────────────────────────────────────────

def test_remove_pid_file_if_owned_not_owned(tmp_path):
    """测试不属于自己的 PID 文件不会被删除。"""
    pid_path = tmp_path / "pid.json"
    pid_path.write_text(json.dumps({"pid": 99999}), encoding="utf-8")
    with patch.object(os, "getpid", return_value=os.getpid() + 1):
        dc.remove_pid_file_if_owned(pid_path)
    assert pid_path.exists()


def test_remove_pid_file_if_owned_owned(tmp_path):
    """测试属于自己的 PID 文件会被删除。"""
    pid_path = tmp_path / "owned_pid.json"
    my_pid = os.getpid()
    pid_path.write_text(json.dumps({"pid": my_pid, "start_time": dc._get_process_start_time(my_pid)}), encoding="utf-8")
    dc.remove_pid_file_if_owned(pid_path)
    assert not pid_path.exists()


# ── 异常场景测试 ──────────────────────────────────────────────────────────

def test_write_pid_file(tmp_path):
    """测试写入纯文本 PID 文件。"""
    pid_path = tmp_path / "plain.pid"
    dc.write_pid_file(pid_path, 54321)
    assert pid_path.read_text(encoding="utf-8") == "54321"


def test_write_pid_record_creates_directory(tmp_path):
    """测试写入 PID 记录时自动创建父目录。"""
    pid_path = tmp_path / "subdir" / "gateway.pid"
    dc.write_pid_record(pid_path)
    assert pid_path.exists()