"""同FD字节快照与父链核验；便携节点可在Windows实际执行。"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from agent_py_agent.agent.common import nofollow_fs as fs


@pytest.mark.parametrize('portable', [False, True])
def test_binary_snapshot_preserves_bytes_and_captured_fd_identity(tmp_path, monkeypatch, portable):
    root = tmp_path / 'root'
    (root / 'parent').mkdir(parents=True)
    target = root / 'parent' / 'leaf'
    raw = b'first\r\n\x1asecond\x00\xff\n'
    target.write_bytes(raw)
    if portable:
        monkeypatch.setattr(fs, '_supports_dir_fd', lambda: False)
    descriptors = []
    read = fs._read_descriptor
    def observe(fd, maximum):
        descriptors.append((fd, os.fstat(fd)))
        return read(fd, maximum)
    monkeypatch.setattr(fs, '_read_descriptor', observe)
    snapshot = fs.read_bytes_snapshot_beneath(root, ('parent', 'leaf'), max_bytes=len(raw))
    fields = ('st_dev', 'st_ino', 'st_mode', 'st_nlink', 'st_size', 'st_mtime_ns', 'st_ctime_ns')
    assert snapshot.content == raw
    assert tuple(getattr(snapshot.stat, name) for name in fields) == tuple(getattr(descriptors[-1][1], name) for name in fields)
    assert snapshot.stat.st_size == len(raw)
    assert snapshot.parents and snapshot.changed_ns is not None
    for descriptor, _info in descriptors:
        with pytest.raises(OSError):
            os.fstat(descriptor)
    metadata = fs.inspect_file_snapshot_beneath(root, ('parent', 'leaf'))
    assert metadata.content == b'' and metadata.stat == snapshot.stat
    assert metadata.parents == snapshot.parents and metadata.changed_ns == snapshot.changed_ns


@pytest.mark.skipif(os.name == 'nt', reason='Windows不允许rename仍打开的文件；其字节/FD合同由通用节点实际验证')
@pytest.mark.parametrize('portable', [False, True])
@pytest.mark.parametrize('replacement', ['leaf', 'parent'])
def test_real_replacement_during_read_never_returns_snapshot(tmp_path, monkeypatch, portable, replacement):
    root = tmp_path / 'root'
    parent = root / 'parent'
    parent.mkdir(parents=True)
    target = parent / 'leaf'
    target.write_bytes(b'old')
    if portable:
        monkeypatch.setattr(fs, '_supports_dir_fd', lambda: False)
    original = fs._read_descriptor
    descriptors = []
    def change(fd, maximum):
        raw = original(fd, maximum)
        descriptors.append(fd)
        if replacement == 'leaf':
            newer = parent / 'new'
            newer.write_bytes(b'new')
            os.replace(newer, target)
        else:
            parent.rename(root / 'retired')
            parent.mkdir()
            target.write_bytes(b'new')
        return raw
    monkeypatch.setattr(fs, '_read_descriptor', change)
    with pytest.raises(fs.NoFollowPathError):
        fs.read_bytes_snapshot_beneath(root, ('parent', 'leaf'), max_bytes=10)
    for descriptor in descriptors:
        with pytest.raises(OSError):
            os.fstat(descriptor)


def test_portable_strict_capability_does_not_silently_fall_back(tmp_path, monkeypatch):
    monkeypatch.setattr(fs, '_supports_dir_fd', lambda: False)
    with pytest.raises(fs.NoFollowPathError):
        fs.read_bytes_snapshot_beneath(tmp_path, ('leaf',), max_bytes=100, require_dir_fd=True)


@pytest.mark.parametrize('portable', [False, True])
def test_snapshot_limit_and_missing_do_not_forge_an_identity(tmp_path, monkeypatch, portable):
    (tmp_path / 'leaf').write_bytes(b'abcdef')
    if portable:
        monkeypatch.setattr(fs, '_supports_dir_fd', lambda: False)
    with pytest.raises(ValueError):
        fs.read_bytes_snapshot_beneath(tmp_path, ('leaf',), max_bytes=2)
    assert fs.read_bytes_snapshot_beneath(tmp_path, ('missing',), max_bytes=2) is None


@pytest.mark.parametrize('portable', [False, True])
def test_backdated_mtime_still_changes_actual_descriptor_generation(tmp_path, monkeypatch, portable):
    if portable:
        monkeypatch.setattr(fs, '_supports_dir_fd', lambda: False)
    path = tmp_path / 'leaf'
    path.write_bytes(b'old bytes')
    original = fs.read_bytes_snapshot_beneath(tmp_path, ('leaf',), max_bytes=100)
    path.write_bytes(b'new bytes')
    os.utime(path, ns=(original.stat.st_atime_ns, original.stat.st_mtime_ns))
    current = fs.inspect_file_snapshot_beneath(tmp_path, ('leaf',))
    assert current.stat.st_mtime_ns == original.stat.st_mtime_ns
    assert current.changed_ns is not None and current.changed_ns != original.changed_ns
