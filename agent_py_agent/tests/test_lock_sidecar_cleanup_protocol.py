# LLM: obsfix34b 锁 sidecar 清理协议用例：清理者持锁 unlink + 加锁方 inode 身份核对。
#   钉住：活锁不删（回归双重后缀 bug）、Windows unlink 失败不删除、清理与迟到者/新来者任何交错
#   最多一方在临界区、身份核对重试有上限且重试后能拿到有效锁。
# 模块用途: 用确定性时序（unlink 钩子 + 线程）覆盖锁清理协议的并发安全。

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

from agent_py_agent.agent.gateway_parts.io import (
    _LOCK_IDENTITY_RETRY_COUNT,
    _flock_exclusive,
    _open_private_lock_descriptor,
    cleanup_orphan_lock_files,
)
from agent_py_agent.agent.gateway_parts.io import _locked_file_path as gateway_locked_file_path


# 函数用途: 在临时目录造一个"孤儿锁"（数据文件不存在、mtime 早于 24 小时宽限）。
def _orphan_lock(tmp_path: Path, name: str = "x.json.lock") -> Path:
    lock_path = tmp_path / name
    lock_path.write_text("", encoding="utf-8")
    stamp = time.time() - 25 * 3600
    os.utime(lock_path, (stamp, stamp))
    return lock_path


# 函数用途: 真实持有者（锁文件本体持锁）在场时，清理者绝不删除该锁；释放后才算孤儿。
def test_live_holder_lock_is_not_removed(tmp_path: Path) -> None:
    lock_path = _orphan_lock(tmp_path)
    handle = os.fdopen(_open_private_lock_descriptor(lock_path), "a+", encoding="utf-8")
    _flock_exclusive(handle)
    try:
        assert cleanup_orphan_lock_files(tmp_path) == 0
        assert lock_path.exists()
    finally:
        handle.close()
    assert cleanup_orphan_lock_files(tmp_path) == 1


# 函数用途: Windows 语义下持锁 unlink 失败（文件被打开）时放弃本次删除，不回退成先 close 再删。
def test_unlink_failure_keeps_orphan_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lock_path = _orphan_lock(tmp_path)
    real_unlink = Path.unlink

    def failing_unlink(self, *args, **kwargs):
        if str(self) == str(lock_path):
            raise PermissionError("file is open")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", failing_unlink)
    assert cleanup_orphan_lock_files(tmp_path) == 0
    assert lock_path.exists()


# 函数用途: 清理者持锁 unlink 后，阻塞迟到者必须经身份核对重试、与新来者互斥——任何时刻最多一方在临界区。
def test_cleanup_and_two_lockers_never_overlap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lock_path = _orphan_lock(tmp_path)
    data_path = tmp_path / "x.json"
    guard = threading.Lock()
    counters = {"active": 0, "max": 0}

    def critical() -> None:
        with guard:
            counters["active"] += 1
            counters["max"] = max(counters["max"], counters["active"])
        time.sleep(0.4)
        with guard:
            counters["active"] -= 1

    def latecomer() -> None:
        with gateway_locked_file_path(data_path):
            critical()

    threads: list[threading.Thread] = []
    real_unlink = Path.unlink

    def delayed_unlink(self, *args, **kwargs):
        if str(self) == str(lock_path) and not threads:
            thread = threading.Thread(target=latecomer)
            thread.start()
            threads.append(thread)
            time.sleep(0.3)  # 迟到者已打开旧 inode 并阻塞在 flock 上
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", delayed_unlink)
    try:
        assert cleanup_orphan_lock_files(tmp_path) == 1
    finally:
        monkeypatch.undo()

    with gateway_locked_file_path(data_path):  # 新来者
        critical()
    for thread in threads:
        thread.join()
    assert counters["max"] == 1


# 函数用途: 锁文件被反复替换时，加锁方在有限次核对后按锁竞争失败，不无限重试。
def test_lock_identity_retry_limit_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import agent_py_agent.agent.gateway_parts.io as gateway_io

    data_path = tmp_path / "x.json"
    calls = {"count": 0}
    real_flock = gateway_io._flock_exclusive

    def swapping_flock(handle) -> None:
        real_flock(handle)
        calls["count"] += 1
        lock_path = data_path.with_name(data_path.name + ".lock")
        lock_path.unlink()
        lock_path.write_text("", encoding="utf-8")

    monkeypatch.setattr(gateway_io, "_flock_exclusive", swapping_flock)
    with pytest.raises(BlockingIOError):
        with gateway_locked_file_path(data_path):
            pass
    assert calls["count"] == _LOCK_IDENTITY_RETRY_COUNT


# 函数用途: common/json_io 的加锁路径同样核对身份：锁文件被替换一次后重试并正常进入临界区。
def test_json_io_lock_retries_when_lock_file_replaced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import agent_py_agent.agent.common.json_io as json_io

    data_path = tmp_path / "data.json"
    data_path.write_text("{}", encoding="utf-8")
    calls = {"count": 0}
    real_flock = json_io._flock_descriptor

    def swapping_flock(descriptor: int, *, blocking: bool = True) -> None:
        real_flock(descriptor, blocking=blocking)
        calls["count"] += 1
        if calls["count"] == 1:
            lock_path = data_path.with_name(data_path.name + ".lock")
            lock_path.unlink()
            lock_path.write_text("", encoding="utf-8")

    monkeypatch.setattr(json_io, "_flock_descriptor", swapping_flock)
    with json_io.locked_json_path(data_path):
        pass
    assert calls["count"] == 2
