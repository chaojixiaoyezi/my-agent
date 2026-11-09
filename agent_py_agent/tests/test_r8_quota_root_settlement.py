"""账本自身的rename不能证明owner根目录没有准入外写入。"""
from pathlib import Path

import pytest

from agent_py_agent.agent.user_space import owner_quota as quota


def _clear_scans():
    with quota._locked_quota_scan_cache(blocking=True):
        scans = list(quota._QUOTA_SCANS.values())
        quota._QUOTA_SCANS.clear()
    for scan in scans:
        scan['iterator'].close()


def _finish(root, *, reports=None):
    for _ in range(256):
        report = quota.reconcile_owner_quota_slice(root, max_entries=64, max_ms=5000)
        if reports is not None:
            reports.append(report)
        assert report['enumerated_entries'] + report['settled_entries'] <= 1024 + report['replay_allowance']
        if report['complete']:
            return report
    raise AssertionError('bounded quota pass did not finish')


def _seed(root):
    for number in range(40):
        directory = root / f'd{number:03d}'
        directory.mkdir()
        for index in range(10):
            (directory / f'{index:04d}.bin').write_bytes(b'x')
    assert _finish(root)['used_bytes'] == 400


@pytest.mark.parametrize('collision', ['same_tick', 'before_ledger_rename'])
def test_root_external_write_is_settled_despite_ledger_stamp_chain(tmp_path, monkeypatch, collision):
    _clear_scans()
    try:
        _seed(tmp_path)
        if collision == 'before_ledger_rename':
            original = quota._write_quota_ledger
            injected = []

            def insert_before_rename(root, ledger):
                if not injected and ledger.get('reconcile_cursor', {}).get('complete') is False:
                    (tmp_path / 'oob.bin').write_bytes(b'z' * 4096)
                    injected.append(True)
                return original(root, ledger)

            monkeypatch.setattr(quota, '_write_quota_ledger', insert_before_rename)
        first = quota.reconcile_owner_quota_slice(tmp_path, max_entries=64, max_ms=5000)
        assert not first['complete']
        if collision == 'same_tick':
            frozen = tmp_path.stat()
            original_stat = Path.stat

            class SameTick:
                def __init__(self, actual):
                    self.actual = actual

                def __getattr__(self, key):
                    return getattr(frozen if key in {'st_mtime_ns', 'st_ctime_ns', 'st_mtime', 'st_ctime'}
                                   else self.actual, key)

            def coarse_stat(path, *args, **kwargs):
                actual = original_stat(path, *args, **kwargs)
                return SameTick(actual) if path == tmp_path else actual

            monkeypatch.setattr(Path, 'stat', coarse_stat)
            (tmp_path / 'oob.bin').write_bytes(b'z' * 4096)
        else:
            assert injected == [True]
        report = _finish(tmp_path)
        assert report['used_bytes'] == quota.owner_logical_usage_bytes(tmp_path, exact=True) == 4496
        assert quota._read_quota_ledger(tmp_path)['usage_authoritative'] is True
        assert quota._verify_quota_metadata(tmp_path, report['cursor'])
    finally:
        _clear_scans()


def test_large_root_settlement_obeys_existing_per_slice_entry_budget(tmp_path):
    _clear_scans()
    try:
        _seed(tmp_path)
        assert not quota.reconcile_owner_quota_slice(tmp_path, max_entries=64, max_ms=5000)['complete']
        for index in range(3000):
            (tmp_path / f'new-{index:04d}').write_bytes(b'z')
        reports = []
        report = _finish(tmp_path, reports=reports)
        assert sum(row['settled_entries'] > 0 for row in reports) >= 3
        assert sum(row['settled_entries'] for row in reports) >= 3000
        assert report['used_bytes'] == quota.owner_logical_usage_bytes(tmp_path, exact=True) == 3400
        assert quota._read_quota_ledger(tmp_path)['usage_authoritative'] is True
        assert quota._verify_quota_metadata(tmp_path, report['cursor'])
    finally:
        _clear_scans()


def test_unreadable_root_settlement_never_publishes_authoritative_usage(tmp_path, monkeypatch):
    _clear_scans()
    try:
        _seed(tmp_path)
        assert not quota.reconcile_owner_quota_slice(tmp_path, max_entries=64, max_ms=5000)['complete']
        (tmp_path / 'oob.bin').write_bytes(b'z' * 4096)
        original = quota._rescan_quota_dir

        def denied_root(root, rel, *args):
            if not rel:
                raise PermissionError('fixture root unavailable')
            return original(root, rel, *args)

        with monkeypatch.context() as failure:
            failure.setattr(quota, '_rescan_quota_dir', denied_root)
            assert _finish(tmp_path)['complete']
        ledger = quota._read_quota_ledger(tmp_path)
        assert ledger['usage_authoritative'] is False
        assert ledger['dirty_since'] is not None
        repaired = _finish(tmp_path)
        assert repaired['used_bytes'] == quota.owner_logical_usage_bytes(tmp_path, exact=True) == 4496
        assert quota._read_quota_ledger(tmp_path)['usage_authoritative'] is True
    finally:
        _clear_scans()


def test_cold_resume_during_large_root_settlement_stays_dirty_until_fresh_pass(tmp_path):
    _clear_scans()
    try:
        _seed(tmp_path)
        assert not quota.reconcile_owner_quota_slice(tmp_path, max_entries=64, max_ms=5000)['complete']
        for index in range(3000):
            (tmp_path / f'new-{index:04d}').write_bytes(b'z')
        for _ in range(256):
            report = quota.reconcile_owner_quota_slice(tmp_path, max_entries=64, max_ms=5000)
            if report['settled_entries']:
                break
        assert not report['complete'] and report['cursor']['settling_rel'] == ''
        _clear_scans()
        assert _finish(tmp_path)['complete']
        assert quota._read_quota_ledger(tmp_path)['usage_authoritative'] is False
        repaired = _finish(tmp_path)
        assert repaired['used_bytes'] == quota.owner_logical_usage_bytes(tmp_path, exact=True) == 3400
        assert quota._read_quota_ledger(tmp_path)['usage_authoritative'] is True
    finally:
        _clear_scans()
