"""显式维护的扫描预算：跳过、错误和非UUID目录均不可无限枚举。"""
import json
import os
import sys
from collections import OrderedDict
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation import display_archive as archive
from agent_py_agent.agent.conversation import display_archive_maintenance as maintenance


@pytest.mark.parametrize('operation', ['compact', 'prune', 'pinned', 'errors'])
def test_skipped_and_failed_archives_obey_scan_budget(tmp_path, operation):
    root = tmp_path / 'display_archives'
    root.mkdir()
    pins = set()
    for number in range(100):
        archive_id = f'{number:032x}'
        directory = root / archive_id
        directory.mkdir()
        pins.add(archive_id)
        if operation == 'errors':
            (directory / 'manifest.json').write_text('{broken')
        else:
            manifest = dict(schema=archive.DISPLAY_ARCHIVE_SCHEMA, archive_id=archive_id,
                            thread_id='thread-test', page_count=1, row_count=1, char_count=1)
            (directory / 'manifest.json').write_text(json.dumps(manifest))
    now = 2_000_000_000.0
    if operation == 'compact':
        result = maintenance.compact_display_archives(tmp_path, now=now, min_age_days=100000, max_archives=1)
    else:
        result = maintenance.prune_expired_display_archives(
            tmp_path, now=now, ttl_days=100000, max_archives=1,
            pinned_archive_ids=pins if operation == 'pinned' else None,
        )
    assert result['scanned'] == 64, result
    assert result['errors' if operation == 'errors' else 'skipped'] == 64, result
    assert result['compacted'] == result['expired'] == result['removed'] == 0
    assert len(list(root.iterdir())) == 100


# 用真实目录迭代器旁听yield数，结果scanned只统计UUID仍保持原合同。
def test_non_archive_entries_also_consume_enumeration_budget(tmp_path, monkeypatch):
    root = tmp_path / 'display_archives'
    root.mkdir()
    for number in range(100):
        (root / f'not-an-archive-{number}').mkdir()
    original, visited = os.scandir, []

    class Observed:
        def __enter__(self):
            self.entries = original(root)
            self.entries.__enter__()
            return self

        def __iter__(self):
            for entry in self.entries:
                visited.append(entry.name)
                yield entry

        def __exit__(self, *args):
            return self.entries.__exit__(*args)

    monkeypatch.setattr(maintenance.os, 'scandir', lambda _: Observed())
    result = maintenance.compact_display_archives(tmp_path, max_archives=1)
    assert len(visited) == 64
    assert result == dict(scanned=0, compacted=0, expired=0, removed=0, skipped=0, errors=0)


def test_success_limit_and_zero_budget_keep_archive_contract(tmp_path):
    store = SimpleNamespace(storage=SimpleNamespace(root=tmp_path), threads=SimpleNamespace(load=lambda _: True))
    refs = [archive.archive_display_rows(SimpleNamespace(conversation_store=store),
            thread_id='thread-test', rows=[{'text': '中🙂' * 10000}]) for _ in range(3)]
    before = [[archive.read_display_archive_page(store, ref, i) for i in range(ref['page_count'])] for ref in refs]
    assert maintenance.compact_display_archives(store, max_archives=0)['scanned'] == 0
    result = maintenance.compact_display_archives(store, min_age_days=0, max_archives=1)
    assert result['compacted'] == result['scanned'] == 1
    assert [[archive.read_display_archive_page(store, ref, i) for i in range(ref['page_count'])] for ref in refs] == before


@pytest.mark.parametrize('maximum', [-1, True, 1.5])
def test_invalid_batch_limit_is_rejected(tmp_path, maximum):
    with pytest.raises(ValueError):
        maintenance.compact_display_archives(tmp_path, max_archives=maximum)


def _ordered_archives(tmp_path, count):
    """真实 producer 建档后按实际枚举顺序选前缀，不假定文件系统按创建顺序返回。"""
    store = SimpleNamespace(storage=SimpleNamespace(root=tmp_path), threads=SimpleNamespace(load=lambda _: True))
    refs = [archive.archive_display_rows(SimpleNamespace(conversation_store=store),
            thread_id='thread-test', rows=[{'text': f'归档-{number}'}]) for number in range(count)]
    root = tmp_path / 'display_archives'
    by_id = {ref['archive_id']: ref for ref in refs}
    with os.scandir(root) as entries:
        ordered = [(by_id[entry.name], root / entry.name) for entry in entries]
    for _, directory in ordered:
        os.utime(directory / 'manifest.json', (2_000_000_000.0 - 2 * 86400,) * 2)
    return store, ordered


def test_compact_display_archives_rotates_past_scan_budget_without_starvation(tmp_path):
    store, ordered = _ordered_archives(tmp_path, 80)
    before = [archive.read_display_archive_page(store, ref, 0) for ref, _ in ordered]
    compacted = 0
    for _ in range(85):
        result = maintenance.compact_display_archives(store, now=2_000_000_000.0, min_age_days=0, max_archives=1)
        assert result['errors'] == 0 and result['compacted'] <= 1 and result['scanned'] <= 64
        compacted += result['compacted']
        if compacted == 80:
            break
    assert compacted == 80
    assert all((directory / 'pages_bundle.json').is_file() for _, directory in ordered)
    assert not any((directory / '0.json').exists() for _, directory in ordered)
    assert [archive.read_display_archive_page(store, ref, 0) for ref, _ in ordered] == before
    assert len(list((tmp_path / 'display_archives').iterdir())) == 80


def test_prune_expired_archives_rotates_past_pinned_prefix(tmp_path):
    store, ordered = _ordered_archives(tmp_path, 80)
    pinned = {ref['archive_id'] for ref, _ in ordered[:64]}
    originals = [(directory / 'manifest.json').read_bytes() for _, directory in ordered[:64]]
    expired = 0
    for _ in range(20):
        result = maintenance.prune_expired_display_archives(
            store, now=2_000_000_000.0, ttl_days=1, max_archives=1, pinned_archive_ids=pinned)
        assert result['errors'] == 0 and result['expired'] <= 1 and result['scanned'] <= 64
        expired += result['expired']
    assert expired == 16
    assert all(json.loads((directory / 'manifest.json').read_text())['expired'] for _, directory in ordered[64:])
    assert not any((directory / '0.json').exists() for _, directory in ordered[64:])
    assert [(directory / 'manifest.json').read_bytes() for _, directory in ordered[:64]] == originals
    assert all((directory / '0.json').exists() for _, directory in ordered[:64])
    removed = 0
    for _ in range(20):
        result = maintenance.prune_expired_display_archives(
            store, now=2_000_000_000.0 + 31 * 86400, ttl_days=1, max_archives=1, pinned_archive_ids=pinned)
        assert result['errors'] == 0 and result['removed'] <= 1 and result['scanned'] <= 64
        removed += result['removed']
    assert removed == 16
    assert not any(directory.exists() for _, directory in ordered[64:])
    assert {path.name for path in (tmp_path / 'display_archives').iterdir()} == pinned


@pytest.mark.parametrize('operation', ['compact', 'prune'])
def test_maintenance_reaches_collectible_archive_150_of_200(tmp_path, operation):
    store, ordered = _ordered_archives(tmp_path, 200)
    now = 2_000_000_000.0
    for _, directory in ordered:
        os.utime(directory / 'manifest.json', (now,) * 2)
    _, target = ordered[149]
    os.utime(target / 'manifest.json', (now - 2 * 86400,) * 2)
    pinned = {ref['archive_id'] for ref, _ in ordered[:64]}
    results = []
    for _ in range(4):
        if operation == 'compact':
            result = maintenance.compact_display_archives(store, now=now, max_archives=1)
        else:
            result = maintenance.prune_expired_display_archives(
                store, now=now, ttl_days=1, max_archives=1, pinned_archive_ids=pinned)
        assert result['errors'] == 0 and result['scanned'] <= 64
        results.append(result)
        if result['compacted'] + result['expired']:
            break
    assert sum(result['compacted'] + result['expired'] for result in results) == 1
    assert len(results) == 3
    assert not (target / '0.json').exists()
    if operation == 'compact':
        assert (target / 'pages_bundle.json').is_file()
    else:
        assert json.loads((target / 'manifest.json').read_text())['expired'] is True
    assert all((directory / '0.json').is_file() for _, directory in ordered[:64])
    assert len(list((tmp_path / 'display_archives').iterdir())) == 200


def test_rotating_scan_wraps_with_one_shared_inspection_budget(tmp_path, monkeypatch):
    store, ordered = _ordered_archives(tmp_path, 80)
    original_manifest, visited = maintenance._manifest, []

    def observe(fd, archive_id):
        visited.append(archive_id)
        return original_manifest(fd, archive_id)

    monkeypatch.setattr(maintenance, '_manifest', observe)
    assert maintenance.compact_display_archives(store, now=2_000_000_000.0, min_age_days=100000, max_archives=1)['scanned'] == 64
    visited.clear()
    result = maintenance.compact_display_archives(store, now=2_000_000_000.0, min_age_days=100000, max_archives=1)
    assert result['scanned'] == result['skipped'] == 64
    assert visited == [ref['archive_id'] for ref, _ in ordered[64:] + ordered[:48]]

    for _, directory in ordered:
        os.utime(directory / 'manifest.json', (2_000_000_000.0,) * 2)
    os.utime(ordered[3][1] / 'manifest.json', (2_000_000_000.0 - 2 * 86400,) * 2)
    result = maintenance.compact_display_archives(store, now=2_000_000_000.0, max_archives=1)
    assert result['compacted'] == 1 and result['scanned'] == 36
    assert (ordered[3][1] / 'pages_bundle.json').is_file()
    visited.clear()
    maintenance.compact_display_archives(store, now=2_000_000_000.0, max_archives=1)
    assert visited[0] == ordered[4][0]['archive_id']
    assert len(visited) == 64


def test_rotating_scan_restarts_after_directory_shrinks_past_cursor(tmp_path):
    store, ordered = _ordered_archives(tmp_path, 80)
    assert maintenance.compact_display_archives(store, now=2_000_000_000.0, min_age_days=100000, max_archives=1)['scanned'] == 64
    for _, directory in ordered[10:]:
        for leaf in directory.iterdir():
            leaf.unlink()
        directory.rmdir()
    result = maintenance.compact_display_archives(store, now=2_000_000_000.0, max_archives=1)
    assert result['errors'] == 0 and result['compacted'] == result['scanned'] == 1
    assert (ordered[0][1] / 'pages_bundle.json').is_file()


def test_scan_cursors_are_isolated_by_directory_and_operation(tmp_path):
    store, ordered = _ordered_archives(tmp_path / 'first', 80)
    assert maintenance.compact_display_archives(store, now=2_000_000_000.0, max_archives=1)['compacted'] == 1
    pinned = {ref['archive_id'] for ref, _ in ordered[:64]}
    result = maintenance.prune_expired_display_archives(
        store, now=2_000_000_000.0, ttl_days=1, max_archives=1, pinned_archive_ids=pinned)
    assert result['expired'] == 0 and result['scanned'] == result['skipped'] == 64
    other_store, other_ordered = _ordered_archives(tmp_path / 'second', 2)
    result = maintenance.compact_display_archives(other_store, now=2_000_000_000.0, max_archives=1)
    assert result['compacted'] == result['scanned'] == 1
    assert (other_ordered[0][1] / 'pages_bundle.json').is_file()
    assert (other_ordered[1][1] / '0.json').is_file()


def test_rotating_scan_adjusts_cursor_for_removal_during_wrap(tmp_path):
    store, ordered = _ordered_archives(tmp_path, 80)
    now = 2_000_000_000.0
    assert maintenance.prune_expired_display_archives(store, now=now, ttl_days=1)['expired'] == 80
    pinned = {ref['archive_id'] for ref, _ in ordered}
    result = maintenance.prune_expired_display_archives(
        store, now=now + 31 * 86400, max_archives=1, pinned_archive_ids=pinned)
    assert result['scanned'] == result['skipped'] == 64
    pinned.difference_update(ref['archive_id'] for ref, _ in ordered[:2])
    result = maintenance.prune_expired_display_archives(
        store, now=now + 31 * 86400, max_archives=1, pinned_archive_ids=pinned)
    assert result['removed'] == 1 and result['scanned'] == 17 and result['errors'] == 0
    assert not ordered[0][1].exists()
    assert ordered[1][1].is_dir()
    result = maintenance.prune_expired_display_archives(
        store, now=now + 31 * 86400, max_archives=1, pinned_archive_ids=pinned)
    assert result['removed'] == result['scanned'] == 1 and result['errors'] == 0
    assert not ordered[1][1].exists()
    assert {path.name for path in (tmp_path / 'display_archives').iterdir()} == pinned


def test_scan_cursor_cache_is_bounded_and_refreshes_recent_directories(tmp_path, monkeypatch):
    cursors = OrderedDict()
    monkeypatch.setattr(maintenance, '_scan_cursors', cursors)
    roots = []
    for number in range(129):
        root = tmp_path / str(number) / 'display_archives'
        root.mkdir(parents=True)
        roots.append(root)
    for root in roots[:128]:
        assert maintenance.compact_display_archives(root, max_archives=1)['errors'] == 0
    keys = list(cursors)
    assert len(keys) == 128
    maintenance.compact_display_archives(roots[0], max_archives=1)
    assert next(reversed(cursors)) == keys[0]
    maintenance.compact_display_archives(roots[-1], max_archives=1)
    assert len(cursors) == 128 and keys[0] in cursors and keys[1] not in cursors
    assert all(offset == 0 for offset in cursors.values())
    before = list(cursors.items())
    assert maintenance.compact_display_archives(roots[1], max_archives=0)['scanned'] == 0
    assert list(cursors.items()) == before
    assert not any(list(root.iterdir()) for root in roots)


@pytest.mark.parametrize(('maximum', 'budget'), [(1, 64), (5, 80)])
def test_scan_budget_keeps_existing_multiplier_on_fresh_and_resumed_scans(tmp_path, maximum, budget):
    store, _ = _ordered_archives(tmp_path, 100)
    for _ in range(2):
        result = maintenance.compact_display_archives(
            store, now=2_000_000_000.0, min_age_days=100000, max_archives=maximum)
        assert result['scanned'] == result['skipped'] == budget
        assert result['errors'] == result['compacted'] == 0


# 模拟新进程：关闭并丢弃进程内活迭代器与内存游标，只剩父目录游标文件。
def _fresh_process():
    live = getattr(maintenance, '_live_scans', {})
    with maintenance._scan_cursor_lock:
        for scans, _, _ in live.values():
            scans.close()
        live.clear()
        maintenance._scan_cursors.clear()


def _young_archives(root, count, now):
    root.mkdir()
    for number in range(count):
        archive_id = f'{number:032x}'
        directory = root / archive_id
        directory.mkdir()
        manifest = dict(schema=archive.DISPLAY_ARCHIVE_SCHEMA, archive_id=archive_id,
                        thread_id='thread-test', page_count=1, row_count=1, char_count=1)
        (directory / 'manifest.json').write_text(json.dumps(manifest))
        os.utime(directory / 'manifest.json', (now, now))


# 同进程续扫接着读活迭代器，每次调用实际枚举不超过预算，不随游标偏移线性增长；一轮内每档恰检查一次。
def test_resumed_scan_enumeration_stays_within_budget_per_call(tmp_path, monkeypatch):
    root = tmp_path / 'display_archives'
    now = 2_000_000_000.0
    _young_archives(root, 300, now)
    original, counts, visited = os.scandir, [], []
    original_manifest = maintenance._manifest

    class Observed:
        def __init__(self, target):
            self.entries = original(target)

        def __enter__(self):
            self.entries.__enter__()
            return self

        def __iter__(self):
            return self

        def __next__(self):
            entry = next(self.entries)
            counts[-1] += 1
            return entry

        def __exit__(self, *args):
            return self.entries.__exit__(*args)

    def observe(fd, archive_id):
        visited.append(archive_id)
        return original_manifest(fd, archive_id)

    monkeypatch.setattr(maintenance.os, 'scandir', lambda target: Observed(target))
    monkeypatch.setattr(maintenance, '_manifest', observe)
    for _ in range(10):
        counts.append(0)
        result = maintenance.prune_expired_display_archives(tmp_path, now=now, max_archives=1)
        assert result['scanned'] == result['skipped'] == 64 and result['errors'] == 0
    assert counts == [64] * 10
    assert len(set(visited[:300])) == 300 and visited[300:] == visited[:340]


# 每轮新进程只能靠父目录游标文件续扫：80档、每轮1个名额也必须全部合页，且归档目录里不出现游标文件。
def test_fresh_process_rounds_resume_from_persisted_cursor(tmp_path):
    store, ordered = _ordered_archives(tmp_path, 80)
    compacted = 0
    for _ in range(85):
        _fresh_process()
        result = maintenance.compact_display_archives(store, now=2_000_000_000.0, min_age_days=0, max_archives=1)
        assert result['errors'] == 0 and result['scanned'] <= 64
        compacted += result['compacted']
    assert compacted == 80
    assert all((directory / 'pages_bundle.json').is_file() for _, directory in ordered)
    assert {path.name for path in (tmp_path / 'display_archives').iterdir()} == {ref['archive_id'] for ref, _ in ordered}
    saved = json.loads((tmp_path / maintenance._CURSOR_FILE).read_text())
    identity = [(tmp_path / 'display_archives').stat().st_dev, (tmp_path / 'display_archives').stat().st_ino]
    assert all(value[:2] == identity for value in saved.values())


# 父目录游标损坏或被换成链接时只放弃跨进程续点：不跟随链接、不改链接目标，维护照常且不计错误。
@pytest.mark.parametrize('damage', ['corrupt', 'symlink', 'other_root'])
def test_persisted_cursor_damage_falls_back_without_following_links(tmp_path, damage):
    store, ordered = _ordered_archives(tmp_path, 80)
    cursor = tmp_path / maintenance._CURSOR_FILE
    outside = tmp_path / 'outside.json'
    outside.write_text('{"keep":true}')
    if damage == 'corrupt':
        cursor.write_text('{broken')
    elif damage == 'symlink':
        cursor.symlink_to(outside)
    else:
        operation = 'compact_display_archives.<locals>.action'
        cursor.write_text(json.dumps({operation: [0, 0, 70]}))
    _fresh_process()
    result = maintenance.compact_display_archives(store, now=2_000_000_000.0, min_age_days=0, max_archives=1)
    assert result['errors'] == 0 and result['compacted'] == result['scanned'] == 1
    assert (ordered[0][1] / 'pages_bundle.json').is_file()
    assert outside.read_text() == '{"keep":true}'
    if damage == 'symlink':
        assert cursor.is_symlink()
    else:
        assert json.loads(cursor.read_text())['compact_display_archives.<locals>.action'][2] == 1


_R7_NOW = 2_000_000_000.0


# 关闭并丢弃进程内全部续扫状态，各用例不继承活迭代器、等待键或调用序号。
def _r7_reset():
    with maintenance._scan_cursor_lock:
        for scans, _, _ in maintenance._live_scans.values():
            scans.close()
        maintenance._live_scans.clear()
        maintenance._scan_cursors.clear()
        getattr(maintenance, "_scan_recent", {}).clear()
        getattr(maintenance, "_scan_waiting", set()).clear()


@pytest.fixture(autouse=True)
def _r7_clean_scans():
    _r7_reset()
    yield
    _r7_reset()


def _r7_young_archives(root, count):
    root.mkdir(parents=True)
    for number in range(count):
        archive_id = f"{number:032x}"
        directory = root / archive_id
        directory.mkdir()
        manifest = dict(schema=archive.DISPLAY_ARCHIVE_SCHEMA, archive_id=archive_id,
                        thread_id="thread-test", page_count=1, row_count=1, char_count=1)
        (directory / "manifest.json").write_text(json.dumps(manifest))
        os.utime(directory / "manifest.json", (_R7_NOW, _R7_NOW))
    return root


# 非UUID占位文件同样消耗枚举预算，用于廉价构造大量多轮才能扫完的根。
def _r7_filler_root(root, count):
    root.mkdir(parents=True)
    for number in range(count):
        (root / f"filler-{number:04d}").touch()
    return root


# LLM: 透传真实os.scandir，只记每次维护调用的原始yield数和各根inode已见名称，不伪造目录或读取返回指标。
# 函数用途: 观察续扫的真实枚举量与每根是否完整覆盖。
@pytest.fixture
def _r7_observed(monkeypatch):
    original, calls, seen = os.scandir, [], {}

    class Observed:
        def __init__(self, target):
            self.entries = original(target)
            self.names = seen.setdefault(os.fstat(target).st_ino, set())

        def __enter__(self):
            self.entries.__enter__()
            return self

        def __iter__(self):
            return self

        def __next__(self):
            entry = next(self.entries)
            calls[-1] += 1
            self.names.add(entry.name)
            return entry

        def __exit__(self, *args):
            return self.entries.__exit__(*args)

    monkeypatch.setattr(maintenance.os, "scandir", Observed)
    return calls, seen


# LLM: 超过旧8个上限的root/operation键轮转维护（含同owner合页+TTL两键与目录尾回绕）；每次调用真实枚举恰为预算。
# 函数用途: 回归R6-ARCH-01同进程淘汰后无预算重放前缀。
@pytest.mark.parametrize(("owners", "both_operations"), [(9, False), (5, True), (8, True)])
def test_many_roots_and_operations_keep_every_call_within_enumeration_budget(tmp_path, _r7_observed, owners, both_operations):
    calls, _ = _r7_observed
    roots = [_r7_young_archives(tmp_path / str(index) / "display_archives", 200) for index in range(owners)]
    operations = [maintenance.compact_display_archives]
    if both_operations:
        operations.append(maintenance.prune_expired_display_archives)
    for _ in range(4):
        for root in roots:
            for operation in operations:
                calls.append(0)
                result = operation(root, now=_R7_NOW, max_archives=1)
                assert result["scanned"] == result["skipped"] == 64 and result["errors"] == 0
    assert calls == [64] * (4 * owners * len(operations))
    assert len(maintenance._live_scans) == owners * len(operations)


# LLM: 130个键超过128个活迭代器名额：占用者不被中途淘汰，满额新键只查目录头一窗且不推进游标，占用者走到目录尾即让出；
#   每次调用不超预算、FD名额有界，且每根最终完整扫过一轮。
# 函数用途: 回归名额不足时既不无界重放也不饿死后来的根。
def test_live_scan_slots_are_time_shared_beyond_capacity(tmp_path, _r7_observed):
    calls, seen = _r7_observed
    roots = [_r7_filler_root(tmp_path / str(index) / "display_archives", 100) for index in range(130)]
    for round_index in range(4):
        for root in roots:
            calls.append(0)
            assert maintenance.compact_display_archives(root, now=_R7_NOW, max_archives=1)["errors"] == 0
            assert len(maintenance._live_scans) <= 128
        if round_index == 0:
            assert len(maintenance._live_scans) == 128 and len(maintenance._scan_waiting) == 2
    assert max(calls) == 64
    assert all(len(seen[root.stat().st_ino]) == 100 for root in roots)


# LLM: 128个占位迭代器停止维护后，新根先等待；超过活跃键数两倍次调用后闲置迭代器被清退，新根取得名额并扫完整轮。
# 函数用途: 回归停止维护的根不会永久占满名额。
def test_idle_live_scans_are_released_to_a_waiting_root(tmp_path, _r7_observed):
    calls, seen = _r7_observed
    for index in range(128):
        root = _r7_filler_root(tmp_path / str(index) / "display_archives", 100)
        calls.append(0)
        maintenance.compact_display_archives(root, now=_R7_NOW, max_archives=1)
    late = _r7_filler_root(tmp_path / "late" / "display_archives", 100)
    calls.append(0)
    maintenance.compact_display_archives(late, now=_R7_NOW, max_archives=1)
    assert len(maintenance._live_scans) == 128 and len(maintenance._scan_waiting) == 1
    for _ in range(300):
        if len(seen[late.stat().st_ino]) == 100:
            break
        calls.append(0)
        assert maintenance.compact_display_archives(late, now=_R7_NOW, max_archives=1)["errors"] == 0
        assert len(maintenance._live_scans) <= 128
    assert len(seen[late.stat().st_ino]) == 100
    assert max(calls) == 64 and not maintenance._scan_waiting


# LLM: 续扫中途异常丢失活迭代器后，同进程下一次从头开新一轮，不按旧偏移重放前缀。
# 函数用途: 回归同进程丢失迭代器时的枚举上限。
def test_lost_live_scan_restarts_without_replaying_the_prefix(tmp_path, _r7_observed, monkeypatch):
    calls, _ = _r7_observed
    root = _r7_young_archives(tmp_path / "display_archives", 300)
    for _ in range(3):
        calls.append(0)
        assert maintenance.compact_display_archives(root, now=_R7_NOW, max_archives=1)["scanned"] == 64
    original = maintenance._scan_entries

    def broken(*args):
        monkeypatch.setattr(maintenance, "_scan_entries", original)
        raise OSError("scan failed")

    monkeypatch.setattr(maintenance, "_scan_entries", broken)
    calls.append(0)
    assert maintenance.compact_display_archives(root, now=_R7_NOW, max_archives=1)["errors"] == 1
    calls.append(0)
    result = maintenance.compact_display_archives(root, now=_R7_NOW, max_archives=1)
    assert result["scanned"] == result["skipped"] == 64 and result["errors"] == 0
    assert calls == [64, 64, 64, 0, 64]


# LLM: 持久游标偏移超出sys.maxsize、为负或非int均按损坏游标从0恢复，不让islice抛出未捕获异常；sys.maxsize本身按目录缩短重开。
# 函数用途: 回归R6-ARCH-02超大合法JSON整数偏移。
@pytest.mark.parametrize("offset", [2**63, sys.maxsize + 1, sys.maxsize, -1, True, 1.5])
def test_out_of_range_persisted_cursor_offset_is_treated_as_damaged(tmp_path, offset):
    root = _r7_young_archives(tmp_path / "display_archives", 80)
    info = root.stat()
    operation = "compact_display_archives.<locals>.action"
    cursor = tmp_path / maintenance._CURSOR_FILE
    cursor.write_text(json.dumps({operation: [info.st_dev, info.st_ino, offset]}))
    result = maintenance.compact_display_archives(root, now=_R7_NOW, max_archives=1)
    assert result["scanned"] == result["skipped"] == 64 and result["errors"] == 0
    assert json.loads(cursor.read_text())[operation] == [info.st_dev, info.st_ino, 64]
