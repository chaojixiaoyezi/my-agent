# LLM: GW-11 回归：非排他私有锁先按原 no-follow/单链接/前后身份校验打开已存在的锁，仅缺失时排他创建，
#   并发新建撞 EEXIST 时再打开复用同一 inode；排他模式与链接拒绝语义不变。
# 模块用途: 核复用不再先失败一次排他创建、缺失新建 0600、竞态复用、排他互斥与符号链接拒绝。
from __future__ import annotations

import fcntl
import os
import stat

import pytest

from agent_py_agent.agent.common import nofollow_fs
from agent_py_agent.agent.common.nofollow_fs import NoFollowPathError, open_private_lock_beneath

pytestmark = pytest.mark.skipif(not nofollow_fs._supports_dir_fd(), reason="dir_fd 路径专用")


@pytest.fixture()
def opens(monkeypatch):
    calls = []
    original = nofollow_fs._openat_file

    def recording(parent_fd, name, flags, mode):
        calls.append(bool(flags & os.O_CREAT))
        return original(parent_fd, name, flags, mode)

    monkeypatch.setattr(nofollow_fs, "_openat_file", recording)
    return calls


def test_existing_lock_is_reused_without_exclusive_create(tmp_path, opens):
    first = open_private_lock_beneath(tmp_path, ("locks", "a.lock"))
    assert opens == [False, True]  # 缺失：先打开得 ENOENT，再排他创建
    assert stat.S_IMODE(os.fstat(first).st_mode) == 0o600
    opens.clear()
    second = open_private_lock_beneath(tmp_path, ("locks", "a.lock"))
    try:
        assert opens == [False]
        assert os.fstat(first).st_ino == os.fstat(second).st_ino
        fcntl.flock(first, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            fcntl.flock(second, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(first)
        os.close(second)


def test_concurrent_creation_race_reopens_same_inode(tmp_path, monkeypatch):
    (tmp_path / "locks").mkdir(mode=0o700)
    original = nofollow_fs._openat_file
    created = []

    def racing(parent_fd, name, flags, mode):
        if flags & os.O_CREAT and not created:
            # 我们看到 ENOENT 之后、排他创建之前，另一进程抢先建好了锁。
            created.append(os.open(tmp_path / "locks" / name, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600))
        return original(parent_fd, name, flags, mode)

    monkeypatch.setattr(nofollow_fs, "_openat_file", racing)
    descriptor = open_private_lock_beneath(tmp_path, ("locks", "b.lock"))
    try:
        assert os.fstat(descriptor).st_ino == os.fstat(created[0]).st_ino
    finally:
        os.close(descriptor)
        os.close(created[0])


def test_exclusive_mode_and_link_rejection_unchanged(tmp_path, opens):
    descriptor = open_private_lock_beneath(tmp_path, ("locks", "c.lock"), exclusive=True)
    os.close(descriptor)
    assert opens == [True]
    opens.clear()
    with pytest.raises(FileExistsError):
        open_private_lock_beneath(tmp_path, ("locks", "c.lock"), exclusive=True)
    assert opens == [True]
    os.symlink(tmp_path / "elsewhere", tmp_path / "locks" / "d.lock")
    with pytest.raises(NoFollowPathError):
        open_private_lock_beneath(tmp_path, ("locks", "d.lock"))
    os.link(tmp_path / "locks" / "c.lock", tmp_path / "locks" / "e.lock")
    with pytest.raises(NoFollowPathError):
        open_private_lock_beneath(tmp_path, ("locks", "e.lock"))
