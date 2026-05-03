"""分布式锁测试 - dispatch_lock.py 死锁检测、锁获取释放。"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class TestDispatchWatchLock:
    """测试 _DispatchWatchLock 分布式锁。"""

    def test_lock_acquire_success(self, tmp_path: Path):
        """成功获取锁。"""
        from agent_py_agent.agent.agent_core.dispatch_lock import _DispatchWatchLock

        lock_path = tmp_path / "dispatch.lock"

        lock = _DispatchWatchLock(lock_path)
        with lock as l:
            assert l.acquired is True
            assert l.token is not None

    def test_lock_release_on_context_exit(self, tmp_path: Path):
        """上下文退出时释放锁。"""
        from agent_py_agent.agent.agent_core.dispatch_lock import _DispatchWatchLock

        lock_path = tmp_path / "dispatch.lock"

        lock = _DispatchWatchLock(lock_path)
        with lock:
            assert lock_path.exists()

        # 退出后锁文件应该被删除
        assert not lock_path.exists()

    def test_lock_not_released_if_token_mismatch(self, tmp_path: Path):
        """token 不匹配时不释放锁。"""
        from agent_py_agent.agent.agent_core.dispatch_lock import _DispatchWatchLock

        lock_path = tmp_path / "dispatch.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)

        # 先写入一个不同 token 的锁
        other_payload = {
            "token": "different_token",
            "pid": 99999,
            "created_at": time.time(),
        }
        lock_path.write_text(json.dumps(other_payload), encoding="utf-8")

        lock = _DispatchWatchLock(lock_path)
        lock.acquired = True  # 模拟已获取锁

        # 退出时 token 不匹配，不删除锁文件
        lock.__exit__(None, None, None)
        assert lock_path.exists()

    def test_lock_already_exists_raises(self, tmp_path: Path):
        """锁已存在时抛出异常。"""
        from agent_py_agent.agent.agent_core.dispatch_lock import _DispatchWatchLock

        lock_path = tmp_path / "dispatch.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)

        # 先创建一个锁文件
        existing_payload = {
            "token": "existing_token",
            "pid": os.getpid(),
            "created_at": time.time(),
        }
        lock_path.write_text(json.dumps(existing_payload), encoding="utf-8")

        lock = _DispatchWatchLock(lock_path)

        with pytest.raises(RuntimeError, match="dispatch watch lock 已存在"):
            lock.__enter__()

    def test_force_lock_removes_existing(self, tmp_path: Path):
        """force=True 时移除已存在的锁。"""
        from agent_py_agent.agent.agent_core.dispatch_lock import _DispatchWatchLock

        lock_path = tmp_path / "dispatch.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text("{}", encoding="utf-8")

        lock = _DispatchWatchLock(lock_path, force=True)
        with lock:
            assert lock.acquired is True
            assert lock_path.exists()

    def test_lock_with_different_tokens(self, tmp_path: Path):
        """两个锁实例使用不同的 token。"""
        from agent_py_agent.agent.agent_core.dispatch_lock import _DispatchWatchLock

        lock_path = tmp_path / "dispatch.lock"

        lock1 = _DispatchWatchLock(lock_path)
        lock2 = _DispatchWatchLock(lock_path)

        with lock1:
            # 读取锁内容验证 token
            payload = json.loads(lock_path.read_text(encoding="utf-8"))
            assert payload["token"] == lock1.token
            assert payload["token"] != lock2.token

    def test_lock_token_is_hex_uuid(self, tmp_path: Path):
        """token 是十六进制 UUID。"""
        from agent_py_agent.agent.agent_core.dispatch_lock import _DispatchWatchLock

        lock_path = tmp_path / "dispatch.lock"
        lock = _DispatchWatchLock(lock_path)

        assert len(lock.token) == 32  # UUID hex is 32 chars
        assert all(c in "0123456789abcdef" for c in lock.token)

    def test_lock_pid_is_current_process(self, tmp_path: Path):
        """锁中记录的是当前进程 PID。"""
        from agent_py_agent.agent.agent_core.dispatch_lock import _DispatchWatchLock

        lock_path = tmp_path / "dispatch.lock"

        lock = _DispatchWatchLock(lock_path)
        with lock:
            payload = json.loads(lock_path.read_text(encoding="utf-8"))
            assert payload["pid"] == os.getpid()


class TestLockEdgeCases:
    """锁的边界情况测试。"""

    def test_lock_exit_without_acquire(self, tmp_path: Path):
        """未获取锁时调用 exit 不做任何事。"""
        from agent_py_agent.agent.agent_core.dispatch_lock import _DispatchWatchLock

        lock_path = tmp_path / "dispatch.lock"
        lock = _DispatchWatchLock(lock_path)

        # 不进入 context，直接 exit
        lock.__exit__(None, None, None)
        # 不应该抛出异常

    def test_lock_exit_with_corrupted_file(self, tmp_path: Path):
        """锁文件损坏时的处理。"""
        from agent_py_agent.agent.agent_core.dispatch_lock import _DispatchWatchLock

        lock_path = tmp_path / "dispatch.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text("not valid json{{{", encoding="utf-8")

        lock = _DispatchWatchLock(lock_path)
        lock.acquired = True

        # 损坏的文件不应该导致崩溃
        lock.__exit__(None, None, None)

    def test_lock_exit_when_file_deleted(self, tmp_path: Path):
        """退出时锁文件已被删除的处理。"""
        from agent_py_agent.agent.agent_core.dispatch_lock import _DispatchWatchLock

        lock_path = tmp_path / "dispatch.lock"
        lock = _DispatchWatchLock(lock_path)
        lock.acquired = True

        # 锁文件不存在时退出不应该崩溃
        lock.__exit__(None, None, None)