"""RES-W15: recover a deleted Gateway runtime-file locator from published cold copies."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
from pathlib import Path
from unittest.mock import Mock

import pytest

from agent_py_agent.agent.gateway_parts import input_delivery_service as inputs
from agent_py_agent.agent.gateway_parts import runtime_file_retention as retention
from agent_py_agent.agent.gateway_parts.io import write_json_file_atomic
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.runtime_errors import DataCorruptionError


def _seed(paths, key='input-a'):
    prepared = {
        'id': key,
        'kind': 'ask',
        'goal': 'original complete request',
        'user_id': 'user-a',
        'conversation': {'channel_conversation_id': 'conversation-a'},
        'metadata': {'client_input_digest': 'digest-' + key, 'channel': 'chat'},
    }
    row = inputs.GatewayInputReceipt(
        key, 'digest-' + key, 'message-' + key, 'consumed', prepared,
        inputs._prepared_payload_digest(prepared), 'guidance-' + key, updated_at=1,
    )
    write_json_file_atomic(inputs.gateway_input_receipt_path(paths, key), row.to_dict())
    return row


def _outbox(paths, key, *, updated_at=1):
    path = paths.root / 'client_outbox' / f'{key}.json'
    write_json_file_atomic(path, {
        'schema_version': 'tui_active_input_outbox.v3',
        'entries': {},
        'updated_at': updated_at,
    })
    return path


def _archive(paths, directory):
    reports = []
    for _ in range(100):
        report = retention.archive_terminal_slice(
            paths, directory=directory, before=100, max_entries=1, max_ms=1000,
        )
        reports.append(report)
        if report['complete']:
            return reports
    pytest.fail('bounded retention failed to finish')


def _prepare(paths, row, callback=None):
    return inputs.load_or_prepare_gateway_input_locked(
        paths,
        request_id=row.request_id,
        client_input_digest=row.client_input_digest,
        client_message_id=row.client_message_id,
        guidance_dedupe_key=row.guidance_dedupe_key,
        prepared_request=row.prepared_request,
        prepare_request=callback,
    )


def _admit(paths, key):
    prepared = {
        'id': key,
        'kind': 'ask',
        'goal': 'new request',
        'user_id': 'user-a',
        'conversation': {'channel_conversation_id': 'conversation-a'},
        'metadata': {'client_input_digest': 'digest-' + key, 'channel': 'chat'},
    }
    return inputs.load_or_prepare_gateway_input_locked(
        paths,
        request_id=key,
        client_input_digest='digest-' + key,
        client_message_id='message-' + key,
        guidance_dedupe_key='guidance-' + key,
        prepared_request=prepared,
    )


def _remove_family(path):
    for suffix in ('', '-wal', '-shm', '-journal'):
        candidate = Path(str(path) + suffix)
        candidate.unlink(missing_ok=True)


def _index_rows(path):
    with sqlite3.connect(path) as connection:
        return connection.execute(
            'SELECT directory,name,month,digest FROM files ORDER BY directory,name',
        ).fetchall()


def _rebuild_temporary_files(paths):
    archive = paths.root / 'archive'
    return [item for item in archive.iterdir() if item.name.startswith('.runtime-files.sqlite3.rebuild-')]


@pytest.fixture
def runtime_index_link(monkeypatch, tmp_path):
    """Use a real hard link when supported; emulate link publication in restricted test runners."""
    source, destination = tmp_path / 'link-source', tmp_path / 'link-destination'
    source.write_bytes(b'probe')
    real_link = os.link
    try:
        real_link(source, destination)
    except PermissionError:
        def publish(source_path, destination_path):
            destination_path = Path(destination_path)
            if destination_path.exists():
                raise FileExistsError(destination_path)
            destination_path.write_bytes(Path(source_path).read_bytes())
    else:
        destination.unlink()

        def publish(source_path, destination_path):
            real_link(source_path, destination_path)
    source.unlink()
    calls = []

    def recording_link(source_path, destination_path):
        calls.append((source_path, destination_path))
        return publish(source_path, destination_path)

    monkeypatch.setattr(retention.os, 'link', recording_link)
    return calls


def test_missing_index_allows_new_requests_without_read_side_repair(tmp_path):
    paths = gateway_paths_from_root(tmp_path / 'gateway')
    _outbox(paths, 'published')
    assert not any(report['errors'] for report in _archive(paths, 'client_outbox'))
    index = paths.root / 'archive/runtime-files.sqlite3'
    _remove_family(index)

    for key in ('new-a', 'new-b', 'new-c'):
        row, created = _admit(paths, key)
        assert created and row.request_id == key and row.state == 'pending'

    assert not index.exists()
    assert not _rebuild_temporary_files(paths)


def test_missing_index_reads_consumed_input_and_archived_outbox(tmp_path):
    paths = gateway_paths_from_root(tmp_path / 'gateway')
    row = _seed(paths)
    _outbox(paths, 'published')
    assert not any(report['errors'] for report in _archive(paths, 'input_receipts'))
    assert not any(report['errors'] for report in _archive(paths, 'client_outbox'))
    hot = inputs.gateway_input_receipt_path(paths, row.request_id)
    hot.unlink()
    index = paths.root / 'archive/runtime-files.sqlite3'
    _remove_family(index)
    callback = Mock(side_effect=AssertionError('must not prepare a consumed input'))

    reused, created = _prepare(paths, row, callback)

    assert reused == row and not created
    callback.assert_not_called()
    assert retention.lookup_runtime_file(paths, 'client_outbox', 'published.json') == (
        paths.root / 'archive/client_outbox/197001/published.json'
    )
    assert not hot.exists() and not index.exists()


def test_next_writer_rebuilds_exact_rows_then_archives_new_entry(tmp_path, caplog, runtime_index_link):
    paths = gateway_paths_from_root(tmp_path / 'gateway')
    _seed(paths)
    _outbox(paths, 'published')
    assert not any(report['errors'] for report in _archive(paths, 'input_receipts'))
    assert not any(report['errors'] for report in _archive(paths, 'client_outbox'))
    index = paths.root / 'archive/runtime-files.sqlite3'
    before_rows = _index_rows(index)
    _remove_family(index)
    newer = _outbox(paths, 'newer', updated_at=2)
    newer_bytes = newer.read_bytes()

    with caplog.at_level(logging.WARNING, logger=retention.__name__):
        reports = _archive(paths, 'client_outbox')

    assert not any(report['errors'] for report in reports), reports
    rebuilt = [record for record in caplog.records if 'GATEWAY_ARCHIVE_INDEX_REBUILT' in record.message]
    assert (len(rebuilt) == 1 and f'root={paths.root}' in rebuilt[0].message
            and f'rows={len(before_rows)}' in rebuilt[0].message)
    after_rows = _index_rows(index)
    assert [row for row in after_rows if row[1] != 'newer.json'] == before_rows
    assert ('client_outbox', 'newer.json', '197001', hashlib.sha256(newer_bytes).hexdigest()) in after_rows
    assert len(runtime_index_link) == 1
    assert not newer.exists()


def test_publication_marker_without_cold_directory_still_fails_closed(tmp_path):
    paths = gateway_paths_from_root(tmp_path / 'gateway')
    _outbox(paths, 'published')
    _archive(paths, 'client_outbox')
    cold = paths.root / 'archive/client_outbox/197001/published.json'
    cold.unlink()
    cold.parent.rmdir()
    cold.parent.parent.rmdir()
    _remove_family(paths.root / 'archive/runtime-files.sqlite3')

    with pytest.raises(DataCorruptionError, match='runtime_archive_index_missing'):
        retention.lookup_runtime_file(paths, 'input_receipts', 'unseen.json')


def test_changed_cold_copy_after_rebuild_fails_digest_check(tmp_path, runtime_index_link):
    paths = gateway_paths_from_root(tmp_path / 'gateway')
    _outbox(paths, 'published')
    _archive(paths, 'client_outbox')
    index = paths.root / 'archive/runtime-files.sqlite3'
    _remove_family(index)
    _outbox(paths, 'newer', updated_at=2)
    assert not any(report['errors'] for report in _archive(paths, 'client_outbox'))
    (paths.root / 'archive/client_outbox/197001/published.json').write_bytes(b'changed')

    with pytest.raises(DataCorruptionError, match='runtime_archive_bytes_missing_or_changed'):
        retention.lookup_runtime_file(paths, 'client_outbox', 'published.json')
    assert len(runtime_index_link) == 1


@pytest.mark.parametrize(
    ('stage', 'message'),
    [
        ('archive', 'runtime_archive_symlink'),
        ('index', 'runtime_archive_index_symlink'),
        ('marker', 'runtime_archive_marker_symlink'),
        ('input_receipts', 'runtime_archive_symlink'),
        ('client_outbox', 'runtime_archive_symlink'),
    ],
)
def test_runtime_archive_symlinks_still_fail_closed(tmp_path, stage, message):
    paths = gateway_paths_from_root(tmp_path / 'gateway')
    paths.root.mkdir(parents=True, exist_ok=True)
    archive = paths.root / 'archive'
    target = tmp_path / f'target-{stage}'
    if stage == 'archive':
        target.mkdir()
        archive.symlink_to(target, target_is_directory=True)
    else:
        archive.mkdir()
        name = {
            'index': 'runtime-files.sqlite3',
            'marker': 'runtime-files-published.json',
        }.get(stage, stage)
        if stage in {'input_receipts', 'client_outbox'}:
            target.mkdir()
            (archive / 'runtime-files-published.json').write_text('{}')
            (archive / name).symlink_to(target, target_is_directory=True)
        else:
            target.write_text('target')
            (archive / name).symlink_to(target)

    with pytest.raises(DataCorruptionError, match=message):
        retention.lookup_runtime_file(paths, 'input_receipts', 'unseen.json')


def test_concurrent_index_winner_is_kept_and_rebuild_temp_is_removed(tmp_path, monkeypatch):
    paths = gateway_paths_from_root(tmp_path / 'gateway')
    _outbox(paths, 'published')
    _archive(paths, 'client_outbox')
    index = paths.root / 'archive/runtime-files.sqlite3'
    _remove_family(index)
    _outbox(paths, 'newer', updated_at=2)
    link_calls = []

    def racing_link(source, destination):
        link_calls.append((source, destination))
        with sqlite3.connect(destination) as connection:
            connection.execute(
                'CREATE TABLE files(directory TEXT, name TEXT, month TEXT, digest TEXT, '
                'PRIMARY KEY(directory,name))',
            )
            connection.execute('PRAGMA user_version=73')
        raise FileExistsError(destination)

    monkeypatch.setattr(retention.os, 'link', racing_link)
    reports = _archive(paths, 'client_outbox')

    assert not any(report['errors'] for report in reports), reports
    assert len(link_calls) == 1
    with sqlite3.connect(index) as connection:
        assert connection.execute('PRAGMA user_version').fetchone()[0] == 73
        names = {row[0] for row in connection.execute('SELECT name FROM files')}
    assert names == {'newer.json'}
    assert not _rebuild_temporary_files(paths)


def test_rebuild_destination_disappearance_never_creates_empty_index(tmp_path, monkeypatch, caplog):
    paths = gateway_paths_from_root(tmp_path / 'gateway')
    _outbox(paths, 'published')
    _archive(paths, 'client_outbox')
    index = paths.root / 'archive/runtime-files.sqlite3'
    _remove_family(index)
    newer = _outbox(paths, 'newer', updated_at=2)

    def vanishing_link(source, destination):
        destination = Path(destination)
        destination.write_bytes(Path(source).read_bytes())
        destination.unlink()

    monkeypatch.setattr(retention.os, 'link', vanishing_link)
    with caplog.at_level(logging.WARNING, logger=retention.__name__):
        reports = _archive(paths, 'client_outbox')

    assert any(report['errors'] for report in reports)
    assert newer.exists() and not index.exists()
    assert not _rebuild_temporary_files(paths)
    assert not [record for record in caplog.records if 'GATEWAY_ARCHIVE_INDEX_REBUILT' in record.message]


def test_concurrent_symlink_index_winner_fails_closed(tmp_path, monkeypatch, caplog):
    paths = gateway_paths_from_root(tmp_path / 'gateway')
    _outbox(paths, 'published')
    _archive(paths, 'client_outbox')
    index = paths.root / 'archive/runtime-files.sqlite3'
    _remove_family(index)
    newer = _outbox(paths, 'newer', updated_at=2)
    winner = tmp_path / 'winner.sqlite3'
    with sqlite3.connect(winner) as connection:
        connection.execute(
            'CREATE TABLE files(directory TEXT, name TEXT, month TEXT, digest TEXT, '
            'PRIMARY KEY(directory,name))',
        )

    def symlink_winner(_source, destination):
        Path(destination).symlink_to(winner)
        raise FileExistsError(destination)

    monkeypatch.setattr(retention.os, 'link', symlink_winner)
    with caplog.at_level(logging.WARNING, logger=retention.__name__):
        reports = _archive(paths, 'client_outbox')

    assert any(error['error_type'] == 'DataCorruptionError'
               for report in reports for error in report['errors'])
    assert newer.exists() and index.is_symlink() and _index_rows(winner) == []
    assert not _rebuild_temporary_files(paths)
    assert not [record for record in caplog.records if 'GATEWAY_ARCHIVE_INDEX_REBUILT' in record.message]


def test_rebuild_link_permission_failure_preserves_source_and_cleans_temp(tmp_path, monkeypatch, caplog):
    paths = gateway_paths_from_root(tmp_path / 'gateway')
    _outbox(paths, 'published')
    _archive(paths, 'client_outbox')
    index = paths.root / 'archive/runtime-files.sqlite3'
    _remove_family(index)
    newer = _outbox(paths, 'newer', updated_at=2)

    def denied_link(_source, _destination):
        raise PermissionError('hard links unavailable')

    monkeypatch.setattr(retention.os, 'link', denied_link)
    with caplog.at_level(logging.WARNING, logger=retention.__name__):
        reports = _archive(paths, 'client_outbox')

    assert any(error['error_type'] == 'PermissionError'
               for report in reports for error in report['errors'])
    assert newer.exists() and not index.exists()
    assert not _rebuild_temporary_files(paths)
    assert not [record for record in caplog.records if 'GATEWAY_ARCHIVE_INDEX_REBUILT' in record.message]


@pytest.mark.parametrize('operation', ['lookup', 'rebuild'])
def test_cold_directory_replacement_during_enumeration_fails_closed(
        tmp_path, monkeypatch, runtime_index_link, operation):
    paths = gateway_paths_from_root(tmp_path / 'gateway')
    _outbox(paths, 'published')
    _archive(paths, 'client_outbox')
    index = paths.root / 'archive/runtime-files.sqlite3'
    _remove_family(index)
    newer = _outbox(paths, 'newer', updated_at=2) if operation == 'rebuild' else None
    cold = paths.root / 'archive/client_outbox'
    saved = paths.root / 'archive/client_outbox-saved'
    empty = tmp_path / 'empty-cold'
    empty.mkdir()
    real_scandir = os.scandir
    swapped = False

    def racing_scandir(target):
        nonlocal swapped
        if not swapped and (isinstance(target, int) or Path(target) == cold):
            cold.rename(saved)
            cold.symlink_to(empty, target_is_directory=True)
            swapped = True
        return real_scandir(target)

    monkeypatch.setattr(retention.os, 'scandir', racing_scandir)
    if operation == 'lookup':
        with pytest.raises(DataCorruptionError, match='runtime_archive_symlink'):
            retention.lookup_runtime_file(paths, 'client_outbox', 'published.json')
    else:
        reports = _archive(paths, 'client_outbox')
        assert any(report['errors'] for report in reports)
        assert newer is not None and newer.exists() and not index.exists()
        assert not _rebuild_temporary_files(paths)


@pytest.mark.parametrize('directory', ['input_receipts', 'client_outbox'])
def test_maintenance_rebuilds_index_without_new_archive_candidate(
        tmp_path, caplog, runtime_index_link, directory):
    paths = gateway_paths_from_root(tmp_path / 'gateway')
    if directory == 'input_receipts':
        _seed(paths)
    else:
        _outbox(paths, 'published')
    assert not any(report['errors'] for report in _archive(paths, directory))
    index = paths.root / 'archive/runtime-files.sqlite3'
    before_rows = _index_rows(index)
    _remove_family(index)

    with caplog.at_level(logging.WARNING, logger=retention.__name__):
        reports = _archive(paths, directory)

    assert not any(report['errors'] for report in reports), reports
    assert _index_rows(index) == before_rows
    rebuilt = [record for record in caplog.records if 'GATEWAY_ARCHIVE_INDEX_REBUILT' in record.message]
    assert len(rebuilt) == 1 and f'rows={len(before_rows)}' in rebuilt[0].message
    assert len(runtime_index_link) == 1


def test_cold_directory_disappearance_after_eligibility_never_reprepares(tmp_path, monkeypatch):
    paths = gateway_paths_from_root(tmp_path / 'gateway')
    row = _seed(paths)
    assert not any(report['errors'] for report in _archive(paths, 'input_receipts'))
    hot = inputs.gateway_input_receipt_path(paths, row.request_id)
    hot.unlink()
    index = paths.root / 'archive/runtime-files.sqlite3'
    _remove_family(index)
    cold = paths.root / 'archive/input_receipts'
    saved = cold.with_name('input_receipts-saved')
    original_months = retention._cold_months

    def disappear_before_months(gateway_paths, directory):
        if directory == 'input_receipts' and cold.exists():
            cold.rename(saved)
        return original_months(gateway_paths, directory)

    monkeypatch.setattr(retention, '_cold_months', disappear_before_months)
    prepare = Mock(return_value=row.prepared_request)
    with pytest.raises((DataCorruptionError, OSError)):
        _prepare(paths, row, prepare)
    prepare.assert_not_called()
    assert not hot.exists() and not index.exists()
    assert json.loads((saved / '197001/input-a.json').read_text()) == row.to_dict()


def test_month_replacement_during_file_enumeration_never_publishes_partial_index(
        tmp_path, monkeypatch, caplog, runtime_index_link):
    paths = gateway_paths_from_root(tmp_path / 'gateway')
    _outbox(paths, 'published')
    assert not any(report['errors'] for report in _archive(paths, 'client_outbox'))
    index = paths.root / 'archive/runtime-files.sqlite3'
    _remove_family(index)
    newer = _outbox(paths, 'newer', updated_at=2)
    month = paths.root / 'archive/client_outbox/197001'
    identity = month.stat()
    saved = month.with_name('197001-saved')
    empty = tmp_path / 'empty-month'
    empty.mkdir()
    real_scandir = os.scandir
    swapped = False

    def replace_before_file_scan(target):
        nonlocal swapped
        is_month = (os.path.samestat(os.fstat(target), identity)
                    if isinstance(target, int) else Path(target) == month)
        if is_month and not swapped:
            month.rename(saved)
            month.symlink_to(empty, target_is_directory=True)
            swapped = True
        return real_scandir(target)

    monkeypatch.setattr(retention.os, 'scandir', replace_before_file_scan)
    with caplog.at_level(logging.WARNING, logger=retention.__name__):
        reports = _archive(paths, 'client_outbox')
    assert swapped and any(report['errors'] for report in reports)
    assert newer.exists() and not index.exists()
    assert (saved / 'published.json').exists() and not list(empty.iterdir())
    assert not _rebuild_temporary_files(paths)
    assert not [record for record in caplog.records if 'GATEWAY_ARCHIVE_INDEX_REBUILT' in record.message]


def test_rebuild_ignores_lock_sidecars_created_by_archived_receipt_reads(tmp_path, runtime_index_link):
    paths = gateway_paths_from_root(tmp_path / 'gateway')
    row = _seed(paths)
    assert not any(report['errors'] for report in _archive(paths, 'input_receipts'))
    index = paths.root / 'archive/runtime-files.sqlite3'
    before_rows = _index_rows(index)
    inputs.gateway_input_receipt_path(paths, row.request_id).unlink()
    prepare = Mock(side_effect=AssertionError('must not prepare consumed input'))
    reused, created = _prepare(paths, row, prepare)
    assert reused == row and not created
    prepare.assert_not_called()
    lock = paths.root / 'archive/input_receipts/197001/input-a.json.lock'
    assert lock.is_file()
    _remove_family(index)
    _outbox(paths, 'newer', updated_at=2)

    assert not any(report['errors'] for report in _archive(paths, 'client_outbox'))
    after_rows = _index_rows(index)
    assert [row for row in after_rows if row[1] != 'newer.json'] == before_rows
    assert lock.is_file() and not _rebuild_temporary_files(paths)


@pytest.mark.parametrize('operation', ['lookup', 'rebuild'])
def test_real_month_replacement_before_open_preserves_consumed_input(
        tmp_path, monkeypatch, runtime_index_link, operation):
    paths = gateway_paths_from_root(tmp_path / 'gateway')
    row = _seed(paths)
    assert not any(report['errors'] for report in _archive(paths, 'input_receipts'))
    hot = inputs.gateway_input_receipt_path(paths, row.request_id)
    hot.unlink()
    index = paths.root / 'archive/runtime-files.sqlite3'
    _remove_family(index)
    newer = _outbox(paths, 'newer', updated_at=2) if operation == 'rebuild' else None
    month = paths.root / 'archive/input_receipts/197001'
    saved = month.with_name('197001-saved')
    real_open = retention.open_directory_beneath
    swapped = False

    def replace_before_open(root, parts, **options):
        nonlocal swapped
        if parts == ('archive', 'input_receipts', '197001') and not swapped:
            month.rename(saved)
            month.mkdir()
            swapped = True
        return real_open(root, parts, **options)

    monkeypatch.setattr(retention, 'open_directory_beneath', replace_before_open)
    prepare = Mock(return_value=row.prepared_request)
    if operation == 'lookup':
        with pytest.raises((DataCorruptionError, OSError)):
            _prepare(paths, row, prepare)
        prepare.assert_not_called()
    else:
        reports = _archive(paths, 'client_outbox')
        assert any(report['errors'] for report in reports)
        assert newer is not None and newer.exists()
    assert swapped and not index.exists() and not hot.exists()
    assert not _rebuild_temporary_files(paths)
    month.rmdir()
    saved.rename(month)
    reused, created = _prepare(paths, row, prepare)
    assert reused == row and not created
    prepare.assert_not_called()


def test_cold_directory_disappearance_during_rebuild_never_publishes_empty_index(
        tmp_path, monkeypatch, runtime_index_link):
    paths = gateway_paths_from_root(tmp_path / 'gateway')
    row = _seed(paths)
    assert not any(report['errors'] for report in _archive(paths, 'input_receipts'))
    index = paths.root / 'archive/runtime-files.sqlite3'
    _remove_family(index)
    newer = _outbox(paths, 'newer', updated_at=2)
    cold = paths.root / 'archive/input_receipts'
    saved = cold.with_name('input_receipts-saved')
    original_months = retention._cold_months

    def disappear_before_months(gateway_paths, directory):
        if directory == 'input_receipts' and cold.exists():
            cold.rename(saved)
        return original_months(gateway_paths, directory)

    monkeypatch.setattr(retention, '_cold_months', disappear_before_months)
    reports = _archive(paths, 'client_outbox')
    assert any(report['errors'] for report in reports)
    assert not index.exists() and newer.exists()
    assert json.loads((saved / '197001/input-a.json').read_text()) == row.to_dict()
    assert not _rebuild_temporary_files(paths)
