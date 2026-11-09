"""冷目录发现分片推进，分页不能通过截断未排序目录来漏掉旧owner。"""
import stat
from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.agent import owner_wake_discovery as discovery


def test_cold_owner_page_bounds_scan_and_eventually_covers_all(tmp_path, monkeypatch):
    owners = tmp_path / 'owners'
    for number in reversed(range(500)):
        (owners / 'providers' / 'feishu' / 'users' / f'u{number:04d}').mkdir(parents=True)
    visits = []
    original = discovery._candidate_owner_homes

    def counted(root):
        for row in original(root):
            visits.append(row)
            yield row

    monkeypatch.setattr(discovery, '_candidate_owner_homes', counted)
    cursor, found = None, []
    for _ in range(140):
        before = len(visits)
        page = discovery.discover_owner_home_page(owners, limit=4, after_cursor=cursor)
        assert len(visits) - before <= 128
        assert len(page.targets) <= 4
        found.extend(target.identity.owner_id for target in page.targets)
        cursor = page.next_cursor
        if cursor is None:
            break
    else:
        raise AssertionError('分页未收敛')
    assert found == [f'u{number:04d}' for number in range(500)]
    assert len(visits) == 500


def test_removed_owner_not_returned_during_reconciliation(tmp_path):
    owners = tmp_path / 'owners'
    for number in range(300):
        (owners / 'local' / f'u{number:04d}').mkdir(parents=True)
    cursor = None
    for _ in range(10):
        page = discovery.discover_owner_home_page(owners, limit=100, after_cursor=cursor)
        cursor = page.next_cursor
        if cursor is None:
            break
    (owners / 'local' / 'u0000').rmdir()
    page = discovery.discover_owner_home_page(owners, limit=4)
    assert all(target.identity.owner_id != 'u0000' for target in page.targets)
    assert page.next_cursor is not None


def _coarse_directory_clock(monkeypatch):
    original = Path.stat

    def measured(path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        if not stat.S_ISDIR(result.st_mode):
            return result
        fields = {name: getattr(result, name) for name in dir(result) if name.startswith('st_')}
        fields.update(st_mtime_ns=1, st_ctime_ns=1)
        return SimpleNamespace(**fields)

    monkeypatch.setattr(Path, 'stat', measured)


def test_owner_add_remove_invalidates_even_inside_one_filesystem_clock_tick(tmp_path, monkeypatch):
    owners = tmp_path / 'owners'
    bucket = owners / 'providers' / 'feishu' / 'users'
    (bucket / 'first').mkdir(parents=True)
    _coarse_directory_clock(monkeypatch)
    assert [row.identity.owner_id for row in discovery.discover_owner_home_page(owners).targets] == ['first']
    (bucket / 'second').mkdir()
    assert [row.identity.owner_id for row in discovery.discover_owner_home_page(owners).targets] == ['first', 'second']
    (bucket / 'first').rmdir()
    assert [row.identity.owner_id for row in discovery.discover_owner_home_page(owners).targets] == ['second']


def test_new_run_is_detected_with_unchanged_directory_timestamps(tmp_path, monkeypatch):
    import json

    from agent_py_agent.tests.test_owner_wake_discovery import _settled_history, _write_link

    _coarse_directory_clock(monkeypatch)
    _settled, active = _settled_history(tmp_path)
    assert discovery.unfinished_task_ids(tmp_path) == ['task-100']
    new = active.parents[2] / 'new-run' / 'work' / 'state.json'
    new.parent.mkdir(parents=True)
    new.write_text(json.dumps({'task_id': 'new', 'status': 'RUNNING'}))
    _write_link(tmp_path, 'new', 'active')
    assert set(discovery.unfinished_task_ids(tmp_path)) == {'task-100', 'new'}


def test_large_link_cache_reenumerates_names_at_ttl_even_when_metadata_collides(tmp_path, monkeypatch):
    import json
    import os
    import time

    folder = tmp_path / 'links'
    folder.mkdir()
    old_ns = time.time_ns() - 120_000_000_000
    for index in range(100):
        path = folder / f'task-{index}.json'
        path.write_text(json.dumps({'task_id': f'task-{index}', 'status': 'completed'}))
        os.utime(path, ns=(old_ns, old_ns))
    _coarse_directory_clock(monkeypatch)
    first = discovery._directory_scan(folder, 'link')
    (folder / 'task-0.json').unlink()
    replacement = folder / 'new-task.json'
    replacement.write_text(json.dumps({'task_id': 'new', 'status': 'active'}))
    # 同数量文件替换可保持全部目录元数据；模拟这种合法文件系统结果，不伪造文件内容。
    monkeypatch.setattr(discovery, '_directory_fp', lambda root: first.dir_fp)
    assert discovery._directory_scan(folder, 'link').run_names == first.run_names
    monkeypatch.setattr(discovery.time, 'monotonic', lambda: first.verified_at + 61)
    refreshed = discovery._directory_scan(folder, 'link')
    assert 'task-0.json' not in refreshed.run_names
    assert ('new', 'active', replacement) in refreshed.active_rows
