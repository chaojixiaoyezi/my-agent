# LLM: 只在普通临时目录按原atomic writer确定性交错；W正例复用原C/P4/producer与受控端点，不启动真实服务/模型/native。
# 模块用途: 验证显式snapshot仅有限重读安全A/B，硬路径/权限/目录/清理错误与默认调用仍保守拒绝。
import os
from pathlib import Path

import pytest

from agent_py_agent.agent.common import nofollow_fs as fs
from agent_py_agent.agent.gateway_parts import multiprocess_takeover_runtime as takeover
from agent_py_agent.tests.test_gateway_prepared_route_immutability import context as context
from agent_py_agent.tests.test_gateway_role_heartbeat_stability import (
    publish_heartbeat,
)
from agent_py_agent.tests.test_gateway_role_heartbeat_stability import (
    running as running,
)
from agent_py_agent.tests.test_gateway_worker_runtime import physical as physical


# LLM: 所有叶尝试复用原os.open；仅指定窗口调用原writer，不代替read/open/身份验证或按文本制造安全异常。
# 函数用途: 在真实stat/open/fstat窗口发表不同inode的普通文件，并记录准确attempt和parent FD。
def replace_at_open(monkeypatch, root, window, *, every=False, action=None):
    supported = fs._supports_dir_fd()
    monkeypatch.setattr(fs, '_supports_dir_fd', lambda: supported)
    original = os.open
    attempts, parents = [], []

    def opening(name, flags, mode=0o777, **kwargs):
        target = name == 'leaf'
        if target:
            attempts.append(name)
            parents.append(kwargs.get('dir_fd'))
        replace = target and (every or len(attempts) == 1)
        if replace and window == 'A':
            (action or (lambda: fs.write_bytes_atomic_beneath(root, ('leaf',), b'new')) )()
        descriptor = original(name, flags, mode, **kwargs)
        if replace and window == 'B':
            (action or (lambda: fs.write_bytes_atomic_beneath(root, ('leaf',), b'new')) )()
        return descriptor

    monkeypatch.setattr(os, 'open', opening)
    return attempts, parents


@pytest.mark.parametrize('window', ['A', 'B', 'after_read'])
def test_explicit_snapshot_reads_new_atomic_version_in_all_approved_windows(tmp_path, monkeypatch, window):
    fs.write_bytes_atomic_beneath(tmp_path, ('leaf',), b'old')
    if window == 'after_read':
        original = fs._read_descriptor
        calls = []

        def reading(fd, limit):
            content = original(fd, limit)
            calls.append(content)
            if len(calls) == 1:
                fs.write_bytes_atomic_beneath(tmp_path, ('leaf',), b'new')
            return content

        monkeypatch.setattr(fs, '_read_descriptor', reading)
    else:
        attempts, parents = replace_at_open(monkeypatch, tmp_path, window)
    assert fs.read_bytes_beneath(tmp_path, ('leaf',), max_bytes=3, require_dir_fd=True, snapshot_retries=2) == b'new'
    if window == 'after_read':
        assert calls == [b'old', b'new']
    else:
        assert len(attempts) == 2 and len(set(parents)) == 1


@pytest.mark.parametrize('window', ['A', 'B'])
def test_default_read_keeps_original_no_retry_failure(tmp_path, monkeypatch, window):
    fs.write_bytes_atomic_beneath(tmp_path, ('leaf',), b'old')
    attempts, _parents = replace_at_open(monkeypatch, tmp_path, window)
    with pytest.raises(fs.NoFollowPathError):
        fs.read_bytes_beneath(tmp_path, ('leaf',), require_dir_fd=True)
    assert len(attempts) == 1


@pytest.mark.parametrize('window', ['A', 'B'])
def test_safe_churn_exhausts_exactly_three_attempts_without_sleep(tmp_path, monkeypatch, window):
    fs.write_bytes_atomic_beneath(tmp_path, ('leaf',), b'old')
    attempts, parents = replace_at_open(monkeypatch, tmp_path, window, every=True)
    with pytest.raises(fs._SnapshotLeafChanged):
        fs.read_bytes_beneath(tmp_path, ('leaf',), snapshot_retries=2)
    assert len(attempts) == 3 and len(set(parents)) == 1


@pytest.mark.parametrize('kind', ['symlink', 'hardlink', 'fifo', 'directory'])
def test_unsafe_leaf_swap_is_never_retried(tmp_path, monkeypatch, kind):
    leaf = tmp_path / 'leaf'
    leaf.write_bytes(b'old')
    other = tmp_path / 'outside'
    other.write_bytes(b'untouched')

    def change():
        leaf.unlink()
        if kind == 'symlink':
            leaf.symlink_to(other)
        elif kind == 'hardlink':
            os.link(other, leaf)
        elif kind == 'fifo':
            os.mkfifo(leaf)
        else:
            leaf.mkdir()

    attempts, _parents = replace_at_open(monkeypatch, tmp_path, 'A', every=True, action=change)
    with pytest.raises(OSError):
        fs.read_bytes_beneath(tmp_path, ('leaf',), snapshot_retries=2)
    assert len(attempts) == 1 and other.read_bytes() == b'untouched'


@pytest.mark.parametrize('which', ['root', 'middle', 'parent'])
def test_directory_replacement_never_reanchors_original_parent(tmp_path, monkeypatch, which):
    root = tmp_path / 'root'
    parent = root / 'middle' / 'parent'
    parent.mkdir(parents=True)
    (parent / 'leaf').write_bytes(b'old')
    path = {'root': root, 'middle': root / 'middle', 'parent': parent}[which]
    supported = fs._supports_dir_fd()
    monkeypatch.setattr(fs, '_supports_dir_fd', lambda: supported)
    original = os.open
    attempts = []

    def opening(name, flags, mode=0o777, **kwargs):
        if name == 'leaf':
            attempts.append(name)
            assert len(attempts) == 1
            moved = path.with_name(path.name + '-old')
            path.rename(moved)
            path.mkdir()
        return original(name, flags, mode, **kwargs)

    monkeypatch.setattr(os, 'open', opening)
    with pytest.raises(fs.NoFollowPathError):
        fs.read_bytes_beneath(root, ('middle', 'parent', 'leaf'), snapshot_retries=2)
    assert len(attempts) == 1


def test_initial_missing_is_none_but_missing_after_observed_race_is_not(tmp_path, monkeypatch):
    assert fs.read_bytes_beneath(tmp_path, ('absent',), snapshot_retries=2) is None
    (tmp_path / 'leaf').write_bytes(b'old')
    original = fs._read_snapshot_leaf
    attempts = []

    def reading(parent, name, limit, allow_missing):
        attempts.append(allow_missing)
        try:
            return original(parent, name, limit, allow_missing)
        except fs._SnapshotLeafChanged:
            (tmp_path / 'leaf').unlink()
            raise

    monkeypatch.setattr(fs, '_read_snapshot_leaf', reading)
    replace_at_open(monkeypatch, tmp_path, 'A')
    with pytest.raises(FileNotFoundError):
        fs.read_bytes_beneath(tmp_path, ('leaf',), snapshot_retries=2)
    assert attempts == [True, False]


def test_later_hard_permission_error_is_not_hidden_by_first_race(tmp_path, monkeypatch):
    (tmp_path / 'leaf').write_bytes(b'old')
    original = fs._read_snapshot_leaf
    primary = PermissionError('fixed synthetic primary')
    calls = []

    def reading(*args):
        calls.append(1)
        if len(calls) == 2:
            raise primary
        return original(*args)

    monkeypatch.setattr(fs, '_read_snapshot_leaf', reading)
    replace_at_open(monkeypatch, tmp_path, 'A')
    with pytest.raises(PermissionError) as caught:
        fs.read_bytes_beneath(tmp_path, ('leaf',), snapshot_retries=2)
    assert caught.value is primary and len(calls) == 2


@pytest.mark.parametrize('value', [True, -1, 3, '2', None])
def test_invalid_snapshot_budget_rejected_before_open(tmp_path, monkeypatch, value):
    monkeypatch.setattr(fs, 'open_directory_beneath', lambda *a, **k: pytest.fail('must validate before any open'))
    with pytest.raises(ValueError):
        fs.read_bytes_beneath(tmp_path, ('leaf',), snapshot_retries=value)


def test_explicit_snapshot_never_uses_portable_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(fs, '_supports_dir_fd', lambda: False)
    monkeypatch.setattr(fs, '_read_bytes_portable', lambda *a: pytest.fail('strict snapshot cannot use portable'))
    with pytest.raises(fs.NoFollowPathError):
        fs.read_bytes_beneath(tmp_path, ('leaf',), snapshot_retries=2)


def test_explicit_snapshot_preserves_read_limit_and_default_call_shape(tmp_path, monkeypatch):
    (tmp_path / 'leaf').write_bytes(b'1234')
    with pytest.raises(ValueError, match='read limit'):
        fs.read_bytes_beneath(tmp_path, ('leaf',), max_bytes=3, snapshot_retries=2)
    monkeypatch.setattr(fs, '_snapshot_parent', lambda *a: pytest.fail('default cannot enter snapshot path'))
    assert fs.read_bytes_beneath(tmp_path, ('leaf',)) == b'1234'


def test_original_worker_atomic_publication_does_not_quarantine(running, monkeypatch):
    supported = fs._supports_dir_fd()
    monkeypatch.setattr(fs, '_supports_dir_fd', lambda: supported)
    original = os.open
    attempts = []

    def opening(name, flags, mode=0o777, **kwargs):
        if name == 'w0.json':
            attempts.append(name)
            if len(attempts) == 1:
                publish_heartbeat(running, monkeypatch, stamp=2.0, healthy=False)
        return original(name, flags, mode, **kwargs)

    monkeypatch.setattr(os, 'open', opening)
    controller = running.control.controller()
    assert takeover._refresh_targets(controller) is True
    assert controller._all_quarantined is False and len(attempts) >= 2


# LLM: 只跟踪原os.open返回的本次FD，每次close先真实归还再可注入次生错误，不用仅计数伪造释放。
# 函数用途: 观察snapshot全部暂存FD生命周期，检测泄漏、重复close及primary优先级。
def monitor_descriptors(monkeypatch):
    original_open, original_close = os.open, os.close
    supported = fs._supports_dir_fd()
    records, active, failures = [], {}, []
    control = {'fail_close': False}

    def opening(*args, **kwargs):
        descriptor = original_open(*args, **kwargs)
        row = {'fd': descriptor, 'closed': 0}
        records.append(row)
        active[descriptor] = row
        return descriptor

    def closing(descriptor):
        row = active.pop(descriptor)
        row['closed'] += 1
        original_close(descriptor)
        if control['fail_close']:
            error = OSError('synthetic secondary close')
            failures.append(error)
            raise error

    monkeypatch.setattr(fs, '_supports_dir_fd', lambda: supported)
    monkeypatch.setattr(os, 'open', opening)
    monkeypatch.setattr(os, 'close', closing)
    return records, active, failures, control


@pytest.mark.parametrize('body_error', [False, True])
def test_snapshot_read_and_close_preserve_primary_and_release_every_fd_once(tmp_path, monkeypatch, body_error):
    (tmp_path / 'leaf').write_bytes(b'old')
    records, active, failures, control = monitor_descriptors(monkeypatch)
    primary = OSError('synthetic original read')
    original = fs._read_descriptor

    def reading(*args):
        control['fail_close'] = True
        if body_error:
            raise primary
        return original(*args)

    monkeypatch.setattr(fs, '_read_descriptor', reading)
    with pytest.raises(OSError) as caught:
        fs.read_bytes_beneath(tmp_path, ('leaf',), snapshot_retries=2)
    assert caught.value is (primary if body_error else failures[0])
    assert not active and all(row['closed'] == 1 for row in records)


def test_typed_candidate_close_failure_stops_retry_and_preserves_same_error(tmp_path, monkeypatch):
    (tmp_path / 'leaf').write_bytes(b'old')
    records, active, _failures, control = monitor_descriptors(monkeypatch)
    attempts, _parents = replace_at_open(monkeypatch, tmp_path, 'A')
    original = fs._snapshot_leaf_identity
    errors = []

    def identity(*args):
        try:
            return original(*args)
        except fs._SnapshotLeafChanged as error:
            errors.append(error)
            control['fail_close'] = True
            raise

    monkeypatch.setattr(fs, '_snapshot_leaf_identity', identity)
    with pytest.raises(fs._SnapshotLeafChanged) as caught:
        fs.read_bytes_beneath(tmp_path, ('leaf',), snapshot_retries=2)
    assert caught.value is errors[0] and caught.value.cleanup_failed is True
    assert len(attempts) == 1 and not active and all(row['closed'] == 1 for row in records)


def test_initial_prefix_close_failure_also_releases_retained_parent(tmp_path, monkeypatch):
    (tmp_path / 'parent').mkdir()
    (tmp_path / 'parent/leaf').write_bytes(b'old')
    records, active, failures, control = monitor_descriptors(monkeypatch)
    original = fs.open_directory_beneath
    calls = []

    def directory(*args, **kwargs):
        descriptor = original(*args, **kwargs)
        calls.append(1)
        if len(calls) == 2:
            control['fail_close'] = True
        return descriptor

    monkeypatch.setattr(fs, 'open_directory_beneath', directory)
    with pytest.raises(OSError) as caught:
        fs.read_bytes_beneath(tmp_path, ('parent', 'leaf'), snapshot_retries=2)
    assert caught.value is failures[0]
    assert not active and all(row['closed'] == 1 for row in records)


def test_snapshot_capture_fstat_error_keeps_original_and_closes_just_opened_fd(tmp_path, monkeypatch):
    (tmp_path / 'leaf').write_bytes(b'old')
    records, active, _failures, control = monitor_descriptors(monkeypatch)
    original = os.fstat
    calls = []
    primary = OSError('synthetic snapshot fstat')

    def fstat(descriptor):
        calls.append(descriptor)
        if len(calls) == 2:
            control['fail_close'] = True
            raise primary
        return original(descriptor)

    monkeypatch.setattr(os, 'fstat', fstat)
    with pytest.raises(OSError) as caught:
        fs.read_bytes_beneath(tmp_path, ('leaf',), snapshot_retries=2)
    assert caught.value is primary
    assert not active and all(row['closed'] == 1 for row in records)


def test_hard_link_seen_after_safe_race_is_not_retried_or_laundered(tmp_path, monkeypatch):
    (tmp_path / 'leaf').write_bytes(b'old')
    outside = tmp_path / 'outside'
    outside.write_bytes(b'untouched')
    original = fs._read_snapshot_leaf
    calls = []

    def reading(*args):
        calls.append(1)
        try:
            return original(*args)
        except fs._SnapshotLeafChanged:
            (tmp_path / 'leaf').unlink()
            os.link(outside, tmp_path / 'leaf')
            raise

    monkeypatch.setattr(fs, '_read_snapshot_leaf', reading)
    replace_at_open(monkeypatch, tmp_path, 'A')
    with pytest.raises(fs.NoFollowPathError) as caught:
        fs.read_bytes_beneath(tmp_path, ('leaf',), snapshot_retries=2)
    assert type(caught.value) is fs.NoFollowPathError and len(calls) == 2
    assert outside.read_bytes() == b'untouched'


def test_unrelated_outer_exception_cannot_hide_this_snapshot_close_error(tmp_path, monkeypatch):
    (tmp_path / 'leaf').write_bytes(b'old')
    records, active, failures, control = monitor_descriptors(monkeypatch)
    original = fs._read_descriptor

    def reading(*args):
        content = original(*args)
        control['fail_close'] = True
        return content

    monkeypatch.setattr(fs, '_read_descriptor', reading)
    try:
        raise LookupError('unrelated caller error')
    except LookupError:
        with pytest.raises(OSError) as caught:
            fs.read_bytes_beneath(tmp_path, ('leaf',), snapshot_retries=2)
    assert caught.value is failures[0]
    assert not active and all(row['closed'] == 1 for row in records)


@pytest.mark.parametrize('window', ['before_open', 'after_read'])
@pytest.mark.parametrize('change', ['size', 'mtime', 'ctime'])
def test_snapshot_same_inode_metadata_change_is_hard_refusal_without_retry(tmp_path, monkeypatch, window, change):
    supported = fs._supports_dir_fd()
    monkeypatch.setattr(fs, '_supports_dir_fd', lambda: supported)
    leaf = tmp_path / 'leaf'
    leaf.write_bytes(b'old')
    initial = leaf.stat()
    attempts, reads = [], []
    original_open, original_read = os.open, fs._read_descriptor

    def mutate():
        if change == 'size':
            leaf.write_bytes(b'changed-size')
        elif change == 'mtime':
            os.utime(leaf, ns=(initial.st_atime_ns, initial.st_mtime_ns + 1000000000))
        else:
            leaf.chmod(0o640 if initial.st_mode & 0o777 != 0o640 else 0o600)
        assert leaf.stat().st_ino == initial.st_ino

    def opening(name, flags, mode=0o777, **kwargs):
        if name == 'leaf' and flags & os.O_ACCMODE == os.O_RDONLY:
            attempts.append(name)
            if window == 'before_open' and len(attempts) == 1:
                mutate()
        return original_open(name, flags, mode, **kwargs)

    def reading(fd, limit):
        content = original_read(fd, limit)
        reads.append(content)
        if window == 'after_read' and len(reads) == 1:
            mutate()
        return content

    monkeypatch.setattr(os, 'open', opening)
    monkeypatch.setattr(fs, '_read_descriptor', reading)
    with pytest.raises(fs.NoFollowPathError):
        fs.read_bytes_beneath(tmp_path, ('leaf',), max_bytes=64, require_dir_fd=True, snapshot_retries=2)
    assert attempts == ['leaf']
    assert len(reads) == (0 if window == 'before_open' else 1)


@pytest.mark.parametrize('initial_mode,new_mode', [(0o664, 0o640), (0o600, 0o644)])
def test_snapshot_permission_change_is_refused_even_when_timestamps_are_equal(monkeypatch, initial_mode, new_mode):
    import stat
    from types import SimpleNamespace

    before = SimpleNamespace(st_dev=1, st_ino=2, st_nlink=1, st_mode=stat.S_IFREG | initial_mode,
                             st_size=3, st_mtime_ns=100, st_ctime_ns=100)
    opened = SimpleNamespace(**{**vars(before), 'st_mode': stat.S_IFREG | new_mode})
    original_stat = os.stat

    def observed_stat(path, *args, **kwargs):
        if path == 'leaf' and kwargs.get('dir_fd') == 0:
            return opened
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(fs.os, 'stat', observed_stat)
    with pytest.raises(fs.NoFollowPathError):
        fs._snapshot_leaf_identity(0, 'leaf', before, opened)
