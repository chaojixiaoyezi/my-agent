"""三套锁写法统一成私有：B（common/json_io._locked_file_path、io/jsonl._locked_text_file）与
C（_DispatchWatchLock）都经 open_private_lock_beneath 拿 fd，文件 0600、不跟随符号链接；
缺失目录由原语按 0700 新建，已存在的目录（含经符号链接进入的）权限一律不动——锁只动自己建的东西；
已存在的 0644 锁在下次打开时无条件收紧到 0600（自愈存量，不做全盘扫描）。

be 2026-10-03 只读评估：B 两处用 Path.open("a+")+mkdir 跟随 umask，真实 home 里本项目空锁 2158 个 0644，
同机他用户能打开并 flock(LOCK_EX) 卡住宿主写入。umask 固定 0o022 跑，钉“不靠 umask”。
全部用 pytest tmp_path 和测试替身，不碰真实 home，不调真实模型。
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from agent_py_agent.agent.agent_core.orchestration.dispatch.lock import _DispatchWatchLock
from agent_py_agent.agent.common.json_io import locked_json_path
from agent_py_agent.agent.common.nofollow_fs import open_private_lock_beneath_tightened
from agent_py_agent.agent.gateway_parts.daemon_metadata import _flocked_sidecar
from agent_py_agent.agent.io.jsonl import append_line_locked

_POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="POSIX 权限位语义")


# 函数用途: 固定 umask 0o022（生产常见值），验证私有权限不依赖 umask；用例结束恢复原值。
@pytest.fixture
def default_umask():
    previous = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(previous)


# 函数用途: 读一个路径的权限位。
def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@_POSIX_ONLY
def test_locked_json_path_lock_is_private(tmp_path, default_umask):
    target = tmp_path / "state" / "desktop.json"
    with locked_json_path(target):
        lock = target.with_name(target.name + ".lock")
        assert (_mode(lock), _mode(lock.parent)) == (0o600, 0o700), "json 锁必须私有，且不靠 umask"
        assert lock.read_bytes() == b"", "只 flock，不写内容"


@_POSIX_ONLY
def test_locked_text_file_lock_is_private(tmp_path, default_umask):
    target = tmp_path / "index" / "active_tasks.jsonl"
    append_line_locked(target, '{"a": 1}')
    lock = target.with_name(target.name + ".lock")
    assert _mode(lock) == 0o600, "jsonl 锁必须私有，且不靠 umask"
    assert lock.read_bytes() == b"", "只 flock，不写内容"
    # pdp 2026-10-03：append_line_locked 的缺失目录按 0700 新建（不再跟随 umask）；锁只动自己新建的东西。
    assert _mode(lock.parent) == 0o700, "jsonl 数据目录按 0700 新建，锁不替调用方收紧"


@_POSIX_ONLY
def test_dispatch_watch_lock_and_its_directory_are_private(tmp_path, default_umask):
    lock_path = tmp_path / "subagents" / "subagent_dispatch_watch.lock"
    with _DispatchWatchLock(lock_path):
        assert (_mode(lock_path), _mode(lock_path.parent)) == (0o600, 0o700), "派工锁与其父目录都必须私有"


@_POSIX_ONLY
def test_existing_world_readable_locks_are_tightened_on_next_acquire(tmp_path, default_umask):
    json_target = tmp_path / "state" / "desktop.json"
    json_lock = json_target.with_name(json_target.name + ".lock")
    json_lock.parent.mkdir(parents=True, exist_ok=True)
    json_lock.write_text("", encoding="utf-8")
    os.chmod(json_lock, 0o644)  # 生产旧状态
    os.chmod(json_lock.parent, 0o755)

    text_target = tmp_path / "index" / "active_tasks.jsonl"
    text_lock = text_target.with_name(text_target.name + ".lock")
    text_lock.parent.mkdir(parents=True, exist_ok=True)
    text_lock.write_text("", encoding="utf-8")
    os.chmod(text_lock, 0o644)
    os.chmod(text_lock.parent, 0o755)

    dispatch_lock = tmp_path / "subagents" / "dispatch.lock"
    dispatch_lock.parent.mkdir(parents=True, exist_ok=True)
    dispatch_lock.write_text(json.dumps({"schema_version": "dispatch-watch-lock.v2"}), encoding="utf-8")
    os.chmod(dispatch_lock, 0o644)
    os.chmod(dispatch_lock.parent, 0o755)

    with locked_json_path(json_target):
        pass
    append_line_locked(text_target, '{"a": 1}')
    with _DispatchWatchLock(dispatch_lock):
        pass

    locks = (json_lock, text_lock, dispatch_lock)
    assert {_mode(path) for path in locks} == {0o600}, "存量 0644 锁下次用到时必须收紧"
    assert {_mode(path.parent) for path in locks} == {0o755}, "已存在的锁目录不归锁管，权限一律不动"


@_POSIX_ONLY
def test_lock_leaf_is_not_followed_through_a_symlink(tmp_path, default_umask):
    outside = tmp_path / "outside.lock"
    outside.write_text("", encoding="utf-8")
    target = tmp_path / "state" / "desktop.json"
    lock = target.with_name(target.name + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.symlink_to(outside)

    with pytest.raises(OSError):
        with locked_json_path(target):
            pass

    assert outside.stat().st_size == 0 and not outside.is_symlink(), "符号链接锁必须被拒绝，且不跟随改写目标"


@_POSIX_ONLY
def test_daemon_sidecar_write_lock_is_private(tmp_path, default_umask):
    """daemon_metadata 的 .wlock 是第四处旧写法（lock_path.open("a+")），同批收私。"""
    sidecar = tmp_path / "run" / "gateway_state.json"
    with _flocked_sidecar(sidecar):
        wlock = sidecar.with_name(sidecar.name + ".wlock")
        assert (_mode(wlock), _mode(wlock.parent)) == (0o600, 0o700), "写锁 sidecar 与其目录都必须私有"


@_POSIX_ONLY
def test_existing_world_readable_daemon_wlock_is_tightened(tmp_path, default_umask):
    sidecar = tmp_path / "run" / "gateway_state.json"
    wlock = sidecar.with_name(sidecar.name + ".wlock")
    wlock.parent.mkdir(parents=True, exist_ok=True)
    wlock.write_text("", encoding="utf-8")
    os.chmod(wlock, 0o644)  # 生产旧状态
    os.chmod(wlock.parent, 0o755)

    with _flocked_sidecar(sidecar):
        pass

    assert _mode(wlock) == 0o600, "存量 0644 wlock 下次用到时必须收紧"
    assert _mode(wlock.parent) == 0o755, "已存在的锁目录不归锁管，权限一律不动"


@_POSIX_ONLY
def test_daemon_wlock_leaf_is_not_followed_through_a_symlink(tmp_path, default_umask):
    sidecar = tmp_path / "run" / "gateway_state.json"
    wlock = sidecar.with_name(sidecar.name + ".wlock")
    wlock.parent.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside.wlock"
    outside.write_text("", encoding="utf-8")
    wlock.symlink_to(outside)

    with pytest.raises(OSError):
        with _flocked_sidecar(sidecar):
            pass

    assert outside.stat().st_size == 0, "符号链接写锁必须被拒绝，且不跟随改写目标"


@_POSIX_ONLY
def test_dispatch_watch_lock_still_publishes_readable_metadata(tmp_path, default_umask):
    lock_path = tmp_path / "subagents" / "dispatch.lock"
    with _DispatchWatchLock(lock_path) as lock:
        payload = json.loads(lock_path.read_text(encoding="utf-8"))

    assert payload["schema_version"] == "dispatch-watch-lock.v2"
    assert payload["token"] == lock.token
    assert payload["pid"] == os.getpid(), "元数据照旧写在同一个 fd 上，权限仍是 0600"
    assert _mode(lock_path) == 0o600


@_POSIX_ONLY
def test_non_blocking_lock_reports_contention_without_waiting(tmp_path, default_umask):
    target = tmp_path / "state" / "desktop.json"
    with locked_json_path(target):
        assert _non_blocking_acquire_fails(target), "blocking=False 竞争时必须立即失败，不能进去"

    with locked_json_path(target, blocking=False) as _held:
        assert True, "锁空出来后 blocking=False 仍应成功"


# 函数用途: 在别人持锁时用 blocking=False 再取一次，返回它是否如约立即失败。
def _non_blocking_acquire_fails(target: Path) -> bool:
    try:
        with locked_json_path(target, blocking=False):
            return False
    except OSError:
        return True


@_POSIX_ONLY
def test_lock_beneath_symlinked_ancestor_keeps_existing_dir_modes(tmp_path, default_umask):
    """等价用例（step17i 上下文快照口径）：已存在的、经符号链接进入的目录里拿锁，目录权限必须不变。

    快照的私有写入经 json_io 锁落盘；owner 把某级目录做成符号链接（归档根/日期父级）时，
    锁只许新建自己的锁文件，不许收紧链接目标里已存在的目录（期望 0755 就保持 0755）。"""
    real_root = tmp_path / "real-state"
    state = real_root / "state"
    state.mkdir(parents=True)
    os.chmod(real_root, 0o755)
    os.chmod(state, 0o755)
    link_root = tmp_path / "linked-state"
    os.symlink(real_root, link_root, target_is_directory=True)

    with locked_json_path(link_root / "state" / "desktop.json"):
        pass

    lock = state / "desktop.json.lock"
    assert lock.is_file()
    assert _mode(lock) == 0o600, "锁文件本身仍必须是私有的"
    assert (_mode(real_root), _mode(state)) == (0o755, 0o755), "经符号链接进入的已存在目录，权限一律不动"
    assert link_root.is_symlink(), "符号链接本身也不许被替换"


@_POSIX_ONLY
def test_primitive_leaves_existing_dir_mode_untouched(tmp_path, default_umask):
    """原语级：已存在的 0750 目录里拿锁，只有锁文件是 0600，目录权限一位都不许动。"""
    state = tmp_path / "state"
    state.mkdir()
    os.chmod(state, 0o750)

    descriptor = open_private_lock_beneath_tightened(tmp_path, ("state", "desktop.json.lock"))
    os.close(descriptor)

    assert _mode(state) == 0o750, "已存在的目录一律不改权限"
    assert _mode(state / "desktop.json.lock") == 0o600, "锁文件本身按 0600 建"
