# ── scoped lock 私有权限（sclk）────────────────────────────────────────────
# 锁目录/锁文件的权限必须由统一私有原语保证（新目录 0700、新文件 0600），不能跟着 umask 走：
# 之前裸 os.open(O_CREAT|O_EXCL) 在 umask 022 下会建出 0644 的世界可读锁文件。
# 已存在的目录一律不改权限（不动别处建的布局），O_EXCL 的"已存在即失败"语义必须保留。

from __future__ import annotations

import json
import os

import pytest


# 函数用途: 每个用例一个临时锁目录，与真实 XDG 状态目录完全隔离。初始不存在，交给被测代码创建。
@pytest.fixture
def tmp_lock_dir(tmp_path):
    return tmp_path / "locks"


# 函数用途: 在隔离的锁目录下，断言新建锁目录 0700、锁文件 0600。
def test_scoped_lock_creates_private_dir_and_file(tmp_lock_dir, monkeypatch):
    from agent_py_agent.agent.gateway_parts import scoped_locks

    # umask 022 下裸创建会得到 0644/0755；这里显式设一个宽松 umask 证明权限来自显式 mode。
    monkeypatch.setattr(scoped_locks, "_get_lock_dir", lambda: tmp_lock_dir)
    previous = os.umask(0o022)
    try:
        acquired, existing = scoped_locks.acquire_scoped_lock("perm-scope", "perm-identity")
    finally:
        os.umask(previous)

    assert acquired is True and existing is None
    lock_path = tmp_lock_dir / f"perm-scope-{scoped_locks._scope_hash('perm-identity')}.lock"
    assert lock_path.exists()
    assert os.stat(tmp_lock_dir).st_mode & 0o777 == 0o700, "锁目录必须 0700"
    assert os.stat(lock_path).st_mode & 0o777 == 0o600, "锁文件必须 0600"


# 函数用途: 已存在的锁目录权限保持不变（不因建锁被收紧或放宽）。
def test_scoped_lock_leaves_existing_directory_mode_untouched(tmp_lock_dir, monkeypatch):
    from agent_py_agent.agent.gateway_parts import scoped_locks

    tmp_lock_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(tmp_lock_dir, 0o750)
    monkeypatch.setattr(scoped_locks, "_get_lock_dir", lambda: tmp_lock_dir)
    acquired, _ = scoped_locks.acquire_scoped_lock("existing-dir", "identity")
    assert acquired is True
    assert os.stat(tmp_lock_dir).st_mode & 0o777 == 0o750, "已存在的目录权限不能被改"


# 函数用途: 锁文件已存在时第二次创建失败（O_EXCL 先到先得语义保留）。
def test_scoped_lock_file_creation_is_exclusive(tmp_lock_dir, monkeypatch):
    from agent_py_agent.agent.gateway_parts import scoped_locks

    monkeypatch.setattr(scoped_locks, "_get_lock_dir", lambda: tmp_lock_dir)
    tmp_lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = tmp_lock_dir / "excl-scope.lock"
    record = {"pid": os.getpid(), "start_time": 1, "scope": "excl-scope", "identity_hash": "x", "metadata": {}, "updated_at": "t"}
    assert scoped_locks._create_lock_file(lock_path, record) is True
    assert scoped_locks._create_lock_file(lock_path, record) is False, "已存在必须返回 False，不能覆盖或打开"
    # 竞态下第二个调用者也不能改掉已存在文件的内容。
    assert json.loads(lock_path.read_text(encoding="utf-8"))["scope"] == "excl-scope"


# 函数用途: 状态目录两级都不存在（全新机器、自定义 XDG_STATE_HOME）时仍能建锁，且每一级都是 0700。
def test_scoped_lock_creates_every_missing_level_with_0700(tmp_path, monkeypatch):
    from agent_py_agent.agent.gateway_parts import scoped_locks

    # sclk2：原来从 lock_path.parent.parent 反推 root，原语第一步就 lstat 这个 root；
    # 自定义 XDG_STATE_HOME 下 my-agent 与 locks 两级都可能不存在，基线能建出来、sclk 会抛 FileNotFoundError。
    state_home = tmp_path / "xdg-state"
    lock_dir = state_home / "my-agent" / "locks"
    monkeypatch.setattr(scoped_locks, "_get_lock_dir", lambda: lock_dir)
    previous = os.umask(0o022)
    try:
        acquired, existing = scoped_locks.acquire_scoped_lock("fresh-scope", "fresh-identity")
    finally:
        os.umask(previous)

    assert acquired is True and existing is None
    lock_path = lock_dir / f"fresh-scope-{scoped_locks._scope_hash('fresh-identity')}.lock"
    assert lock_path.exists()
    assert (
        os.stat(lock_dir).st_mode & 0o777,
        os.stat(lock_dir.parent).st_mode & 0o777,
        os.stat(state_home).st_mode & 0o777,
    ) == (0o700, 0o700, 0o700), "缺失的每一级目录都必须是 0700"
    assert os.stat(lock_path).st_mode & 0o777 == 0o600


# 函数用途: root 从 _get_lock_dir 出发而不是从锁路径反推——锁路径多一层时 root 不会跟着上移。
def test_scoped_lock_root_follows_lock_dir_not_parent_parent(tmp_path, monkeypatch):
    from agent_py_agent.agent.gateway_parts import scoped_locks

    # 锁目录自己再嵌一层（模拟以后锁文件挪进子目录）：只有"从 _get_lock_dir 出发"才能建对位置。
    lock_dir = tmp_path / "nested" / "locks"
    monkeypatch.setattr(scoped_locks, "_get_lock_dir", lambda: lock_dir)
    nested_lock_path = lock_dir / "sub" / "deep-scope.lock"
    record = {"pid": os.getpid(), "start_time": 1, "scope": "deep-scope", "identity_hash": "x",
              "metadata": {}, "updated_at": "t"}

    assert scoped_locks._create_lock_file(nested_lock_path, record) is True
    assert nested_lock_path.exists()
    assert os.stat(nested_lock_path.parent).st_mode & 0o777 == 0o700
    assert os.stat(nested_lock_path).st_mode & 0o777 == 0o600


# 函数用途: 强制 portable 分支（无 dir_fd）下独占语义、私有权限与复用行为都与 POSIX 分支一致。
@pytest.mark.parametrize("nested", [False, True])
def test_portable_branch_keeps_exclusive_semantics_and_private_modes(tmp_path, monkeypatch, nested):
    from agent_py_agent.agent.common import nofollow_fs
    from agent_py_agent.agent.gateway_parts import scoped_locks

    monkeypatch.setattr(nofollow_fs, "_supports_dir_fd", lambda: False)
    lock_dir = tmp_path / "portable" / "locks"
    lock_path = (lock_dir / "sub" / "portable.lock") if nested else (lock_dir / "portable.lock")
    monkeypatch.setattr(scoped_locks, "_get_lock_dir", lambda: lock_dir)
    record = {"pid": os.getpid(), "start_time": 1, "scope": "portable", "identity_hash": "x",
              "metadata": {}, "updated_at": "t"}

    assert scoped_locks._create_lock_file(lock_path, record) is True
    assert os.stat(lock_path).st_mode & 0o777 == 0o600, "portable 分支新建锁也必须 0600"
    assert os.stat(lock_path.parent).st_mode & 0o777 == 0o700
    if nested:
        assert os.stat(lock_dir).st_mode & 0o777 == 0o700

    # ds1 的 M5：去掉 portable 分支的独占检查后，第二次调用会落进"已存在就打开"分支（返回 True），
    # 这条断言必须抓住它。
    assert scoped_locks._create_lock_file(lock_path, record) is False, "portable 分支同样必须独占"

    # 默认（非 exclusive）模式仍可复用已存在的锁，portable 分支也要保住这个语义。
    anchor, parts = nofollow_fs.split_existing_anchor(lock_path.parent)
    descriptor = nofollow_fs.open_private_lock_beneath(anchor, (*parts, lock_path.name))
    os.close(descriptor)
