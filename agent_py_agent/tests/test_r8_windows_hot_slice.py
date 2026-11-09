"""热消息末尾核验只比较同源时钟，仍拒绝身份、权限及真实变更。"""
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation import message_cursor_slice as slices
from agent_py_agent.agent.memory_store.retention_scan_budget import RetentionScanBudget


@pytest.fixture(scope='module')
def hot_source(tmp_path_factory):
    path = tmp_path_factory.mktemp('hot-slice-clocks') / 'messages.jsonl'
    path.write_bytes(b''.join((json.dumps({'message_id': f'msg-{i}', 'role': 'user',
        'content': 'synthetic', 'thread_id': 'thread'}) + '\n').encode() for i in range(20)))
    old = time.time() - 10
    os.utime(path, (old, old))
    time.sleep(2.05)  # 真实准备稳定年龄，不扩大被测25ms预算或伪造FD变更代次。
    return path


def _budget():
    return RetentionScanBudget(4, 4096, time.monotonic() + .025)


def test_distinct_path_creation_clock_does_not_discard_stable_fd_progress(hot_source, monkeypatch):
    original = Path.stat

    class CreationClock:
        def __init__(self, real):
            self.real = real

        def __getattr__(self, name):
            if name == 'st_ctime_ns':
                return self.real.st_ctime_ns - 100_000_000_000
            return getattr(self.real, name)

    def path_clock(path, *args, **kwargs):
        value = original(path, *args, **kwargs)
        # 模拟旧调用方path.stat的另一时钟；规范helper的内部快照仍取真实同源身份。
        return CreationClock(value) if path == hot_source and kwargs.get('follow_symlinks', True) else value

    monkeypatch.setattr(Path, 'stat', path_clock)
    first = slices.read_after_slice(hot_source, 'msg-9', 1, _budget())
    assert first.partial and first.continuation is not None
    assert first.continuation.phase == 'locate' and first.continuation.offset > 0
    for _ in range(10):
        result = slices.read_after_slice(hot_source, 'msg-9', 1, _budget())
        if result.rows:
            break
    assert not result.load_errors and result.rows[0]['message_id'] == 'msg-10'


@pytest.mark.parametrize('changed', ['st_dev', 'st_ino', 'st_mode', 'st_nlink', 'st_size',
                                   'st_mtime_ns', 'st_ctime_ns', 'native_change'])
def test_path_snapshot_mismatch_keeps_all_rows_and_coverage_unpublished(hot_source, monkeypatch, changed):
    original = slices.file_identity_snapshot

    def mismatch(path):
        info, native = original(path)
        if changed == 'native_change':
            return info, native + 1
        values = {name: getattr(info, name) for name in (
            'st_dev', 'st_ino', 'st_mode', 'st_nlink', 'st_size', 'st_mtime_ns', 'st_ctime_ns')}
        values[changed] += 1
        return SimpleNamespace(**values), native

    monkeypatch.setattr(slices, 'file_identity_snapshot', mismatch)
    result = slices.read_after_slice(hot_source, '', 1, _budget())
    assert result.partial and result.reason == 'source_changed'
    assert not result.rows and result.continuation is None


def test_unavailable_native_change_remains_unknown(hot_source, monkeypatch):
    monkeypatch.setattr(slices, 'file_descriptor_change_ns', lambda *_args: None)
    result = slices.read_after_slice(hot_source, '', 1, _budget())
    assert result.partial and result.reason == 'source_identity_unavailable'
    assert not result.rows and result.continuation is None


def test_write_after_path_snapshot_is_still_seen_by_final_native_probe(hot_source, monkeypatch):
    original = slices.file_identity_snapshot

    def rewrite_after_snapshot(path):
        captured = original(path)
        before = path.stat()
        path.write_bytes(path.read_bytes().replace(b'synthetic', b'Synthetic', 1))
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        return captured

    monkeypatch.setattr(slices, 'file_identity_snapshot', rewrite_after_snapshot)
    result = slices.read_after_slice(hot_source, '', 1, _budget())
    assert result.partial and result.reason == 'source_changed'
    assert not result.rows and result.continuation is None
