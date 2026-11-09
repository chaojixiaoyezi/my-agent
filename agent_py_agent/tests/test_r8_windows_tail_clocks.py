"""Windows路径创建时钟不能与FD变更时钟混比；不牺牲完整性与EOF核验。"""
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.common.nofollow_fs import file_descriptor_change_ns
from agent_py_agent.agent.conversation import message_cursor as cursor
from agent_py_agent.agent.conversation import store_messages as messages
from agent_py_agent.agent.conversation import store_observations as observations
from agent_py_agent.agent.io.cursor_cache import CursorCache


@pytest.fixture
def clocks(tmp_path, monkeypatch):
    paths = [tmp_path / name for name in ('messages.jsonl', 'observations.jsonl', 'display.jsonl')]
    paths[0].write_text(json.dumps({'message_id': 'm', 'thread_id': 't', 'role': 'user'}) + '\n')
    paths[1].write_text(json.dumps({'observation_id': 'o', 'thread_id': 't'}) + '\n')
    paths[2].write_text(''.join(json.dumps({'message_id': f'd-{i}', 'thread_id': 't', 'role': 'display'}) + '\n' for i in range(1000)))
    for path in paths:
        old = time.time() - 10
        os.utime(path, (old, old))
    time.sleep(2.05)
    real_stat = Path.stat

    class Creation:
        def __init__(self, info):
            self.info = info

        def __getattr__(self, field):
            return self.info.st_ctime_ns - 100_000_000_000 if field == 'st_ctime_ns' else getattr(self.info, field)

    def path_clock(path, *args, **kwargs):
        info = real_stat(path, *args, **kwargs)
        return Creation(info) if path in paths else info

    def actual_fd(path):
        # 模拟common跨时钟接口边界；返回实际FD身份，不伪造FD或变更代次。
        with path.open('rb') as stream:
            info = os.fstat(stream.fileno())
            return info, file_descriptor_change_ns(stream.fileno(), info)

    monkeypatch.setattr(Path, 'stat', path_clock)
    for module in (cursor, messages, observations):
        monkeypatch.setattr(module, 'file_identity_snapshot', actual_fd, raising=False)
    monkeypatch.setattr(cursor, 'os', SimpleNamespace(**{**vars(os), 'name': 'nt'}))
    monkeypatch.setattr(cursor, '_MESSAGE_CURSORS', CursorCache(128))
    return paths


def test_tail_marker_accepts_same_fd_clock_and_preserves_full_count(clocks):
    assert messages.canonical_message_tail_marker(clocks[0], 't')['message_count'] == 1


def test_observation_tail_accepts_same_fd_clock_without_false_partial(clocks):
    rows, errors, cut, consumed, missing = observations._observation_tail_window(clocks[1], 4096, 16)
    assert len(rows) == 1 and not errors and not cut and not missing and consumed > 0


def test_display_only_eof_reuse_does_not_reparse_old_prefix(clocks, monkeypatch):
    original = json.loads
    parsed = []

    def counted(*args, **kwargs):
        row = original(*args, **kwargs)
        if isinstance(row, dict) and row.get('role') == 'display':
            parsed.append(1)
        return row

    monkeypatch.setattr(json, 'loads', counted)
    assert cursor.read_after_rows(clocks[2], '', 20) == ([], [])
    assert len(parsed) == 1000
    assert cursor.read_after_rows(clocks[2], '', 20) == ([], [])
    assert len(parsed) == 1000


def test_eof_unknown_change_never_claims_no_new_data(clocks, monkeypatch):
    assert cursor.read_after_rows(clocks[0], '', 20)[0]
    assert cursor.read_after_rows(clocks[0], 'm', 20) == ([], [])
    original = cursor.file_identity_snapshot
    calls = []

    def unknown(path):
        info, _changed = original(path)
        calls.append(True)
        return info, None

    monkeypatch.setattr(cursor, 'file_identity_snapshot', unknown)
    assert not cursor._unchanged_end(clocks[0], 'm') and calls


def test_posix_eof_uses_no_additional_file_open(tmp_path, monkeypatch):
    path = tmp_path / 'empty.jsonl'
    path.write_bytes(b'')
    old = time.time() - 10
    os.utime(path, (old, old))
    monkeypatch.setattr(cursor, 'os', SimpleNamespace(**{**vars(os), 'name': 'posix'}))
    monkeypatch.setattr(cursor, '_MESSAGE_CURSORS', CursorCache(128))
    assert cursor.read_after_rows(path, '', 20) == ([], [])
    monkeypatch.setattr(cursor, 'file_identity_snapshot', lambda *_args: pytest.fail('POSIX added file open'), raising=False)
    assert cursor.read_after_rows(path, '', 20) == ([], [])


@pytest.mark.parametrize('operation', ['replace', 'inplace', 'missing', 'permission'])
@pytest.mark.parametrize('consumer', ['marker', 'observations'])
def test_tail_identity_failure_never_reports_complete(tmp_path, monkeypatch, operation, consumer):
    from agent_py_agent.agent.common.nofollow_fs import file_identity_snapshot
    from agent_py_agent.agent.runtime_errors import DataCorruptionError

    path = tmp_path / 'rows.jsonl'
    raw = json.dumps({'message_id': 'm', 'thread_id': 't', 'observation_id': 'o'}) + '\n'
    path.write_text(raw)
    module = messages if consumer == 'marker' else observations

    def changed(source):
        info, native = file_identity_snapshot(source)
        if operation == 'replace':
            other = source.with_suffix('.new')
            other.write_text(raw)
            other.replace(source)
            return file_identity_snapshot(source)
        if operation == 'inplace':
            # 核路径后修改同一个FD所指文件，末次FD检查仍必须识别。
            with source.open('ab') as stream:
                stream.write(raw.encode())
            return info, native
        if operation == 'missing':
            source.unlink()
            return file_identity_snapshot(source)
        raise PermissionError('synthetic denied')

    monkeypatch.setattr(module, 'file_identity_snapshot', changed)
    if consumer == 'marker':
        with pytest.raises((DataCorruptionError, OSError)):
            messages.canonical_message_tail_marker(path, 't')
    else:
        _rows, errors, _cut, _used, missing = observations._observation_tail_window(path, 4096, 16)
        assert errors and not missing


def test_real_native_tail_readers_and_eof(tmp_path, monkeypatch):
    path = tmp_path / 'native.jsonl'
    path.write_text(json.dumps({'message_id': 'm', 'thread_id': 't', 'observation_id': 'o'}) + '\n')
    old = time.time() - 10
    os.utime(path, (old, old))
    time.sleep(2.05)
    monkeypatch.setattr(cursor, '_MESSAGE_CURSORS', CursorCache(128))
    assert messages.canonical_message_tail_marker(path, 't')['message_count'] == 1
    assert observations._observation_tail_window(path, 4096, 16)[1] == []
    assert cursor.read_after_rows(path, '', 20)[0]
    assert cursor.read_after_rows(path, 'm', 20) == ([], [])
    assert cursor._unchanged_end(path, 'm')
    with path.open('a') as stream:
        stream.write(json.dumps({'message_id': 'n', 'thread_id': 't'}) + '\n')
    assert not cursor._unchanged_end(path, 'm')
    assert [row['message_id'] for row in cursor.read_after_rows(path, 'm', 20)[0]] == ['n']
