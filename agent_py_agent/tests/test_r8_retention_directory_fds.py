"""目录扫描跨片和退休不借助GC释放原生目录句柄。"""
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.memory_store import retention_scan as scan
from agent_py_agent.tests.test_r8_candidate_pin_stream import _projection


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
        (directory / 'new').touch()
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
