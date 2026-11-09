"""同FD字节快照与父链核验；便携节点可在Windows实际执行。"""
from __future__ import annotations

import os
import time
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
    # 首个样本必须越过原两秒粗时钟保护窗；同 tick 改写本就不能取得缓存资格。
    time.sleep(2.05)
    original = fs.read_bytes_snapshot_beneath(tmp_path, ('leaf',), max_bytes=100)
    from agent_py_agent.agent.conversation.message_cursor_slice import source_stamp_is_trustworthy
    from agent_py_agent.agent.io.cursor_cache import file_stamp
    assert source_stamp_is_trustworthy(file_stamp(original.stat), original.changed_ns)
    path.write_bytes(b'new bytes')
    os.utime(path, ns=(original.stat.st_atime_ns, original.stat.st_mtime_ns))
    current = fs.inspect_file_snapshot_beneath(tmp_path, ('leaf',))
    assert current.stat.st_mtime_ns == original.stat.st_mtime_ns
    assert current.changed_ns is not None and current.changed_ns != original.changed_ns
    assert not source_stamp_is_trustworthy(file_stamp(current.stat), current.changed_ns)
    actual = fs.read_bytes_snapshot_beneath(tmp_path, ('leaf',), max_bytes=100)
    assert original.content == b'old bytes' and actual.content == b'new bytes'
    assert (actual.stat.st_dev, actual.stat.st_ino, actual.changed_ns) == (
        current.stat.st_dev, current.stat.st_ino, current.changed_ns,
    )


def _path_with_creation_ctime(path, *, mutate=None):
    """模拟Windows原生路径ctime=创建时刻；FD仍由真实临时文件提供。"""
    from types import SimpleNamespace
    fields = ('st_dev', 'st_ino', 'st_mode', 'st_nlink', 'st_size', 'st_mtime_ns', 'st_ctime_ns')
    class Target:
        calls = 0
        def __fspath__(self):
            return os.fspath(path)
        def lstat(self):
            self.calls += 1
            info = path.lstat()
            values = {key: getattr(info, key) for key in fields}
            values['st_ctime_ns'] = 1  # 与同一文件真实FD的ChangeTime刻意不同。
            if mutate is not None and self.calls > 1:
                mutate(values)
            return SimpleNamespace(**values)
    return Target()


def test_windows_identity_uses_fd_clock_without_forging_stat(tmp_path):
    path = tmp_path / 'leaf'
    path.write_bytes(b'first\r\n\x1asecond')
    actual, changed = fs._windows_file_identity_snapshot(_path_with_creation_ctime(path))
    assert actual.st_ino == path.stat().st_ino
    assert actual.st_ctime_ns != 1 and changed is not None and changed > 0


@pytest.mark.parametrize('field', ['st_dev', 'st_ino', 'st_mode', 'st_nlink', 'st_size', 'st_mtime_ns', 'st_ctime_ns'])
def test_windows_identity_keeps_every_path_generation_gate(tmp_path, field):
    path = tmp_path / 'leaf'
    path.write_bytes(b'unchanged')
    target = _path_with_creation_ctime(path, mutate=lambda values: values.__setitem__(field, values[field] + 1))
    with pytest.raises(fs.NoFollowPathError):
        fs._windows_file_identity_snapshot(target)


@pytest.mark.parametrize('changes', [(None, None), (1, 2)])
def test_windows_identity_unknown_or_changing_generation_closes_handle(tmp_path, monkeypatch, changes):
    path = tmp_path / 'leaf'
    path.write_bytes(b'unchanged')
    observed = []
    original_open = os.open
    def capture(*args, **kwargs):
        fd = original_open(*args, **kwargs)
        observed.append(fd)
        return fd
    monkeypatch.setattr(fs.os, 'open', capture)
    samples = iter(changes)
    monkeypatch.setattr(fs, 'file_descriptor_change_ns', lambda fd, info: next(samples))
    with pytest.raises(fs.NoFollowPathError):
        fs._windows_file_identity_snapshot(_path_with_creation_ctime(path))
    assert len(observed) == 1
    with pytest.raises(OSError):
        os.fstat(observed[0])
