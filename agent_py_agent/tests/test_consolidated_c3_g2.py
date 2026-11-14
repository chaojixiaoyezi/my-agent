"""Consolidated tests for shard c3:
- test_r8_quota_root_settlement.py: 账本自身的rename不能证明owner根目录没有准入外写入。
- test_r8_retention_directory_fds.py: 目录扫描跨片和退休不借助GC释放原生目录句柄。
- test_r8_sqlite_recovery.py: 通用库恢复以真实SQLite验证，不将损坏、未知或缺行当作空库。
- test_r8_tui_explicit_stream.py: 真实Gateway传输的公开候选只进入TUI活动块，终稿沿原唯一边界。
- test_r9_asset_clean_json.py: 资产净化保留完整安全合同，纯JSON路径不再遍历通用异常/Mapping适配器。
- test_r9_background_restart_park.py: 可逆原片收口：不中止持久工作，超时保持旧服务准入与未知门。
- test_r9_candidate_anchor_index.py: 规范候选锚点索引的全序、增量更新、迁移续点与失败原头保护。
"""

import json
import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.assets import descriptor
from agent_py_agent.agent.concurrency import restart_gate
from agent_py_agent.agent.gateway_parts import restart_service
from agent_py_agent.agent.gateway_parts.paths import gateway_chunk_path, gateway_paths
from agent_py_agent.agent.gateway_parts.queue_service import archive_request
from agent_py_agent.agent.gateway_parts.stream_writer import BufferedChunkStreamWriter
from agent_py_agent.agent.io.sqlite_recovery import recover_sqlite_database
from agent_py_agent.agent.io.sqlite_scope import scoped_connection
from agent_py_agent.agent.memory_store import candidate_ledger
from agent_py_agent.agent.memory_store import candidates as module
from agent_py_agent.agent.memory_store import retention_scan as scan
from agent_py_agent.agent.memory_store.candidate_anchor_index import STATUSES, eligible, group_key
from agent_py_agent.agent.memory_store.candidate_models import CandidateObservation, MemoryScope
from agent_py_agent.agent.memory_store.candidates import CandidateService
from agent_py_agent.agent.runtime_db.recovery_guard import (
    close_runtime_connection,
    guarded_runtime_connect,
    request_runtime_db_recovery,
    runtime_db_recovery_pending,
)
from agent_py_agent.agent.user_space import owner_quota as quota
from agent_py_agent.cli import gateway_restart_handover as handover
from agent_py_agent.cli.chat_parts.gateway_client import (
    GatewayChunkPollRequest,
    NetworkGatewayObserver,
)
from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime, TuiTurnSummary
from agent_py_agent.tests.test_gateway_background_slice_shutdown import _release, _running
from agent_py_agent.tests.test_r8_candidate_pin_stream import _projection
from agent_py_agent.tests.test_s2w2_tui_network_mode import live_gateway  # noqa: F401

# ============================================================================
# Source: agent_py_agent/tests/test_r8_quota_root_settlement.py
# ============================================================================

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


def test_growth_checks_root_membership_when_directory_timestamps_collide(tmp_path, monkeypatch):
    from agent_py_agent.tests.test_owner_quota_incremental_recovery import _r7_grow

    _clear_scans()
    try:
        for index in range(301):
            (tmp_path / f'{index:05d}').write_bytes(b'x')
        _finish(tmp_path)
        assert _r7_grow(tmp_path) == 'ok'
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
        assert tmp_path.stat().st_mtime_ns == frozen.st_mtime_ns
        assert tmp_path.stat().st_ctime_ns == frozen.st_ctime_ns
        (tmp_path / 'oob.bin').write_bytes(b'z' * 4096)
        assert _r7_grow(tmp_path) == 'refused'
        assert quota._read_quota_ledger(tmp_path)['dirty_since'] is not None
        assert _finish(tmp_path)['used_bytes'] == quota.owner_logical_usage_bytes(tmp_path, exact=True) == 4398
    finally:
        _clear_scans()


def test_external_root_member_between_admission_and_settlement_is_not_vouched(tmp_path):
    from agent_py_agent.tests.test_owner_quota_incremental_recovery import _r7_grow

    _clear_scans()
    try:
        for index in range(301):
            (tmp_path / f'{index:05d}.lock').write_bytes(b'x')
        _finish(tmp_path)
        enforcer = quota.OwnerQuotaEnforcer(tmp_path, max_bytes=1 << 30)
        with enforcer.admission() as admission:
            admission.check([quota.OwnerQuotaChange(tmp_path / 'managed', 1)])
            (tmp_path / 'managed').write_bytes(b'y')
            (tmp_path / 'oob.bin').write_bytes(b'z' * 4096)
        ledger = quota._read_quota_ledger(tmp_path)
        assert ledger['dirty_since'] is not None
        assert _r7_grow(tmp_path) == 'refused'
        assert _finish(tmp_path)['used_bytes'] == quota.owner_logical_usage_bytes(tmp_path, exact=True) == 4398
    finally:
        _clear_scans()


@pytest.mark.parametrize('members', [None, [True, 0], [-1, 0], [1]])
def test_missing_or_invalid_root_member_proof_requires_real_rebuild(tmp_path, members):
    _clear_scans()
    try:
        for index in range(301):
            (tmp_path / f'{index:05d}.lock').write_bytes(b'x')
        _finish(tmp_path)
        ledger = quota._read_quota_ledger(tmp_path)
        if members is None:
            ledger['reconcile_cursor'].pop('root_membership', None)
        else:
            ledger['reconcile_cursor']['root_membership'] = members
        quota._write_quota_ledger(tmp_path, ledger)
        enforcer = quota.OwnerQuotaEnforcer(tmp_path, max_bytes=1 << 30)
        with pytest.raises(quota.OwnerQuotaUnavailable) as error:
            with enforcer.reserve([quota.OwnerQuotaChange(tmp_path / 'managed', 1)]):
                pytest.fail('unknown lock classifications must not authorize growth')
        assert error.value.reason_code == 'reconciliation_pending'
        _finish(tmp_path)
        with enforcer.reserve([quota.OwnerQuotaChange(tmp_path / 'managed', 1)]):
            (tmp_path / 'managed').write_bytes(b'y')
        assert quota._read_quota_ledger(tmp_path)['used_bytes'] == 302
    finally:
        _clear_scans()


@pytest.mark.parametrize('net_growth', [0, -1])
def test_non_growth_check_does_not_scan_root_but_later_growth_checks_fresh_members(
    tmp_path, monkeypatch, net_growth,
):
    _clear_scans()
    try:
        for index in range(301):
            (tmp_path / f'{index:05d}.bin').write_bytes(b'x')
        target = tmp_path / 'managed'
        target.write_bytes(b'y')
        _finish(tmp_path)
        original = quota._quota_large_dir_matches
        checked = []

        def observed(*args, **kwargs):
            checked.append(args[1])
            return original(*args, **kwargs)

        monkeypatch.setattr(quota, '_quota_large_dir_matches', observed)
        enforcer = quota.OwnerQuotaEnforcer(tmp_path, max_bytes=1 << 30)
        with enforcer.admission() as admission:
            admission.check([quota.OwnerQuotaChange(target, 0, append=net_growth == 0)])
            assert checked == []
            (tmp_path / 'oob.bin').write_bytes(b'z' * 4096)
            with pytest.raises(quota.OwnerQuotaUnavailable) as error:
                admission.check([quota.OwnerQuotaChange(target, 1, append=True)])
            assert error.value.reason_code == 'reconciliation_pending'
            assert checked
        assert _finish(tmp_path)['used_bytes'] == quota.owner_logical_usage_bytes(tmp_path, exact=True) == 4398
    finally:
        _clear_scans()


@pytest.mark.parametrize('phase', ['before_growth', 'during_transaction'])
def test_equal_count_external_plain_member_replacement_is_not_adopted(tmp_path, phase):
    from agent_py_agent.tests.test_owner_quota_incremental_recovery import _r7_grow

    _clear_scans()
    try:
        for index in range(301):
            (tmp_path / f'{index:05d}.bin').write_bytes(b'x')
        (tmp_path / 'managed').write_bytes(b'y')
        _finish(tmp_path)

        def replace_member():
            (tmp_path / '00000.bin').unlink()
            (tmp_path / 'replacement.bin').write_bytes(b'z' * 4096)

        if phase == 'before_growth':
            replace_member()
            assert _r7_grow(tmp_path) == 'refused'
            expected = 4397
        else:
            enforcer = quota.OwnerQuotaEnforcer(tmp_path, max_bytes=1 << 30)
            with enforcer.reserve([quota.OwnerQuotaChange(tmp_path / 'managed', 1, append=True)]):
                with (tmp_path / 'managed').open('ab') as stream:
                    stream.write(b'y')
                replace_member()
            assert _r7_grow(tmp_path) == 'refused'
            expected = 4398
        assert quota._read_quota_ledger(tmp_path)['dirty_since'] is not None
        assert _finish(tmp_path)['used_bytes'] == quota.owner_logical_usage_bytes(tmp_path, exact=True) == expected
    finally:
        _clear_scans()


# ============================================================================
# Source: agent_py_agent/tests/test_r8_retention_directory_fds.py
# ============================================================================

def _target_fds(path):
    result = []
    directory = Path('/proc/self/fd') if Path('/proc/self/fd').is_dir() else Path('/dev/fd')
    for name in os.listdir(directory):
        try:
            fd = int(name)
            if os.uname().sysname == 'Darwin':
                import fcntl
                target = os.fsdecode(fcntl.fcntl(fd, 50, bytes(1024)).split(b'\0', 1)[0])
            else:
                target = os.readlink(directory / name)
            if target == str(path):
                result.append(fd)
        except (OSError, ValueError):
            continue
    return result


@pytest.mark.skipif(os.name != 'posix', reason='native directory descriptor lifecycle')
def test_partial_directory_iteration_has_no_live_directory_descriptors(tmp_path):
    projection, _context = _projection(tmp_path)
    projection.contents["pass_token"] = "test-pass"
    projection.budget = SimpleNamespace(max_entries=128, entries=0)
    root = Path(projection.home.owner_home_dir)
    work = root / 'tasks/2026-11-07/active-task/work'
    work.mkdir(parents=True)
    for number in range(260):
        (work / str(number)).write_text('fixture')
    entries = projection._entries(work)
    try:
        seen = []
        for _ in range(260):
            child, directory = next(entries)
            seen.append(child.name)
            assert not directory
            assert _target_fds(work) == []
        with pytest.raises(StopIteration):
            next(entries)
        assert len(set(seen)) == 260
        assert work in projection.sources
    finally:
        entries.close()
        projection.close()


@pytest.mark.skipif(os.name != 'posix', reason='native directory descriptor lifecycle')
@pytest.mark.parametrize('native', [True, False])
def test_close_explicitly_closes_nested_directory_generators_with_retained_references(tmp_path, monkeypatch, native):
    from agent_py_agent.agent.user_space import directory_paging
    if not native:
        monkeypatch.setattr(directory_paging, '_native_directory_supported', lambda: False)
    projection, _context = _projection(tmp_path)
    projection.contents["pass_token"] = "test-pass"
    projection.budget = SimpleNamespace(max_entries=128, entries=0)
    root = Path(projection.home.owner_home_dir)
    root.mkdir(parents=True)
    (root / 'child').mkdir()
    original = projection._entries
    retained = []
    def entries(*args, **kwargs):
        stream = original(*args, **kwargs)
        retained.append(stream)
        return stream
    monkeypatch.setattr(projection, '_entries', entries)
    projection.steps = projection._walk(root)
    next(projection.steps)
    if not native:
        assert _target_fds(root), "fixture did not hold a real portable scandir descriptor"
    projection.close()
    assert retained and all(stream.gi_frame is None for stream in retained)
    assert _target_fds(root) == []
    projection.close()


@pytest.mark.skipif(os.name != 'posix', reason='native directory descriptor lifecycle')
def test_long_directory_native_cursor_reads_each_member_once(tmp_path, monkeypatch):
    from agent_py_agent.agent.user_space import owner_quota
    projection, _context = _projection(tmp_path)
    projection.contents['pass_token'] = 'test-pass'
    projection.budget = SimpleNamespace(max_entries=128, entries=0)
    root = Path(projection.home.owner_home_dir)
    root.mkdir(parents=True)
    for number in range(4096):
        (root / str(number)).touch()
    original, count = owner_quota._QuotaDirectoryStream.__next__, [0]
    def advance(stream):
        value = original(stream)
        count[0] += 1
        return value
    monkeypatch.setattr(owner_quota._QuotaDirectoryStream, '__next__', advance)
    try:
        assert len(list(projection._entries(root))) == 4096
        assert count[0] == 4096, 'native cursor replayed a previous directory prefix'
        assert _target_fds(root) == []
    finally:
        projection.close()


@pytest.mark.skipif(os.name != 'posix', reason='native directory descriptor lifecycle')
@pytest.mark.parametrize('change', ['add', 'replace', 'symlink'])
def test_directory_generation_change_never_publishes_complete_source(tmp_path, change):
    projection, _context = _projection(tmp_path)
    projection.contents['pass_token'] = 'test-pass'
    projection.budget = SimpleNamespace(max_entries=1, entries=0)
    root = Path(projection.home.owner_home_dir)
    directory = root / 'tasks'
    directory.mkdir(parents=True)
    for number in range(4):
        (directory / str(number)).touch()
    entries = projection._entries(directory)
    next(entries)
    assert _target_fds(directory) == []
    if change == 'add':
        before = directory.stat()
        (directory / 'new').touch()
        # 此例验证页间metadata换代；粗时钟下的同stamp新增另由发布前内容复核覆盖。
        os.utime(directory, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000))
    else:
        directory.rename(root / 'old')
        if change == 'replace':
            directory.mkdir()
        else:
            directory.symlink_to(root / 'old', target_is_directory=True)
    try:
        with pytest.raises(OSError):
            list(entries)
        assert directory not in projection.sources
        assert _target_fds(directory) == []
    finally:
        entries.close()
        projection.close()


@pytest.mark.skipif(os.name != 'posix', reason='native directory descriptor lifecycle')
def test_partial_task_work_scan_releases_descriptors_before_retirement(tmp_path):
    projection, _context = _projection(tmp_path)
    projection.contents['pass_token'] = 'test-pass'
    projection.budget = SimpleNamespace(max_entries=1, entries=0)
    root = Path(projection.home.owner_home_dir)
    work = root / 'tasks/2026-11-07/active-task/work'
    work.mkdir(parents=True)
    (work / 'state.json').write_text('{}')
    projection.steps = projection._task_states()
    visited = False
    try:
        for value in projection.steps:
            assert _target_fds(work) == []
            if value == work / 'state.json':
                visited = True
                break
        assert visited
        assert not projection.closed
    finally:
        projection.close()
    assert _target_fds(work) == []


def test_pin_chunk_pause_releases_source_without_gc(tmp_path, monkeypatch):
    projection, _context = _projection(tmp_path)
    projection.contents['pass_token'] = 'pass'
    class Budget:
        max_bytes = 4
        size = 0
        def charge(self, *, size):
            self.size += size
    projection.budget = Budget()
    path = projection.home.owner_home_dir / 'tasks/day/task/work/state.json'
    path.parent.mkdir(parents=True)
    path.write_bytes(b'abcdefgh')
    opened = []
    original = scan._fingerprint_source
    def capture(source):
        stream = original(source)
        opened.append(stream)
        return stream
    monkeypatch.setattr(scan, '_fingerprint_source', capture)
    chunks = projection._chunks(path, required=True)
    try:
        assert next(chunks) == b'abcd'
        assert opened and all(source.closed for source in opened)
        assert next(chunks) == b''
        assert all(source.closed for source in opened)
        projection.budget.size = 0
        assert next(chunks) == b'efgh'
        assert all(source.closed for source in opened)
        with pytest.raises(StopIteration):
            next(chunks)
        import hashlib
        assert projection.sources[path][1] == hashlib.sha256(b'abcdefgh').digest()
    finally:
        chunks.close()
        projection.close()


def test_pin_window_stops_at_deadline_and_resumes_without_rereading(tmp_path, monkeypatch):
    from agent_py_agent.agent.memory_store import retention_scan_budget

    projection, _context = _projection(tmp_path)
    projection.contents.update(pass_token="test", chunk_bytes=4)
    path = projection.home.owner_home_dir / 'tasks/day/task/work/state.json'
    path.parent.mkdir(parents=True)
    path.write_bytes(b'abcdefghijkl')
    clock, handles, reads = [0.0], [], []
    original = scan._fingerprint_source
    monkeypatch.setattr(retention_scan_budget, 'time', SimpleNamespace(monotonic=lambda: clock[0]))

    class Meter:
        def __init__(self, raw):
            self.raw = raw

        def __getattr__(self, name):
            return getattr(self.raw, name)

        def read1(self, size):
            reads.append((self.raw.tell(), size))
            if len(reads) == 1:
                clock[0] = 2.0
            return self.raw.read1(size)

    def open_source(target):
        raw = original(target)
        handles.append(raw)
        return Meter(raw)

    monkeypatch.setattr(scan, '_fingerprint_source', open_source)
    chunks = projection._chunks(path, required=True)
    try:
        projection.budget = retention_scan_budget.RetentionScanBudget(8, 32, 1.0)
        assert next(chunks) == b'abcd'
        assert projection.budget.exhausted() and all(f.closed for f in handles)
        projection.budget = retention_scan_budget.RetentionScanBudget(8, 32, 10.0)
        assert next(chunks) == b'efghijkl'
        assert all(f.closed for f in handles)
        with pytest.raises(StopIteration):
            next(chunks)
        assert len(handles) == 2
        assert reads == [(0, 4), (4, 4), (8, 4)]
    finally:
        chunks.close()
        projection.close()


@pytest.mark.parametrize('change', ['replace', 'truncate', 'rewrite', 'missing'])
def test_pin_chunk_resume_rejects_changed_source_and_releases_handles(tmp_path, monkeypatch, change):
    projection, _context = _projection(tmp_path)
    projection.contents['pass_token'] = 'pass'
    class Budget:
        max_bytes = 4
        size = 0
        def charge(self, *, size):
            self.size += size
    projection.budget = Budget()
    path = projection.home.owner_home_dir / 'tasks/day/task/work/state.json'
    path.parent.mkdir(parents=True)
    path.write_bytes(b'abcdefgh')
    opened = []
    original = scan._fingerprint_source
    def capture(source):
        stream = original(source)
        opened.append(stream)
        return stream
    monkeypatch.setattr(scan, '_fingerprint_source', capture)
    chunks = projection._chunks(path, required=True)
    try:
        assert next(chunks) == b'abcd'
        assert all(source.closed for source in opened)
        if change == 'replace':
            replacement = path.with_suffix('.replacement')
            replacement.write_bytes(b'abcdefgh')
            replacement.replace(path)
        elif change == 'truncate':
            path.write_bytes(b'ab')
        elif change == 'missing':
            path.unlink()
        else:
            before = path.stat()
            path.write_bytes(b'ABCDEFGH')
            # 本例测身份换代拒绝，不依赖仿真文件系统能分辨连续写入的时钟tick。
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000))
        projection.budget.size = 0
        with pytest.raises(FileNotFoundError if change == 'missing' else scan._PinReadChanged):
            next(chunks)
        assert path not in projection.sources
        assert all(source.closed for source in opened)
    finally:
        chunks.close()
        projection.close()


@pytest.mark.skipif(os.name != 'posix', reason='native directory descriptor lifecycle')
def test_same_stamp_directory_addition_is_rejected_by_prepublication_verification(tmp_path, monkeypatch):
    import json

    from agent_py_agent.agent.memory_store.retention_models import MemoryRetentionPolicy
    from agent_py_agent.agent.memory_store.retention_scan_budget import RetentionScanBudget
    from agent_py_agent.agent.user_space import directory_paging

    projection, _context = _projection(tmp_path)
    home = projection.home
    home.owner_home_dir.mkdir(parents=True)
    home.owner_retention_json.write_text(json.dumps(MemoryRetentionPolicy().to_dict()))
    directory = home.owner_home_dir / 'tasks/2026-11-07'
    state = directory / 'old/work/state.json'
    state.parent.mkdir(parents=True)
    before = b'{"task_id":"old","status":"RUNNING","source_refs":["old-proof"]}'
    state.write_bytes(before)
    stamp = projection._stamp(directory)
    original_stamp = projection._stamp
    monkeypatch.setattr(projection, '_stamp', lambda path: stamp if path == directory else original_stamp(path))
    original_page = directory_paging.directory_page
    added = []
    def page(root, parts, cursor, limit, **kwargs):
        result = original_page(root, parts, cursor, limit, **kwargs)
        if Path(root).joinpath(*parts) == directory and not added:
            assert result[2], 'fixture must finish the original bounded page before mutation'
            fresh = directory / 'new/work/state.json'
            fresh.parent.mkdir(parents=True)
            fresh.write_text('{"task_id":"new","status":"RUNNING","source_refs":["new-proof"]}')
            added.append(fresh)
        return result
    monkeypatch.setattr(directory_paging, 'directory_page', page)
    try:
        # 走真实build→pending→verify→current发表链；每拍仍使用正式默认预算。
        with pytest.raises(scan._PinReadChanged):
            for _ in range(32):
                projection.budget = RetentionScanBudget.default()
                assert projection.current([]) is None
            pytest.fail('changed directory was neither rejected nor completed within bounded continuation')
        assert added and projection.value is None
        assert projection.phase == "verify" and projection.pending is not None
        assert state.read_bytes() == before
        assert added[0].is_file()
        assert _target_fds(directory) == []
    finally:
        projection.close()


# ============================================================================
# Source: agent_py_agent/tests/test_r8_sqlite_recovery.py
# ============================================================================

def _database(path):
    with closing(sqlite3.connect(path)) as conn:
        conn.execute('CREATE TABLE records(id INTEGER PRIMARY KEY, value BLOB)')
        conn.executemany('INSERT INTO records VALUES(?,?)', [(i, ('record-' + str(i)).encode()) for i in range(1, 1500)])
        conn.execute('CREATE INDEX records_value ON records(value)')
        conn.execute('PRAGMA user_version=7')
        conn.commit()


def test_false_corruption_intent_is_consumed_at_original_write_entry(tmp_path):
    path = tmp_path / 'local.db'
    _database(path)
    request_runtime_db_recovery(path, reason="sqlite_structural_error")
    with scoped_connection(path, False, lambda: sqlite3.connect(path)) as conn:
        assert conn.execute('SELECT count(*) FROM records').fetchone() == (1499,)
    assert not runtime_db_recovery_pending(path)
    assert not list(tmp_path.glob('local.db.damaged-*'))


def test_actual_notadb_preserves_all_evidence_and_blocks_reentry(tmp_path):
    path = tmp_path / 'run-index.sqlite3'
    path.write_bytes(b'not a sqlite database' * 100)
    files = {'': path.read_bytes(), '-wal': b'original wal', '-shm': b'original shm', '-journal': b'\0' * 8 + b'original journal'}
    for suffix, raw in files.items():
        Path(str(path) + suffix).write_bytes(raw)
    request_runtime_db_recovery(path)
    report = recover_sqlite_database(path)
    assert report['status'] == 'requires_recovery', report
    assert 'next_action' in report
    assert runtime_db_recovery_pending(path)
    backup = path.with_name(report['backup'])
    for suffix, raw in files.items():
        assert Path(str(backup) + suffix).read_bytes() == raw
    before = {p.name for p in tmp_path.iterdir()}
    assert recover_sqlite_database(path)['status'] == 'requires_recovery'
    assert before == {p.name for p in tmp_path.iterdir()}


def test_real_damaged_index_rebuilt_without_record_loss(tmp_path):
    path = tmp_path / 'local.db'
    _database(path)
    with closing(sqlite3.connect(path)) as conn:
        page = conn.execute("SELECT rootpage FROM sqlite_master WHERE name='records_value'").fetchone()[0]
        size = conn.execute('PRAGMA page_size').fetchone()[0]
        original = conn.execute('SELECT * FROM records NOT INDEXED').fetchall()
    with path.open('r+b') as stream:
        stream.seek((page - 1) * size)
        stream.write(b'\0' * size)
    request_runtime_db_recovery(path)
    report = recover_sqlite_database(path)
    assert report['status'] == 'recovered', report
    assert not runtime_db_recovery_pending(path)
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute('SELECT * FROM records NOT INDEXED').fetchall() == original
        assert conn.execute('PRAGMA user_version').fetchone() == (7,)
        assert conn.execute('PRAGMA integrity_check').fetchall() == [('ok',)]
    assert path.with_name(report['backup']).exists()


def test_live_generation_prevents_recovery_until_physical_close(tmp_path):
    path = tmp_path / 'local.db'
    _database(path)
    entered, release = threading.Event(), threading.Event()
    def reader():
        conn = guarded_runtime_connect(path, lambda: sqlite3.connect(path))
        entered.set()
        assert release.wait(5)
        close_runtime_connection(conn)
    thread = threading.Thread(target=reader)
    thread.start()
    assert entered.wait(5)
    try:
        request_runtime_db_recovery(path)
        assert recover_sqlite_database(path)['status'] == 'deferred'
        assert runtime_db_recovery_pending(path)
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()
    assert recover_sqlite_database(path)['status'] == 'healthy'


def test_readonly_entry_does_not_turn_intent_into_mutating_recovery(tmp_path):
    path = tmp_path / 'local.db'
    _database(path)
    request_runtime_db_recovery(path)
    with pytest.raises(sqlite3.OperationalError):
        with scoped_connection(path, True, lambda: sqlite3.connect(path)):
            pass
    assert not list(tmp_path.glob('*.damaged-*'))


def test_interrupted_dump_retires_only_bound_partial_candidate_and_retries(tmp_path, monkeypatch):
    from agent_py_agent.agent.io import sqlite_recovery
    path = tmp_path / 'local.db'
    _database(path)
    with closing(sqlite3.connect(path)) as conn:
        page = conn.execute("SELECT rootpage FROM sqlite_master WHERE name='records_value'").fetchone()[0]
        size = conn.execute('PRAGMA page_size').fetchone()[0]
    with path.open('r+b') as stream:
        stream.seek((page - 1) * size)
        stream.write(b'\0' * size)
    original_source = path.read_bytes()
    original_connect = sqlite3.connect
    class InterruptedConnection(sqlite3.Connection):
        statements = 0
        def execute(self, statement, parameters=()):
            self.statements += 1
            if self.statements == 6:
                raise KeyboardInterrupt('interrupted in full dump')
            return super().execute(statement, parameters)
    def connect(database, *args, **kwargs):
        if '.salvage-' in str(database) and str(database).endswith(tuple('0123456789abcdef')):
            kwargs['factory'] = InterruptedConnection
        return original_connect(database, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(sqlite_recovery.sqlite3, 'connect', connect)
        with pytest.raises(KeyboardInterrupt):
            recover_sqlite_database(path)
    import json

    from agent_py_agent.agent.runtime_db.schema import SALVAGE_REQUEST_SUFFIX
    state = json.loads(Path(str(path) + SALVAGE_REQUEST_SUFFIX).read_text())
    candidate = path.with_name(state['candidate'])
    backup = path.with_name(state['backup'])
    assert state['phase'] == 'building_candidate' and candidate.exists()
    assert path.read_bytes() == backup.read_bytes() == original_source
    receipt = recover_sqlite_database(path)
    assert receipt['status'] == 'recovered', receipt
    assert backup.read_bytes() == original_source
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute('SELECT count(*) FROM records').fetchone() == (1499,)
        assert conn.execute('PRAGMA integrity_check').fetchall() == [('ok',)]


def test_real_generic_structural_error_marks_only_corruption_recovery_intent(tmp_path):
    from agent_py_agent.agent.runtime_db.recovery_guard import (
        runtime_db_corruption_recovery_pending,
    )
    path = tmp_path / 'local.db'
    path.write_bytes(b'not a sqlite database' * 100)
    factory_calls = []
    def connect():
        factory_calls.append(True)
        return sqlite3.connect(path)
    with pytest.raises(sqlite3.DatabaseError) as error:
        with scoped_connection(path, False, connect) as conn:
            conn.execute('SELECT name FROM sqlite_master').fetchall()
    assert error.value.sqlite_errorcode & 0xff == 26
    assert runtime_db_corruption_recovery_pending(path)
    with pytest.raises(sqlite3.OperationalError) as deferred:
        with scoped_connection(path, False, connect):
            raise AssertionError('damaged source must not be admitted')
    assert deferred.value.sqlite_recovery['status'] == 'requires_recovery'
    assert factory_calls == [True]


@pytest.mark.parametrize('replace_with_manual', [False, True])
def test_automatic_consumer_rechecks_intent_under_ex_before_unlock(tmp_path, monkeypatch, replace_with_manual):
    from agent_py_agent.agent.io import sqlite_scope
    from agent_py_agent.agent.runtime_db.recovery_guard import (
        runtime_db_corruption_recovery_pending,
        runtime_db_generation_guard,
    )
    path = tmp_path / 'local.db'
    _database(path)
    request_runtime_db_recovery(path, reason='sqlite_structural_error')
    selected, replaced = threading.Event(), threading.Event()
    def controller():
        assert selected.wait(5)
        with runtime_db_generation_guard(path, exclusive=True):
            pass
        if replace_with_manual:
            request_runtime_db_recovery(path)
        replaced.set()
    thread = threading.Thread(target=controller)
    thread.start()
    original_check = sqlite_scope.runtime_db_corruption_recovery_pending
    def paused_check(db):
        result = original_check(db)
        selected.set()
        assert replaced.wait(5)
        return result
    monkeypatch.setattr(sqlite_scope, 'runtime_db_corruption_recovery_pending', paused_check)
    factories = []
    def connect():
        factories.append(True)
        return sqlite3.connect(path)
    try:
        if replace_with_manual:
            with pytest.raises(sqlite3.OperationalError) as error:
                with scoped_connection(path, False, connect):
                    raise AssertionError('new manual intent must stay fenced')
            assert error.value.sqlite_recovery['reason'] == 'recovery_intent_requires_original_controller'
            assert runtime_db_recovery_pending(path) and not runtime_db_corruption_recovery_pending(path)
            assert factories == []
        else:
            with scoped_connection(path, False, connect) as conn:
                assert conn.execute('SELECT count(*) FROM records').fetchone() == (1499,)
            assert not runtime_db_recovery_pending(path)
            assert factories == [True]
    finally:
        selected.set()
        thread.join(5)
    assert not thread.is_alive()


def test_factory_close_failure_retains_physical_guard_until_creator_confirms_close(tmp_path):
    from agent_py_agent.agent.runtime_db.recovery_guard import runtime_db_generation_guard
    path = tmp_path / 'local.db'
    _database(path)
    failed, release = threading.Event(), threading.Event()
    captured = []
    class CloseFails(sqlite3.Connection):
        def close(self):
            if not release.is_set():
                raise sqlite3.OperationalError('physical close unconfirmed')
            return super().close()
    original = sqlite3.DatabaseError('initialization failed')
    def creator():
        def factory():
            conn = sqlite3.connect(path, factory=CloseFails)
            try:
                raise original
            except BaseException as error:
                try:
                    conn.close()
                except BaseException as cleanup:
                    error.sqlite_cleanup_error = cleanup
                    error.sqlite_unclosed_factory_connection = conn
                raise
        try:
            guarded_runtime_connect(path, factory)
        except BaseException as error:
            captured.append(error)
            failed.set()
            assert release.wait(5)
            close_runtime_connection(error.sqlite_unclosed_factory_connection)
    thread = threading.Thread(target=creator)
    thread.start()
    assert failed.wait(5)
    try:
        assert captured == [original]
        assert isinstance(original.sqlite_cleanup_error, sqlite3.OperationalError)
        with pytest.raises(sqlite3.OperationalError):
            with runtime_db_generation_guard(path, exclusive=True):
                raise AssertionError('unclosed factory conn must retain SH')
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()
    with runtime_db_generation_guard(path, exclusive=True):
        pass


def test_factory_close_exception_after_physical_close_does_not_leak_guard(tmp_path):
    from agent_py_agent.agent.runtime_db.recovery_guard import runtime_db_generation_guard
    path = tmp_path / 'local.db'
    _database(path)
    original = sqlite3.DatabaseError('initialization failed')
    def factory():
        conn = sqlite3.connect(path)
        conn.close()
        original.sqlite_unclosed_factory_connection = conn
        raise original
    with pytest.raises(sqlite3.DatabaseError) as error:
        guarded_runtime_connect(path, factory)
    assert error.value is original
    with runtime_db_generation_guard(path, exclusive=True):
        pass


# ============================================================================
# Source: agent_py_agent/tests/test_r8_tui_explicit_stream.py
# ============================================================================

def _visible(runtime):
    snapshot = runtime.store.snapshot()
    return ''.join(block.text for block in (*snapshot.stable_blocks, *snapshot.active_blocks)
                   if block.role == 'assistant')


@pytest.mark.parametrize("sensitive", [False, True])
@pytest.mark.parametrize("reconnect", [False, True])
@pytest.mark.parametrize("level", ["stream", "off"])
def test_real_network_public_candidate_before_canonical_final(live_gateway, monkeypatch, reconnect, level, sensitive):  # noqa: F811
    paths = live_gateway['paths']
    runtime = TuiRuntime('wire-stream')
    turn = runtime.begin_turn('r')
    from agent_py_agent.agent.gateway_parts.stream_events import public_model_text

    secret = 'SyntheticWireCredentialOnly'
    raw = '公开候选正文。' * 100
    if sensitive:
        raw = raw[:90] + '{"password":"' + secret + '"}' + raw[90:]
    text = public_model_text(raw, max_chars=0, delivery_channel='', identifiers=())
    writer = BufferedChunkStreamWriter(gateway_chunk_path(paths, 'r'),
        text_delta_holdback_chars=8, rich_transcript=True)
    writer.set_verbose_level(level)
    write = writer.bind_model_call('call-1')
    write('即将撤回的旧候选。' * 40)
    writer.reset_model_text('retry', model_call_id='call-1')
    for offset in range(0, len(raw), 32):
        write(raw[offset:offset + 32])
    writer.commit_model_response({'schema': 'assistant_messages.v1',
        'source': {'call_id': 'call-1'}, 'boundary': 'terminal',
        'messages': [{'text': raw}], 'final_text': raw})
    writer.flush()
    record = paths.processing / 'r.json'
    record.write_text(json.dumps({'id': 'r', 'execution_attempt_id': 'a', 'user_id': 'alice', 'channel': 'chat'}))
    terminal = {'ok': True, 'response': text, 'id': 'r', 'request_id': 'r',
                'user_id': 'alice', 'channel': 'chat', 'execution_attempt_id': 'a'}
    archive_request(paths, record, paths.done, 'r', terminal_response=terminal)
    live_gateway['start']()
    agent = SimpleNamespace(config=SimpleNamespace(gateway_port=live_gateway['port'],
        gateway_request_timeout=10, gateway_auth_token=''))
    import agent_py_agent.agent.common.authenticated_http as transport
    actual_open = transport.open_authenticated_request
    opened = []
    class Disconnect:
        def __init__(self, raw):
            self.raw, self.frames = raw, 0
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.raw.close()
        def readline(self, size):
            if self.frames >= 5:
                self.raw.close()
                return b''
            line = self.raw.readline(size)
            if line in (b'\n', b'\r\n'):
                self.frames += 1
            return line
    def opener(request, *, timeout):
        opened.append(request)
        raw = actual_open(request, timeout=timeout)
        return Disconnect(raw) if reconnect and len(opened) == 1 else raw
    monkeypatch.setattr(transport, 'open_authenticated_request', opener)
    seen = []
    def consume(event):
        result = turn.on_gateway_event(event)
        if event.get('kind') == 'assistant_text_delta':
            seen.append(_visible(runtime))
        return result
    observer = NetworkGatewayObserver(agent, f"http://127.0.0.1:{live_gateway['port']}")
    result = observer(GatewayChunkPollRequest(gateway_chunk_path(paths, 'r'), paths.terminal / 'r.json',
        time.time() + 10, lambda _: None, [0], [0], on_event=consume))
    assert result['ok'] is True
    assert all(secret not in frame for frame in seen)
    if level == 'stream':
        assert seen and seen[0], '真实公开delta未进入TUI候选'
        if reconnect:
            assert len(opened) >= 2
            assert opened[1].get_header('Last-event-id')
    else:
        assert not seen
    assert _visible(runtime) == (text if level == 'stream' else '')
    runtime.complete_turn('r', TuiTurnSummary(response_text=text))
    assert _visible(runtime) == text
    assert not runtime.store.snapshot().active_blocks


def _delta(turn, call, seq, text, **extra):
    return turn.on_gateway_event({'kind': 'assistant_text_delta', 'level': 'stream',
        'model_call_id': call, 'seq': seq, 'text': text, **extra})


def test_replay_retry_call_switch_gap_and_final_correction():
    runtime = TuiRuntime('seq')
    turn = runtime.begin_turn('r')
    assert _delta(turn, 'one', 0, '候选')
    assert _delta(turn, 'one', 0, '候选')
    assert _visible(runtime) == '候选'
    assert turn.on_gateway_event({'kind': 'assistant_text_reset', 'model_call_id': 'one', 'reason': 'retry'})
    assert _visible(runtime) == ''
    assert _delta(turn, 'one', 0, '重试')
    assert _visible(runtime) == '重试'
    assert _delta(turn, 'two', 0, '新调用')
    assert _visible(runtime) == '新调用'
    _delta(turn, 'one', 1, '迟到')
    assert _visible(runtime) == '新调用'
    assert not turn.on_gateway_event({'kind': 'assistant_final', 'model_source': {'call_id': 'one'}, 'text': '旧调用迟到提交'})
    _delta(turn, 'two', 2, '欠序')
    assert _visible(runtime) == ''
    # 显示流不完整不影响完整已采纳正文。
    final = {'kind': 'assistant_final', 'model_source': {'call_id': 'two'}, 'text': '权威终稿'}
    assert turn.on_gateway_event(final)
    assert turn.on_gateway_event(final)
    runtime.complete_turn('r', TuiTurnSummary(response_text='权威终稿'))
    assert _visible(runtime) == '权威终稿'
    assert not turn.on_gateway_event(final)


def test_steer_stop_new_next_turn_rejects_late_and_foreign_frames():
    runtime = TuiRuntime('lifecycle')
    turn = runtime.begin_turn('r')
    _delta(turn, 'call', 0, '未采纳')
    turn.begin_active_turn_input(('steer-id',))
    assert _visible(runtime) == ''
    _delta(turn, 'call', 1, '迟到')
    assert _visible(runtime) == ''
    _delta(turn, 'next-call', 0, '待取消')
    runtime.complete_turn('r', TuiTurnSummary(response_text='', interrupted=True))
    assert _visible(runtime) == ''
    assert not _delta(turn, 'next-call', 1, '停止后迟到')
    runtime.publish_conversation_reset('new-thread')
    following = runtime.begin_turn('r2')
    assert not _delta(following, 'foreign', 0, '外线程', thread_id='old-thread')
    assert not _delta(following, 'foreign', 0, '外请求', request_id='r')
    _delta(following, 'fresh', 0, '当前候选', thread_id='new-thread')
    assert _visible(runtime) == '当前候选'
    runtime.complete_turn('r2', TuiTurnSummary(response_text='下一轮终稿'))
    assert _visible(runtime) == '下一轮终稿'
    assert not _delta(turn, 'late', 0, '新建后迟到')


def test_done_does_not_commit_and_committed_commentary_replaces_candidate():
    runtime = TuiRuntime('done')
    turn = runtime.begin_turn('r')
    _delta(turn, 'call', 0, '待校正')
    turn.on_gateway_event({'kind': 'assistant_text_done', 'model_call_id': 'call', 'seq': 0})
    assert not runtime.store.snapshot().stable_blocks
    _delta(turn, 'call', 1, 'done后迟到')
    assert _visible(runtime) == '待校正'
    commentary = {'kind': 'assistant_commentary', 'model_source': {'call_id': 'call'},
                  'text': '完整已采纳说明', 'replace_candidate': True}
    turn.on_gateway_event(commentary)
    turn.on_gateway_event(commentary)
    assert _visible(runtime) == '完整已采纳说明'
    runtime.complete_turn('r', TuiTurnSummary(response_text='终答'))
    assert _visible(runtime) == '完整已采纳说明终答'


def test_ten_tuis_bound_candidate_storage_and_cumulative_work():
    # 每个TUI拥有自己的有界候选；预算耗尽撤回，不能无限复制增长正文。
    for index in range(10):
        runtime = TuiRuntime(f'bounded-{index}')
        turn = runtime.begin_turn('r')
        text = '文' * 16384
        for seq in range(100):
            _delta(turn, 'call', seq, text)
        assert len(turn._assistant_text) <= 1_048_576
        assert not turn._model_candidate_text
        assert turn._public_text_calls['call'] == -1
        assert _visible(runtime) == ''
        runtime.complete_turn('r', TuiTurnSummary(response_text='完成'))
        assert _visible(runtime) == '完成'


def test_public_stream_never_accepts_raw_or_missing_stream_level():
    runtime = TuiRuntime('raw')
    turn = runtime.begin_turn('r')
    turn.on_gateway_event({'kind': 'model_delta', 'text': '未净化秘密'})
    assert not turn.on_gateway_event({'kind': 'assistant_text_delta', 'model_call_id': 'x',
        'seq': 0, 'text': '非stream'})
    assert _visible(runtime) == ''


@pytest.mark.parametrize('reason', ['cancel', 'superseded', 'tool_call'])
def test_reset_retracts_candidate_and_only_tool_boundary_can_commit(reason):
    runtime = TuiRuntime('reset')
    turn = runtime.begin_turn('r')
    _delta(turn, 'call', 0, '撤回')
    turn.on_gateway_event({'kind': 'assistant_text_reset', 'model_call_id': 'call', 'reason': reason})
    assert _visible(runtime) == ''
    event = {'kind': 'assistant_commentary', 'model_source': {'call_id': 'call'}, 'text': '已采纳工具说明'}
    accepted = turn.on_gateway_event(event)
    assert bool(accepted) is (reason == 'tool_call')
    assert _visible(runtime) == ('已采纳工具说明' if reason == 'tool_call' else '')


def test_final_adoption_reuses_candidate_block_and_summary_freezes_once():
    runtime = TuiRuntime('same-block')
    turn = runtime.begin_turn('r')
    _delta(turn, 'call', 0, '候选')
    block_id = turn._assistant_block_id
    event = {'kind': 'assistant_final', 'model_source': {'call_id': 'call'}, 'text': '校正终稿'}
    turn.on_gateway_event(event)
    turn.on_gateway_event(event)
    assert turn._assistant_block_id == block_id
    assert _visible(runtime) == '校正终稿'
    assert not runtime.store.snapshot().stable_blocks
    runtime.complete_turn('r', TuiTurnSummary(response_text='校正终稿'))
    blocks = [block for block in runtime.store.snapshot().stable_blocks if block.role == 'assistant']
    assert len(blocks) == 1 and blocks[0].block_id == block_id


@pytest.mark.parametrize('retired', [True, False])
def test_real_wire_rejected_committed_source_never_reaches_plain_fallback(live_gateway, retired):  # noqa: F811
    paths = live_gateway['paths']
    runtime = TuiRuntime('rejected-wire')
    turn = runtime.begin_turn('r')
    writer = BufferedChunkStreamWriter(gateway_chunk_path(paths, 'r'), text_delta_holdback_chars=8)
    writer.set_verbose_level('stream')
    writer.bind_model_call('old')('原候选。' * 100)
    if retired:
        writer.reset_model_text('superseded', model_call_id='old')
    else:
        # 第一次真实采纳允许显示；后续同来源重放在新call活动时必须拒绝。
        writer.commit_model_response({'schema': 'assistant_messages.v1', 'source': {'call_id': 'old'},
            'boundary': 'tools', 'messages': [{'text': '已采纳旧说明'}], 'final_text': ''})
    writer.bind_model_call('new')('当前候选。' * 100)
    if retired:
        writer.commit_model_response({'schema': 'assistant_messages.v1', 'source': {'call_id': 'old',
            'run_id': 'must-not-leak'}, 'boundary': 'tools',
            'messages': [{'text': '取消后迟到说明'}], 'final_text': ''})
    writer.flush()
    if not retired:
        rows = gateway_chunk_path(paths, 'r').read_text().splitlines()
        genuine = next(row for row in rows if json.loads(row).get('kind') == 'assistant_commentary')
        with gateway_chunk_path(paths, 'r').open('a') as handle:
            handle.write(genuine + '\n')
    record = paths.processing / 'r.json'
    record.write_text(json.dumps({'id': 'r', 'execution_attempt_id': 'a', 'user_id': 'alice', 'channel': 'chat'}))
    archive_request(paths, record, paths.done, 'r', terminal_response={'ok': True, 'response': '权威终稿',
        'id': 'r', 'request_id': 'r', 'user_id': 'alice', 'channel': 'chat', 'execution_attempt_id': 'a'})
    live_gateway['start']()
    agent = SimpleNamespace(config=SimpleNamespace(gateway_port=live_gateway['port'],
        gateway_request_timeout=10, gateway_auth_token=''))
    rejected, fallback = [], []
    def consume(event):
        result = turn.on_gateway_event(event)
        if not result and event['kind'] == 'assistant_commentary':
            rejected.append(event)
        return result
    observer = NetworkGatewayObserver(agent, f"http://127.0.0.1:{live_gateway['port']}")
    result = observer(GatewayChunkPollRequest(gateway_chunk_path(paths, 'r'), paths.terminal / 'r.json',
        time.time() + 10, fallback.append, [0], [0], on_event=consume))
    assert result['ok'] and rejected
    assert rejected[-1]['model_source'] == {'call_id': 'old'}
    assert not fallback
    assert '取消后迟到说明' not in _visible(runtime)
    runtime.complete_turn('r', TuiTurnSummary(response_text=result['response']))
    assert _visible(runtime) == ('' if retired else '已采纳旧说明') + '权威终稿'


def test_committed_display_identity_window_has_fixed_capacity_without_truncating_final():
    runtime = TuiRuntime('bounded-identities')
    turn = runtime.begin_turn('r')
    _delta(turn, 'call', 0, '候选')
    for index in range(600):
        turn.on_gateway_event({'kind': 'assistant_final', 'model_source': {'call_id': 'call'},
            'text': f'完整采纳版本{index}'})
    assert len(turn._public_text_commits) == 256
    assert _visible(runtime) == '完整采纳版本599'
    final = '完整终稿。' * 300_000
    runtime.complete_turn('r', TuiTurnSummary(response_text=final))
    assert _visible(runtime) == final


@pytest.mark.parametrize('boundary', ['stop', 'steer', 'finish'])
@pytest.mark.parametrize('stream_first', [True, False])
def test_event_controlled_stream_and_control_share_adapter_lock(monkeypatch, boundary, stream_first):
    import threading

    import agent_py_agent.cli.chat_parts.tui_runtime as module

    runtime = TuiRuntime('interleaving')
    turn = runtime.begin_turn('r')
    _delta(turn, 'call', 0, '已有候选')
    entered, release, attempted, finished = (threading.Event() for _ in range(4))
    failures = []
    original = module._consume_public_text_event
    def consume(adapter, payload):
        if stream_first:
            entered.set()
            assert release.wait(5)
        return original(adapter, payload)
    monkeypatch.setattr(module, '_consume_public_text_event', consume)
    def control():
        attempted.set()
        if boundary == 'steer':
            turn.begin_active_turn_input(('actual-steer',))
        else:
            runtime.complete_turn('r', TuiTurnSummary(response_text='完整终稿' if boundary == 'finish' else '',
                interrupted=boundary == 'stop'))
        finished.set()
    def guarded(operation):
        try:
            operation()
        except BaseException as exc:
            failures.append(exc)
    def emit():
        if not stream_first:
            attempted.set()
        _delta(turn, 'call', 1, '交错候选')
    def held_control():
        with turn._lock:
            control()
            entered.set()
            assert release.wait(5)
    first = threading.Thread(target=lambda: guarded(emit if stream_first else held_control))
    second = threading.Thread(target=lambda: guarded(control if stream_first else emit))
    try:
        first.start()
        assert entered.wait(5)
        attempted.clear()
        second.start()
        assert attempted.wait(5)
        # 第一方明确持有原adapter锁；无需靠睡眠猜测交错点。
        acquired = turn._lock.acquire(blocking=False)
        if acquired:
            turn._lock.release()
        assert not acquired
        if stream_first:
            assert not finished.is_set()
        release.set()
        first.join(5)
        second.join(5)
        assert not first.is_alive() and not second.is_alive() and not failures
        assert _visible(runtime) == ('完整终稿' if boundary == 'finish' else '')
        _delta(turn, 'call', 2, '晚到不能复活')
        assert _visible(runtime) == ('完整终稿' if boundary == 'finish' else '')
    finally:
        release.set()
        first.join(5)
        if second.ident is not None:
            second.join(5)


@pytest.mark.parametrize('kind', ['assistant_final', 'assistant_commentary'])
def test_explicit_rejection_never_falls_back_but_optional_observer_keeps_plain_contract(kind):
    from agent_py_agent.agent.gateway_parts.response_renderer import StreamEventDisposition
    from agent_py_agent.cli.chat_parts.gateway_client import _emit_chunk_line

    row = json.dumps({'kind': kind, 'text': '完整公开正文'})
    plain = []
    assert _emit_chunk_line(row, plain.append, lambda event: StreamEventDisposition.REJECTED) == (1, 0)
    assert not plain
    _emit_chunk_line(row, plain.append, lambda event: None)
    _emit_chunk_line(row, plain.append)
    _emit_chunk_line(row, plain.append, lambda event: False)
    assert plain == ['完整公开正文'] * 3


def test_tool_progress_before_full_adoption_only_retracts_tentative_text():
    runtime = TuiRuntime('tool-adoption')
    turn = runtime.begin_turn('r')
    _delta(turn, 'call', 0, '工具前候选')
    turn.write_progress({'phase': 'started', 'tool': 'read_file', 'round': 1, 'call_index': 0})
    assert _visible(runtime) == ''
    assert turn.on_gateway_event({'kind': 'assistant_commentary', 'model_source': {'call_id': 'call'},
        'text': '完整已采纳工具说明'}) is True
    assert _visible(runtime) == '完整已采纳工具说明'


def test_superseded_predecessor_does_not_reject_next_tool_call_without_any_delta():
    runtime = TuiRuntime('superseded-next')
    turn = runtime.begin_turn('r')
    _delta(turn, 'old', 0, '旧候选')
    turn.on_gateway_event({'kind': 'assistant_text_reset', 'model_call_id': 'old', 'reason': 'superseded'})
    assert not turn._public_text_call
    # 工具调用可没有任何正文delta；宿主仍先发该call的tool reset再采纳完整说明。
    turn.on_gateway_event({'kind': 'assistant_text_reset', 'model_call_id': 'next', 'reason': 'tool_call'})
    assert turn.on_gateway_event({'kind': 'assistant_commentary', 'model_source': {'call_id': 'next'},
        'text': '新工具调用完整说明'}) is True
    assert _visible(runtime) == '新工具调用完整说明'
    assert not turn.on_gateway_event({'kind': 'assistant_commentary', 'model_source': {'call_id': 'old'},
        'text': '旧调用不可复活'})


# ============================================================================
# Source: agent_py_agent/tests/test_r9_asset_clean_json.py
# ============================================================================

def test_clean_json_exact_scalars_and_strings_keep_redaction_without_generic_dispatch(monkeypatch):
    payload = {'safe': [None, True, False, 42, -3, 0.5, 'ordinary'],
               'apiKey': 'must-not-survive', 'auth-token': 'must-not-survive',
               'notes': ['Bearer synthetic-credential', '--password=synthetic-credential'],
               'nested': {'tokens': 123, 'token_count': 456, 'credential_ref': 'vault/reference'}}
    expected = descriptor.clean_json(payload)
    calls = []
    from agent_py_agent.agent.common.log_redaction import redact_sensitive_text
    original = redact_sensitive_text

    def text(value, **kwargs):
        calls.append(value)
        return original(value, **kwargs)

    def unexpected(*args, **kwargs):
        raise AssertionError('JSON scalar dispatched through generic object redactor')

    monkeypatch.setattr(descriptor, 'redact_sensitive_text', text, raising=False)
    monkeypatch.setattr(descriptor, 'redact_sensitive_value', unexpected)
    actual = descriptor.clean_json(payload)
    assert actual == expected
    assert actual['safe'] == payload['safe']
    assert 'apiKey' not in actual and 'auth-token' not in actual
    assert actual['nested']['tokens'] == 123 and actual['nested']['token_count'] == 456
    assert 'synthetic-credential' not in repr(actual)
    assert 'must-not-survive' not in repr(actual)
    assert len(calls) == 4  # 每个保留的字符串恰好经过原文本净化口。
    actual['safe'].append('changed')
    assert len(payload['safe']) == 7


@pytest.mark.parametrize('value', [float('nan'), float('inf'), b'not-json', {1: 'bad-key'}, object()])
def test_clean_json_still_rejects_non_json_values(value):
    with pytest.raises(ValueError):
        descriptor.clean_json({'nested': value})


def test_descriptor_serialization_rechecks_mutated_nested_secrets_across_threads():
    row = descriptor.AssetDescriptor('safe-asset', 'skill', 'Safe', metadata={'items': [{'note': 'first'}]})
    row.metadata['items'][0].update({'note': 'Bearer synthetic-live-secret', 'api_key': 'synthetic-live-secret'})

    def read(_):
        result = row.to_dict()
        assert 'synthetic-live-secret' not in repr(result)
        assert 'api_key' not in result['metadata']['items'][0]
        result['metadata']['items'][0]['note'] = 'caller-local'
        return result

    with ThreadPoolExecutor(max_workers=17) as pool:
        results = list(pool.map(read, range(51)))
    assert len(results) == 51
    assert row.metadata['items'][0]['note'] == 'Bearer synthetic-live-secret'


# ============================================================================
# Source: agent_py_agent/tests/test_r9_background_restart_park.py
# ============================================================================

def _request(ctx, monkeypatch):
    ctx.context = replace(ctx.context, paths=gateway_paths(ctx.agent),
                          process_identity={'pid': os.getpid(), 'start_time': 'isolated'})
    ctx.context.paths.root.mkdir(parents=True, exist_ok=True)
    ctx.agent.config.gateway_restart_turn_wait_seconds = 0
    ctx.agent.config.gateway_restart_drain_timeout_seconds = .1
    monkeypatch.setattr(handover, 'log_gateway_event', lambda *a: None)
    restart_service.submit_restart_request(ctx.context.paths, target_pid=os.getpid(), requester={}, reason='',
        cooldown_seconds=0, operation='stop_for_update')


def test_restart_parks_original_background_claim_before_irreversible_cleanup(tmp_path, monkeypatch):
    ctx = _running(tmp_path)
    try:
        _request(ctx, monkeypatch)
        original = ctx.store.claims.load(ctx.thread.thread_id)
        report = handover.drain_for_requested_restart(ctx.context, ctx.context.process_identity)
        assert report is not None
        ctx.future.result(timeout=1)
        current = ctx.store.claims.load(ctx.thread.thread_id)
        assert current['claim_id'] == original['claim_id'] and current['status'] == 'cancelled'
        assert ctx.store.tasks.link_report(ctx.thread.thread_id, 'original-work')[0].status == 'active'
        signals = ctx.store.wakes.pending()
        assert signals
        state, error = ctx.store.wakes.attempts.state_report(signals[0].wake_signal_id)
        assert not error and state.uncounted_count == 0 and state.neutral_count in (None, 0)
        assert not ctx.context.background_shutdown.stopping
        assert ctx.context.background_shutdown.blockers() == []
    finally:
        restart_service.reset_restart_drain_state()
        _release(ctx)


def test_uncooperative_park_cancels_upgrade_and_reopens_only_new_slice_admission(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation import background_shutdown
    monkeypatch.setattr(background_shutdown, 'SHUTDOWN_WAIT_SECONDS', .05)
    ctx = _running(tmp_path, cooperative=False)
    try:
        _request(ctx, monkeypatch)
        assert handover.drain_for_requested_restart(ctx.context, ctx.context.process_identity) is None
        assert not ctx.context.background_shutdown.stopping and not restart_gate.tool_gate_closed()
        assert not ctx.future.done()
        with ctx.context.background_shutdown.observe(None):
            pass
        assert ctx.store.tasks.link_report(ctx.thread.thread_id, 'original-work')[0].status == 'active'
        assert ctx.store.wakes.pending()
    finally:
        restart_service.reset_restart_drain_state()
        _release(ctx)


def test_late_claim_after_cancelled_park_still_consumes_original_slice_exit_request(monkeypatch):
    import threading

    import pytest

    from agent_py_agent.agent.conversation import background_shutdown as shutdown
    monkeypatch.setattr(shutdown, 'SHUTDOWN_WAIT_SECONDS', .03)
    control = shutdown.BackgroundShutdown()
    entered, release = threading.Event(), threading.Event()
    rejected = []
    def original():
        with control.observe(None):
            entered.set()
            assert release.wait(5)
            try:
                shutdown.check_background_admission()
            except InterruptedError:
                rejected.append(True)
    worker = threading.Thread(target=original)
    worker.start()
    try:
        assert entered.wait(5)
        assert control.pause_for_restart()
        assert not control.restart_paused and not control.stopping
        # 旧片的退出意图不撤销，后继新片的准入已恢复。
        with control.observe(None):
            shutdown.check_background_admission()
        release.set()
        worker.join(5)
        assert not worker.is_alive() and rejected == [True]
        assert control.blockers() == []
        control.stop()
        control.resume_after_restart_cancel()
        with pytest.raises(InterruptedError):
            with control.observe(None):
                pass
    finally:
        release.set()
        worker.join(5)


def test_finished_unknown_refuses_park_without_interrupting_other_original_work(monkeypatch):
    from agent_py_agent.agent.conversation import background_shutdown as shutdown
    control = shutdown.BackgroundShutdown()
    item = shutdown.BackgroundSlice(control, None)
    item.finished = True
    control.slices.add(item)
    unknown = {'reason': 'background_operation_unsettled', 'ref': 'exact-original-claim'}
    monkeypatch.setattr(item, 'blockers', lambda: [unknown])
    monkeypatch.setattr(item, 'interrupt', lambda: (_ for _ in ()).throw(AssertionError('UNKNOWN cannot be interrupted away')))
    assert control.pause_for_restart() == [unknown]
    assert not control.restart_paused and not control.stopping
    assert item in control.slices


def test_only_host_park_cannot_push_neutral_boundary_into_quarantine():
    from agent_py_agent.agent.conversation import wake_poison as poison
    state = poison.WakePoisonState(uncounted_count=4, uncounted_since=1., neutral_count=4, neutral_since=1.)
    now = 100_000.
    ordinary = poison.next_poison_state(state,
        poison.WakeAttemptVerdict(poison.WAKE_VERDICT_NEUTRAL, poison.WAKE_REASON_RUN_CANCELLED), now=now)
    assert poison.quarantine_decision(ordinary) is not None
    parked = poison.next_poison_state(state,
        poison.WakeAttemptVerdict(poison.WAKE_VERDICT_NEUTRAL, poison.WAKE_REASON_HOST_RESTART_PARK), now=now)
    assert parked is state and poison.quarantine_decision(parked) is None


def test_host_park_store_preserves_old_neutral_window_without_stall_or_quarantine(tmp_path):
    from agent_py_agent.agent.conversation import wake_poison as poison
    from agent_py_agent.tests.test_wake_attempt_store import _raise, _store
    store = _store(tmp_path)
    signal = _raise(store)
    attempts = store.wakes.attempts
    ordinary = poison.WakeAttemptVerdict(poison.WAKE_VERDICT_NEUTRAL, poison.WAKE_REASON_RUN_CANCELLED)
    for n in range(5):
        attempts.record(signal, ordinary, now=float(n + 1))
    before, error = attempts.state_report(signal.wake_signal_id)
    assert not error and before.neutral_count == 5
    parked = attempts.record(signal,
        poison.WakeAttemptVerdict(poison.WAKE_VERDICT_NEUTRAL, poison.WAKE_REASON_HOST_RESTART_PARK), now=100_000.)
    assert parked.state == before and parked.decision is None and parked.stall_alert is None
    assert store.wakes.pending_one(signal.wake_signal_id) is not None
    # 原业务取消仍执行原neutral结案合同，宿主park不洗掉旧积累。
    next_ordinary = attempts.record(signal, ordinary, now=100_001.)
    assert next_ordinary.decision is not None


# ============================================================================
# Source: agent_py_agent/tests/test_r9_candidate_anchor_index.py
# ============================================================================

def seed(tmp_path, count=320):
    service = CandidateService(tmp_path / 'candidates.jsonl')
    service.observe_many([CandidateObservation(candidate_type='lesson' if i % 2 else 'hot_rule',
        content=f'公开合成经验{i}', subject_key=f'anchor.{i}', scope=MemoryScope('personal', 'personal') if i % 3 else MemoryScope('global', 'global'),
        origin='user_explicit', promotion_target='lesson' if i % 2 else 'hot', source_message_refs=({'message_id': f'm-{i}'},)) for i in range(count)])
    return service


def expected(service, scopes):
    selected, counts = [], {}
    for row in sorted(service.readonly_rows(statuses=STATUSES), key=lambda row: (row.last_observed_at, row.candidate_id), reverse=True):
        key = (row.scope['scope_type'], row.scope['scope_key'], row.candidate_type)
        if not eligible(row) or key[:2] not in scopes or counts.get(key, 0) == 3:
            continue
        selected.append(row.candidate_id)
        counts[key] = counts.get(key, 0) + 1
        if len(selected) == 12:
            break
    return selected


def mutate(service, operation):
    with module._quota_admission(service.quota_enforcer) as admission, service._locked_candidates():
        rows = service._load_unlocked()
        operation(rows)
        module._write_candidates_unlocked(service.path, rows, admission=admission)


def old_header(service):
    # 真实旧头：旧写者不具有anchor命名空间；正文和其它规范索引保持原内容。
    with module._quota_admission(service.quota_enforcer) as admission, service._locked_candidates():
        rows = service._load_unlocked()
        root = rows.root
        for key, _ in list(rows.index.items(root)):
            if key.startswith(('anchor/', 'anchorroot/')):
                root = rows.index.remove(root, key)
        writes, cleanup = rows.index.finish(root)
        for name, raw in writes.items():
            (service.path.parent / (service.path.name + '.records') / name).write_bytes(raw)
        service.path.write_text(candidate_ledger.manifest_text(root, len(rows)), encoding='utf-8')


def test_exact_global_order_ties_and_warm_body_read_cap(tmp_path, monkeypatch):
    service = seed(tmp_path)
    scopes = {('personal', 'personal'), ('global', 'global')}
    wanted = expected(service, scopes)
    reads = []
    original = candidate_ledger._read_candidate_row
    def read(*args, **kwargs):
        reads.append(True)
        return original(*args, **kwargs)
    monkeypatch.setattr(candidate_ledger, '_read_candidate_row', read)
    assert [row.candidate_id for row in service.anchor_rows(scopes)] == wanted
    assert len(reads) == 12
    assert service.anchor_rows({('session', 'foreign')}) == ()


def test_status_scope_type_and_time_updates_remove_old_rank(tmp_path):
    service = seed(tmp_path, 80)
    scopes = {('personal', 'personal'), ('global', 'global')}
    top = service.anchor_rows(scopes)[0].candidate_id
    def change(rows):
        position = rows.index_for(top)
        rows[position] = replace(rows[position], status='rejected', status_history=[*rows[position].status_history, module._status_event('rejected', actor='test', note='', at=rows[position].updated_at)])
        rows[1] = replace(rows[1], proposed_action='replace', target_entry_id='existing-target', target_entry_version=1)
        rows[0] = replace(rows[0], last_observed_at='9999-01-01T00:00:00+00:00',
                          scope=MemoryScope('personal', 'personal').to_dict(), candidate_type='lesson', promotion_target='lesson')
    mutate(service, change)
    assert [row.candidate_id for row in service.anchor_rows(scopes)] == expected(service, scopes)
    assert top not in [row.candidate_id for row in service.anchor_rows(scopes)]
    # 原全表删除规划重建同一唯一头，不能留下已删除的ghost锚点。
    with module._quota_admission(service.quota_enforcer) as admission, service._locked_candidates():
        rows = list(service._load_unlocked())[1:]
        module._write_candidates_unlocked(service.path, rows, admission=admission)
    assert [row.candidate_id for row in service.anchor_rows(scopes)] == expected(service, scopes)


def test_old_head_build_is_chunked_resumable_and_concurrent_mutation_is_seen(tmp_path, monkeypatch):
    service = seed(tmp_path, 300)
    old_header(service)
    original = module._write_candidates_unlocked
    steps = []
    def interrupted(path, rows, **kwargs):
        if hasattr(rows, 'anchor_build_end'):
            steps.append(rows.anchor_build_end)
            if len(steps) == 2:
                raise RuntimeError('restart between committed index chunks')
        return original(path, rows, **kwargs)
    monkeypatch.setattr(module, '_write_candidates_unlocked', interrupted)
    scopes = {('personal', 'personal'), ('global', 'global')}
    with pytest.raises(RuntimeError):
        service.anchor_rows(scopes)
    header = candidate_ledger.read_manifest(service.path)
    rows = candidate_ledger.CandidateRows(service.path, header)
    assert rows.index.get(rows.root, 'anchor/progress') == 128
    assert rows.index.get(rows.root, 'anchor/version') == 0
    monkeypatch.setattr(module, '_write_candidates_unlocked', original)
    mutate(service, lambda rows: rows.__setitem__(0, replace(rows[0], last_observed_at='9999-01-01T00:00:00+00:00')))
    restarted = CandidateService(service.path)
    assert [row.candidate_id for row in restarted.anchor_rows(scopes)] == expected(service, scopes)
    assert steps == [128, 256]


def test_bad_anchor_node_is_not_empty_result(tmp_path):
    service = seed(tmp_path, 20)
    header = candidate_ledger.read_manifest(service.path)
    rows = candidate_ledger.CandidateRows(service.path, header)
    reference = rows.index.get(rows.index.get(rows.root, 'anchor/groups'), group_key({'scope_type': 'personal', 'scope_key': 'personal'}, 'lesson'))
    (service.path.parent / (service.path.name + '.records') / reference['ref']['ref']).write_bytes(b'broken')
    with pytest.raises((ValueError, OSError)):
        service.anchor_rows({('personal', 'personal')})


def test_index_build_quota_failure_keeps_original_head_and_all_rows(tmp_path, monkeypatch):
    from contextlib import contextmanager
    service = seed(tmp_path, 30)
    old_header(service)
    before = service.path.read_bytes()
    class Denied:
        def check(self, changes):
            assert any(str(change.path).endswith('.json') for change in changes)
            raise PermissionError('quota denied before immutable-node publication')
    @contextmanager
    def denied(*args):
        yield Denied()
    monkeypatch.setattr(module, '_quota_admission', denied)
    with pytest.raises(PermissionError):
        service.anchor_rows({('personal', 'personal')})
    assert service.path.read_bytes() == before
    assert len(service.readonly_rows()) == 30


def test_writer_can_advance_between_build_chunks_and_index_keeps_new_state(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from threading import Event, Thread, current_thread
    service = seed(tmp_path, 270)
    old_header(service)
    entered, resume = Event(), Event()
    original = module._quota_admission
    results, errors = [], []
    @contextmanager
    def gated(*args):
        with original(*args) as admission:
            yield admission
        if current_thread().name == 'anchor-builder' and not entered.is_set():
            entered.set()
            assert resume.wait(5)
    monkeypatch.setattr(module, '_quota_admission', gated)
    scopes = {('personal', 'personal'), ('global', 'global')}
    def build():
        try:
            results.extend(service.anchor_rows(scopes))
        except BaseException as error:
            errors.append(error)
    worker = Thread(target=build, name='anchor-builder')
    worker.start()
    try:
        assert entered.wait(5)
        mutate(service, lambda rows: rows.__setitem__(0, replace(rows[0], last_observed_at='9999-01-01T00:00:00+00:00')))
    finally:
        resume.set()
        worker.join(10)
    assert not worker.is_alive() and not errors
    assert [row.candidate_id for row in results] == expected(service, scopes)


def test_empty_source_does_not_create_new_persistence(tmp_path):
    service = CandidateService(tmp_path / 'missing' / 'candidates.jsonl')
    assert service.anchor_rows({('personal', 'personal')}) == ()
    assert not service.path.exists()


def test_original_jsonl_converts_without_losing_records_or_anchor_order(tmp_path):
    import json
    service = seed(tmp_path, 31)
    scopes = {('personal', 'personal'), ('global', 'global')}
    wanted = expected(service, scopes)
    records = [row.to_record() for row in service.readonly_rows()]
    service.path.write_text('\n'.join(json.dumps(record) for record in records), encoding='utf-8')
    assert [row.candidate_id for row in service.anchor_rows(scopes)] == wanted
    assert len(service.readonly_rows()) == len(records)
    assert candidate_ledger.read_manifest(service.path) is not None


def test_existing_row_forged_as_another_group_maximum_is_rejected(tmp_path, monkeypatch):
    service = seed(tmp_path, 30)
    original = candidate_ledger.PlannedIndex.get
    foreign = next(row for row in service.readonly_rows() if row.scope['scope_type'] == 'global')
    key = group_key({'scope_type': 'personal', 'scope_key': 'personal'}, 'lesson')
    def forged(index, root, requested, *args):
        value = original(index, root, requested, *args)
        if requested == key and value is not None:
            return {**value, 'maximum': {'anchor_rank': [foreign.last_observed_at, foreign.candidate_id], 'candidate_id': foreign.candidate_id}}
        return value
    monkeypatch.setattr(candidate_ledger.PlannedIndex, 'get', forged)
    with pytest.raises(ValueError):
        service.anchor_rows({('personal', 'personal')})


def test_old_writer_invalidates_seal_and_restart_uses_new_group_generation(tmp_path, monkeypatch):
    from agent_py_agent.agent.memory_store import candidate_anchor_index
    service = seed(tmp_path, 270)
    scopes = {('personal', 'personal'), ('global', 'global')}
    before = service.anchor_rows(scopes)[0]
    # 旧协议写者仍正常更新规范行/状态索引，但不理解anchor维护或seal。
    with monkeypatch.context() as legacy:
        legacy.setattr(candidate_anchor_index, 'plan_anchors', lambda rows, root, changes: root)
        mutate(service, lambda rows: rows.__setitem__(rows.index_for(before.candidate_id), replace(before,
            status='rejected', status_history=[*before.status_history,
                module._status_event('rejected', actor='test', note='', at=before.updated_at)])))
    rows = service._load_unlocked()
    assert not candidate_anchor_index.source_valid(rows)
    original = module._write_candidates_unlocked
    steps = []
    def interrupted(path, rows, **kwargs):
        if hasattr(rows, 'anchor_build_end'):
            steps.append(rows.anchor_build_end)
            if len(steps) == 2:
                raise RuntimeError('cold restart after first rebuilt chunk')
        return original(path, rows, **kwargs)
    monkeypatch.setattr(module, '_write_candidates_unlocked', interrupted)
    with pytest.raises(RuntimeError):
        service.anchor_rows(scopes)
    rows = service._load_unlocked()
    generation = rows.index.get(rows.root, 'anchor/generation')
    assert generation != 'initial' and rows.index.get(rows.root, 'anchor/progress') == 128
    assert candidate_anchor_index.source_valid(rows) and not candidate_anchor_index.ready(rows)
    monkeypatch.setattr(module, '_write_candidates_unlocked', original)
    restarted = CandidateService(service.path)
    assert [row.candidate_id for row in restarted.anchor_rows(scopes)] == expected(service, scopes)
    assert before.candidate_id not in [row.candidate_id for row in restarted.anchor_rows(scopes)]
    assert restarted._load_unlocked().index.get(restarted._load_unlocked().root, 'anchor/generation') == generation


def test_new_mutation_cannot_reseal_stale_old_writer_projection_as_complete(tmp_path, monkeypatch):
    from agent_py_agent.agent.memory_store import candidate_anchor_index
    service = seed(tmp_path, 160)
    scopes = {('personal', 'personal'), ('global', 'global')}
    seen = {row.candidate_id for row in service.anchor_rows(scopes)}
    target = next(row for row in service.readonly_rows() if row.candidate_id not in seen)
    with monkeypatch.context() as legacy:
        legacy.setattr(candidate_anchor_index, 'plan_anchors', lambda rows, root, changes: root)
        mutate(service, lambda rows: rows.__setitem__(rows.index_for(target.candidate_id),
            replace(target, last_observed_at='2099-01-01T00:00:00+00:00')))
    mutate(service, lambda rows: rows.__setitem__(0, replace(rows[0], review_note='new writer mutation')))
    rows = service._load_unlocked()
    assert candidate_anchor_index.source_valid(rows) and not candidate_anchor_index.ready(rows)
    assert [row.candidate_id for row in service.anchor_rows(scopes)] == expected(service, scopes)
    assert service.anchor_rows(scopes)[0].candidate_id == target.candidate_id


def test_repeated_legacy_writer_changes_yield_and_later_recover(tmp_path, monkeypatch):
    from contextlib import contextmanager

    from agent_py_agent.agent.memory_store import candidate_anchor_index
    service = seed(tmp_path, 270)
    old_header(service)
    original = module._quota_admission
    interrupted = []
    @contextmanager
    def admission(*args):
        with original(*args) as value:
            yield value
        if not interrupted:
            interrupted.append(True)
            # 片锁已释放，旧写者仍可提交；不能因此在同一调用里无限重启全史。
            with monkeypatch.context() as legacy:
                legacy.setattr(candidate_anchor_index, 'plan_anchors', lambda rows, root, changes: root)
                mutate(service, lambda rows: rows.__setitem__(0,
                    replace(rows[0], last_observed_at='2099-01-01T00:00:00+00:00')))
    monkeypatch.setattr(module, '_quota_admission', admission)
    with pytest.raises(RuntimeError, match='source changed again'):
        service.anchor_rows({('personal', 'personal'), ('global', 'global')})
    assert interrupted
    monkeypatch.setattr(module, '_quota_admission', original)
    scopes = {('personal', 'personal'), ('global', 'global')}
    assert [row.candidate_id for row in service.anchor_rows(scopes)] == expected(service, scopes)


def test_repeated_rollback_rebuild_keeps_one_reachable_group_catalog(tmp_path, monkeypatch):
    from agent_py_agent.agent.memory_store import candidate_anchor_index
    service = seed(tmp_path, 80)
    scopes = {('personal', 'personal'), ('global', 'global')}
    for year in (2091, 2092, 2093):
        with monkeypatch.context() as legacy:
            legacy.setattr(candidate_anchor_index, 'plan_anchors', lambda rows, root, changes: root)
            mutate(service, lambda rows: rows.__setitem__(0,
                replace(rows[0], last_observed_at=f'{year}-01-01T00:00:00+00:00')))
        assert [row.candidate_id for row in service.anchor_rows(scopes)] == expected(service, scopes)
        rows = service._load_unlocked()
        keys = [key for key, _ in rows.index.items(rows.root)]
        assert keys.count('anchor/groups') == 1
        assert not any(key.startswith('anchorroot/') for key in keys)
        catalog = rows.index.get(rows.root, 'anchor/groups')
        assert len(list(rows.index.items(catalog))) == 4


def test_seal_remains_stable_when_its_key_splits_a_full_hamt_leaf(tmp_path):
    from types import SimpleNamespace

    from agent_py_agent.agent.memory_store.candidate_anchor_index import seal_root, source_valid
    index = candidate_ledger.PlannedIndex(tmp_path / 'candidates.jsonl')
    root = None
    for position in range(8):
        root = index.put(root, f'ordinary/{position}', position)
    assert len(index._load(root)['entries']) == 8
    sealed = seal_root(index, root)
    assert 'children' in index._load(sealed)
    assert source_valid(SimpleNamespace(index=index, root=sealed))
    changed = index.put(sealed, 'ordinary/0', 999)
    assert not source_valid(SimpleNamespace(index=index, root=changed))
