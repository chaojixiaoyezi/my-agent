"""SUB-65：真实封存后删除定位器，读口和维护口只能从已提交分段恢复。"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from agent_py_agent.agent.common.json_io import locked_json_path
from agent_py_agent.agent.gateway_parts import owner_retention as archive
from agent_py_agent.agent.gateway_parts.io import read_json_file_report, write_json_file_atomic
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.gateway_parts.recovery import (
    _terminal_gateway_request_payload,
    mark_gateway_terminal_projection_complete,
)
from agent_py_agent.agent.runtime_errors import DataCorruptionError


def _rows(index):
    with sqlite3.connect(index.absolute().as_uri() + '?mode=ro', uri=True) as connection:
        return connection.execute('SELECT * FROM receipts ORDER BY path').fetchall()


def _damage(index, damage='missing'):
    for suffix in ('', '-wal', '-shm', '-journal'):
        Path(str(index) + suffix).unlink(missing_ok=True)
    if damage == 'empty':
        index.touch(mode=0o600)
    elif damage == 'no_schema':
        with sqlite3.connect(index) as connection:
            connection.execute('CREATE TABLE unrelated(value TEXT)')


def _cold_bytes(paths):
    return {path.name: path.read_bytes() for path in (paths.root / 'archive').iterdir()
            if path.name == 'receipts-manifest.json' or path.name.endswith('.jsonl')}


@pytest.fixture
def sealed(tmp_path):
    paths = gateway_paths_from_root(tmp_path / 'gateway')
    originals = {}
    # 两轮真实封存产生两个段，避免只恢复 manifest.last_segment 也能侥幸过测。
    for key in ('first', 'second'):
        response = {'id': key, 'ok': True, 'status': 'done', 'ended_at': 1,
                    'output': '原始答复\u0085\u2028，不能重写'}
        payload = _terminal_gateway_request_payload(
            key, {'id': key, 'user_id': 'alice', 'prompt': 'hello'}, response,
        )
        for folder, body in ((paths.terminal, payload), (paths.done, payload),
                             (paths.responses, response)):
            write_json_file_atomic(folder / f'{key}.json', body)
        mark_gateway_terminal_projection_complete(paths, key, response)
        (paths.done / f'{key}.chunks.jsonl').write_bytes(b'{"event":"delta","text":"answer"}\n')
        originals.update({str(path.relative_to(paths.root)): path.read_bytes()
                          for folder in (paths.terminal, paths.done, paths.responses,
                                         paths.root / 'projection_state' / 'terminal_complete')
                          for path in folder.iterdir() if path.name.startswith(key) and not path.name.endswith('.lock')})
        result = archive.seal_gateway_receipts_slice(paths, before=100, max_ms=1000)
        assert result['sealed_requests'] == 1 and not result['errors'], result
    index = paths.root / 'archive' / 'receipts-index.sqlite3'
    return paths, index, originals, _rows(index), _cold_bytes(paths)


@pytest.mark.parametrize('damage', ['missing', 'empty', 'no_schema'])
def test_reader_rebuilds_exact_locator_once_and_preserves_cold_bytes(sealed, damage):
    paths, index, originals, rows, cold = sealed
    _damage(index, damage)

    # 原在途 response 不在归档里；它也必须能先修索引再正常返回“尚无答复”。
    pending = read_json_file_report(paths.responses / 'inflight.json')
    assert pending.payload == {} and pending.load_error is None and not pending.lock_busy
    rebuilt = index.stat()
    assert _rows(index) == rows
    assert rebuilt.st_mode & 0o777 == 0o600
    for relative, expected in originals.items():
        assert archive.lookup_sealed_receipt(paths, relative) == expected
        assert not (paths.root / relative).exists()
    missing = read_json_file_report(paths.terminal / 'never-existed.json')
    assert missing.payload == {} and missing.load_error is None and not missing.lock_busy
    assert index.stat() == rebuilt
    assert _cold_bytes(paths) == cold


@pytest.mark.parametrize('damage', ['missing', 'empty', 'no_schema'])
def test_maintenance_rebuilds_even_without_new_terminal_candidates(sealed, damage):
    paths, index, _originals, rows, cold = sealed
    _damage(index, damage)

    result = archive.seal_gateway_receipts_slice(paths, before=100, max_ms=1000)

    assert not result['errors'] and result['archived_requests'] == 0, result
    assert index.is_file(), 'an empty maintenance pass must rebuild the locator'
    assert _rows(index) == rows and _cold_bytes(paths) == cold


def test_reader_losing_rebuild_election_reports_retryable_lock_busy(sealed):
    paths, index, _originals, rows, _cold = sealed
    _damage(index)
    with locked_json_path(paths.root / 'archive' / 'receipts-maintenance'):
        report = read_json_file_report(paths.responses / 'inflight.json')
        assert report.lock_busy and report.load_error is not None and report.payload == {}
        with pytest.raises(BlockingIOError):
            archive.lookup_sealed_receipt(paths, 'requests/terminal/first.json')
        assert not index.exists()
    assert not read_json_file_report(paths.responses / 'inflight.json').lock_busy
    assert _rows(index) == rows


@pytest.mark.parametrize('sequence', [1, 2])
@pytest.mark.parametrize('reader', [True, False])
def test_missing_manifest_segment_fails_closed_without_publishing(sealed, sequence, reader):
    paths, index, _originals, _rows_before, _cold = sealed
    _damage(index)
    (index.parent / f'receipts-seg-{sequence:012d}.jsonl').unlink()
    cold = _cold_bytes(paths)

    with pytest.raises(DataCorruptionError):
        if reader:
            archive.lookup_sealed_receipt(paths, 'requests/terminal/never-existed.json')
        else:
            archive.seal_gateway_receipts_slice(paths, before=100, max_ms=1000)

    assert not index.exists() and _cold_bytes(paths) == cold
    assert not list(index.parent.glob('.receipts-index.sqlite3.rebuild-*'))


def test_rebuild_does_not_scan_uncommitted_orphan_segment(sealed):
    paths, index, _originals, rows, _cold = sealed
    _damage(index)
    (index.parent / 'receipts-seg-999999999999.jsonl').write_bytes(b'not committed JSON\n')
    assert archive.lookup_sealed_receipt(paths, 'requests/terminal/first.json') is not None
    assert _rows(index) == rows


def test_rebuild_publishes_only_complete_database_and_cleans_failed_stage(sealed, monkeypatch):
    paths, index, originals, rows, cold = sealed
    _damage(index)
    replace = os.replace
    publications = []

    def fail_publication(source, destination, *args, **kwargs):
        if Path(destination) == index:
            assert not index.exists()
            assert _rows(Path(source)) == rows
            publications.append(source)
            raise OSError('injected locator publication failure')
        return replace(source, destination, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(archive.os, 'replace', fail_publication)
        with pytest.raises(OSError, match='injected locator publication failure'):
            archive.lookup_sealed_receipt(paths, 'requests/terminal/first.json')
    assert len(publications) == 1 and not index.exists()
    assert not list(index.parent.glob('.receipts-index.sqlite3.rebuild-*'))
    assert _cold_bytes(paths) == cold
    for relative, expected in originals.items():
        assert archive.lookup_sealed_receipt(paths, relative) == expected


def test_healthy_locator_reads_without_electing_a_rebuilder(sealed):
    paths, index, originals, _rows_before, cold = sealed
    before = index.stat()
    with locked_json_path(paths.root / 'archive' / 'receipts-maintenance'):
        for relative, expected in originals.items():
            assert archive.lookup_sealed_receipt(paths, relative) == expected
    assert index.stat() == before and _cold_bytes(paths) == cold
