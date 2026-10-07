"""宿主二进制文件原语；只操作临时文件，不代替插件进程或 TUI 验收。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agent_py_agent.agent.common import nofollow_fs as fs


def test_strict_source_does_not_downgrade_without_dirfd(tmp_path, monkeypatch):
    path = tmp_path / "source"
    path.write_bytes(b"original")
    monkeypatch.setattr(fs, "_supports_dir_fd", lambda: False)
    with pytest.raises(fs.NoFollowPathError):
        fs.read_bytes_beneath(tmp_path, ("source",), require_dir_fd=True)
    assert fs.read_bytes_beneath(tmp_path, ("source",)) == b"original"


def test_fifo_swap_during_open_is_nonblocking_and_rejected(tmp_path, monkeypatch):
    path = tmp_path / "source"
    path.write_bytes(b"original")
    original = os.open

    def swap(name, flags, mode=0o777, **kwargs):
        if name == "source":
            assert flags & os.O_NONBLOCK  # 缺标记立即失败，测试本身不能挂在 FIFO 上。
            path.unlink()
            os.mkfifo(path)
        return original(name, flags, mode, **kwargs)

    monkeypatch.setattr(fs, "_supports_dir_fd", lambda: True)
    monkeypatch.setattr(os, "open", swap)
    with pytest.raises(fs.NoFollowPathError):
        fs.read_bytes_beneath(tmp_path, ("source",), require_dir_fd=True)


@pytest.mark.parametrize("portable", [False, True])
def test_binary_and_text_share_private_atomic_writer(tmp_path, monkeypatch, portable):
    if portable:
        monkeypatch.setattr(fs, "_supports_dir_fd", lambda: False)
    parts = ("private", "binary.dat")
    content = b"\x00\xff\x80\x00binary"
    fs.write_bytes_atomic_beneath(tmp_path, parts, content)
    path = tmp_path.joinpath(*parts)
    assert path.stat().st_mode & 0o777 == 0o600
    assert fs.read_bytes_beneath(tmp_path, parts, max_bytes=len(content)) == content
    with pytest.raises(ValueError, match="read limit"):
        fs.read_bytes_beneath(tmp_path, parts, max_bytes=len(content) - 1)
    fs.write_text_atomic_beneath(tmp_path, parts, "中文\n")
    assert fs.read_text_beneath(tmp_path, parts) == "中文\n"
    assert fs.read_bytes_beneath(tmp_path, ("missing", "file"), max_bytes=10) is None
    assert not (tmp_path / "missing").exists()
    assert list(path.parent.iterdir()) == [path]


@pytest.mark.parametrize("portable", [False, True])
def test_failed_replace_preserves_original_and_cleans_private_temporary(
    tmp_path, monkeypatch, portable
):
    if portable:
        monkeypatch.setattr(fs, "_supports_dir_fd", lambda: False)
    path = tmp_path / "record"
    fs.write_bytes_atomic_beneath(tmp_path, ("record",), b"before")

    def fail_replace(*args, **kwargs):
        raise OSError("injected replace failure")

    monkeypatch.setattr(os, "replace" if portable else "rename", fail_replace)
    if not portable:
        monkeypatch.setattr(fs, "_supports_dir_fd", lambda: True)
    with pytest.raises(OSError):
        fs.write_bytes_atomic_beneath(tmp_path, ("record",), b"after")
    assert path.read_bytes() == b"before"
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("portable", [False, True])
def test_links_and_nonregular_sources_are_refused(tmp_path, monkeypatch, portable):
    if portable:
        monkeypatch.setattr(fs, "_supports_dir_fd", lambda: False)
    outside = tmp_path / "outside"
    outside.write_bytes(b"unchanged")
    (tmp_path / "link").symlink_to(outside)
    with pytest.raises(fs.NoFollowPathError):
        fs.read_bytes_beneath(tmp_path, ("link",), max_bytes=100)
    with pytest.raises(fs.NoFollowPathError):
        fs.write_bytes_atomic_beneath(tmp_path, ("link",), b"bad")
    assert outside.read_bytes() == b"unchanged"
    (tmp_path / "directory").mkdir()
    with pytest.raises(fs.NoFollowPathError):
        fs.read_bytes_beneath(tmp_path, ("directory",))
    (tmp_path / "dangling").symlink_to(tmp_path / "missing")
    with pytest.raises(fs.NoFollowPathError):
        fs.read_bytes_beneath(tmp_path, ("dangling",))


@pytest.mark.parametrize("portable", [False, True])
def test_private_lock_rejects_parent_symlink(tmp_path, monkeypatch, portable):
    if portable:
        monkeypatch.setattr(fs, "_supports_dir_fd", lambda: False)
    target = tmp_path / "target"
    target.mkdir()
    (tmp_path / "link").symlink_to(target, target_is_directory=True)
    with pytest.raises(fs.NoFollowPathError):
        fs.open_private_lock_beneath(tmp_path, ("link", "new", ".lock"))
    assert not list(target.iterdir())


@pytest.mark.parametrize("portable", [False, True])
@pytest.mark.parametrize("kind", ["symlink", "dangling", "hardlink", "directory"])
def test_private_lock_refuses_unsafe_leaf(tmp_path, monkeypatch, portable, kind):
    if portable:
        monkeypatch.setattr(fs, "_supports_dir_fd", lambda: False)
    outside = tmp_path / "outside"
    leaf = tmp_path / ".lock"
    if kind != "dangling":
        outside.write_bytes(b"unchanged")
    if kind in {"symlink", "dangling"}:
        leaf.symlink_to(outside)
    elif kind == "hardlink":
        os.link(outside, leaf)
    else:
        leaf.mkdir()
    with pytest.raises(fs.NoFollowPathError):
        fs.open_private_lock_beneath(tmp_path, (".lock",))
    if kind == "dangling":
        assert not outside.exists()
    else:
        assert outside.read_bytes() == b"unchanged"


@pytest.mark.parametrize("portable", [False, True])
def test_private_lock_preserves_inode_and_bytes(tmp_path, monkeypatch, portable):
    if portable:
        monkeypatch.setattr(fs, "_supports_dir_fd", lambda: False)
    parts = ("private", ".lock")
    descriptor = fs.open_private_lock_beneath(tmp_path, parts)
    try:
        os.write(descriptor, b"original")
        identity = os.fstat(descriptor).st_ino
    finally:
        os.close(descriptor)
    descriptor = fs.open_private_lock_beneath(tmp_path, parts)
    try:
        assert os.fstat(descriptor).st_ino == identity
        assert os.read(descriptor, 20) == b"original"
        assert os.fstat(descriptor).st_mode & 0o777 == 0o600
    finally:
        os.close(descriptor)


@pytest.mark.parametrize("value", [True, -1, "128"])
def test_invalid_read_budget_rejected_before_open(tmp_path, value):
    with pytest.raises(ValueError):
        fs.read_bytes_beneath(tmp_path, ("missing",), max_bytes=value)


@pytest.mark.parametrize("created_link", [False, True])
def test_portable_directory_creation_race_rechecks_existing_identity(
    tmp_path, monkeypatch, created_link
):
    monkeypatch.setattr(fs, "_supports_dir_fd", lambda: False)
    parent = tmp_path / "private"
    outside = tmp_path / "outside"
    outside.mkdir()
    original = Path.mkdir

    def create_before_caller(path, *args, **kwargs):
        if path == parent:
            if created_link:
                path.symlink_to(outside, target_is_directory=True)
            else:
                original(path, *args, **kwargs)
            raise FileExistsError("another creator won")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", create_before_caller)
    if created_link:
        with pytest.raises(fs.NoFollowPathError):
            fs.open_private_lock_beneath(tmp_path, ("private", ".lock"))
    else:
        descriptor = fs.open_private_lock_beneath(tmp_path, ("private", ".lock"))
        os.close(descriptor)
        assert (parent / ".lock").is_file()
    assert not list(outside.iterdir())


def test_private_dir_without_dirfd_uses_the_portable_path(tmp_path, monkeypatch):
    # Windows 上 os.open 打不开目录：不支持 dir_fd 时 ensure_private_dir 不得打开目录描述符，逐级新建、已存在的不动；
    # 与 dir_fd 那条路同一规则：最近已存在祖先是指向目录的链接时跟随一次，是普通文件就拒绝（CI 修复，2026-10-07）。
    monkeypatch.setattr(fs, "_supports_dir_fd", lambda: False)

    def no_directory_descriptor(*_args, **_kwargs):
        raise AssertionError("不支持 dir_fd 的平台不能打开目录描述符")

    monkeypatch.setattr(fs, "open_directory_beneath", no_directory_descriptor)
    fs.ensure_private_dir(tmp_path / "a" / "b" / "c")
    assert (tmp_path / "a" / "b" / "c").is_dir()
    fs.ensure_private_dir(tmp_path / "a" / "b" / "c")  # 已存在：什么都不做
    (tmp_path / "real").mkdir()
    (tmp_path / "a" / "link").symlink_to(tmp_path / "real", target_is_directory=True)
    fs.ensure_private_dir(tmp_path / "a" / "link" / "new" / "deeper")
    assert (tmp_path / "real" / "new" / "deeper").is_dir()
    (tmp_path / "a" / "plain").write_text("x", encoding="utf-8")
    with pytest.raises(fs.NoFollowPathError):
        fs.ensure_private_dir(tmp_path / "a" / "plain" / "new")
