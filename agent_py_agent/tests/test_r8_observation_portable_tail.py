"""Windows显式portable尾窗复用原父链/FD快照；strict能力不足仍拒绝。"""
import json
import os
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.common import nofollow_fs as fs
from agent_py_agent.agent.conversation import store_observations as observations


@pytest.fixture
def portable(monkeypatch):
    monkeypatch.setattr(fs, '_supports_dir_fd', lambda: False)
    monkeypatch.setattr(observations, 'os', SimpleNamespace(**{**vars(os), 'name': 'nt'}))


def test_portable_tail_reads_only_the_bounded_window(tmp_path, monkeypatch, portable):
    path = tmp_path / 'events.jsonl'
    path.write_bytes(b''.join(json.dumps({'observation_id': str(i), 'thread_id': 't'}).encode() + b'\n' for i in range(10000)))
    original, sizes = os.read, []

    def read(fd, size):
        raw = original(fd, size)
        sizes.append(len(raw))
        return raw

    monkeypatch.setattr(fs.os, 'read', read)
    monkeypatch.setattr(observations, 'open_readonly_file_beneath', lambda *_a: pytest.fail('portable window called strict-only opening'))
    rows, errors, cut, used, missing = observations._observation_tail_window(path, 1024, 16)
    assert len(rows) == 16 and rows[-1]['observation_id'] == '9999'
    assert not errors and cut and not missing
    assert used == sum(sizes) == 1024
    assert path.stat().st_size > used * 100


@pytest.mark.parametrize('change', ['inplace', 'replace', 'parent', 'unknown', 'permission', 'hardlink'])
def test_portable_tail_rejects_unverified_source(tmp_path, monkeypatch, portable, change):
    parent = tmp_path / 'parent'
    parent.mkdir()
    path = parent / 'events.jsonl'
    raw = b'{"observation_id":"o","thread_id":"t"}\n'
    path.write_bytes(raw)
    if change == 'hardlink':
        os.link(path, parent / 'alias')
    elif change == 'unknown':
        monkeypatch.setattr(fs, 'file_descriptor_change_ns', lambda *_a: None)
    elif change == 'permission':
        original = fs.os.open

        def denied(target, *args, **kwargs):
            if os.fspath(target) == os.fspath(path):
                raise PermissionError('synthetic denied')
            return original(target, *args, **kwargs)

        monkeypatch.setattr(fs.os, 'open', denied)
    else:
        original = fs._read_tail_descriptor

        def changed(fd, max_bytes):
            result = original(fd, max_bytes)
            if change == 'inplace':
                with path.open('ab') as stream:
                    stream.write(raw)
            elif change == 'replace':
                replacement = parent / 'new'
                replacement.write_bytes(raw)
                replacement.replace(path)
            else:
                parent.rename(tmp_path / 'old')
                parent.mkdir()
                path.write_bytes(raw)
            return result

        monkeypatch.setattr(fs, '_read_tail_descriptor', changed)
    rows, errors, _cut, _used, missing = observations._observation_tail_window(path, 1024, 16)
    assert rows == [] and errors and not missing and _used == 1024


def test_portable_tail_missing_and_strict_unavailable_remain_distinct(tmp_path, portable):
    path = tmp_path / 'absent.jsonl'
    assert observations._observation_tail_window(path, 1024, 16) == ([], [], False, 0, True)
    path.write_bytes(b'{}\n')
    with pytest.raises(fs.NoFollowPathError):
        fs.read_tail_snapshot_beneath(tmp_path, (path.name,), max_bytes=1024, require_dir_fd=True)


@pytest.mark.parametrize('limit', [0, 17, 4096])
def test_native_tail_snapshot_keeps_exact_window(tmp_path, limit):
    path = tmp_path / 'native.jsonl'
    raw = b'0123456789' * 1000
    path.write_bytes(raw)
    snapshot = fs.read_tail_snapshot_beneath(tmp_path, (path.name,), max_bytes=limit)
    assert snapshot.content == (raw[-limit:] if limit else b'')
    assert snapshot.stat.st_size == len(raw) and snapshot.changed_ns is not None
