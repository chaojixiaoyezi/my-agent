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


# 函数用途: 在跨进程锁日志里追加一行 enter/exit 与进程号（模块级，spawn 子进程可导入）。
def _append_lock_log(log_path_str: str, event: str, pid: int) -> None:
    with open(log_path_str, "a", encoding="utf-8") as fh:
        fh.write(f"{event} {pid}\n")


# 函数用途: 多进程加锁循环体（spawn 子进程入口）：反复取锁，在临界区写 enter/exit 日志。
def _locker_process(data_path_str: str, log_path_str: str, rounds: int) -> None:
    import os as _os
    import time as _time

    from agent_py_agent.agent.gateway_parts.io import _locked_file_path as _lock_path

    data_path = Path(data_path_str)
    for _ in range(rounds):
        with _lock_path(data_path):
            _append_lock_log(log_path_str, "enter", _os.getpid())
            _time.sleep(0.02)
            _append_lock_log(log_path_str, "exit", _os.getpid())


# 函数用途: 常驻跨进程互斥证明：3 个独立进程真实竞争同一把锁，任何时刻最多一方在临界区。
def test_cross_process_lockers_never_overlap(tmp_path: Path) -> None:
    import multiprocessing

    data_path = tmp_path / "x.json"
    log_path = tmp_path / "lock.log"
    ctx = multiprocessing.get_context("spawn")
    procs = [
        ctx.Process(target=_locker_process, args=(str(data_path), str(log_path), 20))
        for _ in range(3)
    ]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join(timeout=90)
    assert [proc.exitcode for proc in procs] == [0, 0, 0]
    active = 0
    max_active = 0
    for line in log_path.read_text(encoding="utf-8").splitlines():
        if line.startswith("enter"):
            active += 1
            max_active = max(max_active, active)
        else:
            active -= 1
    assert active == 0
    assert max_active == 1, "跨进程 flock 必须保证任何时刻最多一方在临界区"


# 函数用途: 非阻塞探测遇到锁文件身份不匹配时按“没拿到”返回（fail-closed），不给出任何授权。
def test_try_lock_identity_mismatch_returns_not_acquired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import agent_py_agent.agent.gateway_parts.io as gateway_io
    from agent_py_agent.agent.gateway_parts.io import try_locked_file_transition

    monkeypatch.setattr(gateway_io, "_lock_handle_matches_path", lambda _handle, _path: False)
    with try_locked_file_transition(tmp_path / "x.json") as acquired:
        assert acquired is False


# 函数用途: 锁身份核对重试耗尽一路到 input_delivery：按可重试的锁失败（BlockingIOError）处理，不是数据损坏。
def test_lock_exhaustion_reaches_input_delivery_as_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json as _json

    import agent_py_agent.agent.gateway_parts.io as gateway_io
    from agent_py_agent.agent.gateway_parts import input_delivery_service as ids
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root

    paths = gateway_paths_from_root(tmp_path / "gateway")
    paths.inbox.mkdir(parents=True, exist_ok=True)
    request_id = "gwreq-lockbusy"
    (paths.inbox / f"{request_id}.json").write_text(_json.dumps({"id": request_id}), encoding="utf-8")
    prepared = {"id": request_id, "request_id": request_id, "kind": "ask", "goal": "hi",
                "user_id": "local", "metadata": {"client_input_digest": "d"}}

    with ids.gateway_input_transition(paths, request_id):
        ids.load_or_prepare_gateway_input_locked(
            paths, request_id=request_id, client_input_digest="d", client_message_id="m",
            guidance_dedupe_key="k", prepared_request=prepared)

    real_matches = gateway_io._lock_handle_matches_path

    def flaky_matches(handle, lock_path):
        if str(lock_path).endswith(f"{request_id}.json.lock"):
            return False
        return real_matches(handle, lock_path)

    monkeypatch.setattr(gateway_io, "_lock_handle_matches_path", flaky_matches)
    with pytest.raises(BlockingIOError):
        with ids.gateway_input_transition(paths, request_id):
            ids.load_or_prepare_gateway_input_locked(
                paths, request_id=request_id, client_input_digest="d", client_message_id="m",
                guidance_dedupe_key="k", prepared_request=prepared)

    monkeypatch.undo()
    with ids.gateway_input_transition(paths, request_id):
        ids.load_or_prepare_gateway_input_locked(
            paths, request_id=request_id, client_input_digest="d", client_message_id="m",
            guidance_dedupe_key="k", prepared_request=prepared)


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
