# LLM: 这里是确定性stat-copyout模型，不是原生竞态复现；0link metadata来自真实脱链FD，未改stat字段；原生证据单独保存。
# 模块用途: 验证零链接观察整轮废弃、原预算及危险门；只操作临时目录，不启动服务、线程或模型。
import errno
import os
import stat

import pytest

from agent_py_agent.agent.common import nofollow_fs as fs


# LLM: 真实同目录原子发表，所有FD在本函数归还；不替代产品读取或制造metadata。
# 函数用途: 为copyout模型产生真实脱链inode与下一完整版本。
def publish(root, payload=b'new'):
    candidate = root / 'copyout-candidate'
    descriptor = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        assert os.write(descriptor, payload) == len(payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(candidate, root / 'leaf')


# LLM: 用真实脱链FD的fstat模拟已证OS stat copyout结果；仅调度返回时机，不伪造metadata值，不代表native API复现。
# 函数用途: 在首次、读前或读后stat位置交入regular0，观察原reader是否只丢弃并保持同parent预算。
def copyout_model(monkeypatch, root, window, *, every=False, retained=False, current_other=False, parent_change=False, probe_error=None):
    supported = fs._supports_dir_fd()
    monkeypatch.setattr(fs, '_supports_dir_fd', lambda: supported)
    original_stat, original_fstat = os.stat, os.fstat
    original_leaf, original_read, original_open = fs._read_snapshot_leaf, fs._read_descriptor, os.open
    state = {'attempts': 0, 'parents': [], 'reads': [], 'samples': [], 'leaf_fds': set(),
             'active': False, 'calls': 0, 'ever': False, 'probing': False, 'opens': 0}
    target = {'initial': 1, 'before_read': 2, 'after_read': 3}[window]

    def leaf(*args):
        state['attempts'] += 1
        state['parents'].append(args[0])
        state.update(active=True, calls=0)
        try:
            return original_leaf(*args)
        finally:
            state['active'] = False

    def opening(name, flags, mode=0o777, **kwargs):
        descriptor = original_open(name, flags, mode, **kwargs)
        if state['active'] and name == 'leaf' and flags & os.O_ACCMODE == os.O_RDONLY:
            state['opens'] += 1
            state['leaf_fds'].add(descriptor)
        return descriptor

    def sample(name, *args, **kwargs):
        value = original_stat(name, *args, **kwargs)
        if not state['active'] or name != 'leaf' or 'dir_fd' not in kwargs:
            return value
        state['calls'] += 1
        if state['calls'] != target or (state['ever'] and not every):
            return value
        state['ever'] = True
        descriptor = original_open('leaf', os.O_RDONLY | os.O_NOFOLLOW, dir_fd=kwargs['dir_fd'])
        try:
            if retained:
                os.rename(root / 'leaf', root / 'retained')
            publish(root, b'mid' if current_other or retained else b'new')
            if current_other or retained:
                os.close(descriptor)
                descriptor = original_open('leaf', os.O_RDONLY | os.O_NOFOLLOW, dir_fd=kwargs['dir_fd'])
                publish(root)
            zero = original_fstat(descriptor)
            assert stat.S_ISREG(zero.st_mode) and zero.st_nlink == 0
            state['samples'].append(zero)
        finally:
            os.close(descriptor)
        if parent_change:
            os.rename(root, root.with_name('moved'))
            root.mkdir()
        state['probing'] = True
        return zero

    def probing(descriptor):
        if state['probing'] and state['active'] and descriptor in state['leaf_fds'] and probe_error is not None:
            raise probe_error
        return original_fstat(descriptor)

    def reading(*args):
        value = original_read(*args)
        state['reads'].append(value)
        return value

    monkeypatch.setattr(fs, '_read_snapshot_leaf', leaf)
    monkeypatch.setattr(fs, '_read_descriptor', reading)
    monkeypatch.setattr(os, 'open', opening)
    monkeypatch.setattr(os, 'stat', sample)
    monkeypatch.setattr(os, 'fstat', probing)
    return state, original_fstat


def closed(state, original_fstat):
    for descriptor in state['leaf_fds']:
        with pytest.raises(OSError) as caught:
            original_fstat(descriptor)
        assert caught.value.errno == errno.EBADF


@pytest.mark.parametrize('window', ['initial', 'before_read', 'after_read'])
def test_zero_copyout_discards_whole_attempt_and_reads_complete_next_version(tmp_path, monkeypatch, window):
    publish(tmp_path, b'old')
    state, fstat = copyout_model(monkeypatch, tmp_path, window)
    assert fs.read_bytes_beneath(tmp_path, ('leaf',), snapshot_retries=2) == b'new'
    assert state['attempts'] == 2 and len(set(state['parents'])) == 1
    assert state['opens'] == (1 if window == 'initial' else 2)
    assert state['reads'] == ([b'old', b'new'] if window == 'after_read' else [b'new'])
    assert len(state['samples']) == 1
    closed(state, fstat)


@pytest.mark.parametrize('window', ['before_read', 'after_read'])
def test_zero_copyout_different_current_still_requires_original_fd_detached(tmp_path, monkeypatch, window):
    publish(tmp_path, b'old')
    state, fstat = copyout_model(monkeypatch, tmp_path, window, current_other=True)
    assert fs.read_bytes_beneath(tmp_path, ('leaf',), snapshot_retries=2) == b'new'
    assert state['attempts'] == 2
    closed(state, fstat)


@pytest.mark.parametrize('window', ['initial', 'before_read', 'after_read'])
def test_zero_copyout_continuous_publication_uses_only_original_three_attempts(tmp_path, monkeypatch, window):
    publish(tmp_path, b'old')
    state, fstat = copyout_model(monkeypatch, tmp_path, window, every=True)
    with pytest.raises(fs._SnapshotLeafChanged):
        fs.read_bytes_beneath(tmp_path, ('leaf',), snapshot_retries=2)
    assert state['attempts'] == len(state['samples']) == 3 and len(set(state['parents'])) == 1
    assert state['opens'] == (0 if window == 'initial' else 3)
    closed(state, fstat)


@pytest.mark.parametrize('window', ['before_read', 'after_read'])
def test_zero_current_cannot_hide_renamed_original_still_attached(tmp_path, monkeypatch, window):
    publish(tmp_path, b'old')
    state, fstat = copyout_model(monkeypatch, tmp_path, window, retained=True)
    with pytest.raises(fs.NoFollowPathError) as caught:
        fs.read_bytes_beneath(tmp_path, ('leaf',), snapshot_retries=2)
    assert type(caught.value) is fs.NoFollowPathError and state['attempts'] == 1
    assert (tmp_path / 'retained').read_bytes() == b'old'
    closed(state, fstat)


@pytest.mark.parametrize('window', ['initial', 'before_read', 'after_read'])
def test_zero_copyout_parent_change_is_hard_refusal(tmp_path, monkeypatch, window):
    root = tmp_path / 'root'
    root.mkdir()
    publish(root, b'old')
    state, fstat = copyout_model(monkeypatch, root, window, parent_change=True)
    with pytest.raises(fs.NoFollowPathError) as caught:
        fs.read_bytes_beneath(root, ('leaf',), snapshot_retries=2)
    assert type(caught.value) is fs.NoFollowPathError and state['attempts'] == 1
    closed(state, fstat)


@pytest.mark.parametrize('window', ['before_read', 'after_read'])
def test_zero_copyout_original_fstat_error_is_preserved(tmp_path, monkeypatch, window):
    publish(tmp_path, b'old')
    primary = OSError(errno.EIO, 'copyout probe failure')
    state, fstat = copyout_model(monkeypatch, tmp_path, window, probe_error=primary)
    with pytest.raises(OSError) as caught:
        fs.read_bytes_beneath(tmp_path, ('leaf',), snapshot_retries=2)
    assert caught.value is primary and state['attempts'] == 1
    closed(state, fstat)


@pytest.mark.parametrize('window', ['before_read', 'after_read'])
def test_zero_copyout_cleanup_failure_blocks_retry(tmp_path, monkeypatch, window):
    publish(tmp_path, b'old')
    state, fstat = copyout_model(monkeypatch, tmp_path, window)
    original_close, failed = os.close, []

    def closing(descriptor):
        original_close(descriptor)
        if state['probing'] and descriptor in state['leaf_fds'] and not failed:
            failed.append(descriptor)
            raise OSError(errno.EIO, 'copyout close failure')

    monkeypatch.setattr(os, 'close', closing)
    with pytest.raises(fs._SnapshotLeafChanged) as caught:
        fs.read_bytes_beneath(tmp_path, ('leaf',), snapshot_retries=2)
    assert caught.value.cleanup_failed and state['attempts'] == 1 and len(failed) == 1
    closed(state, fstat)


# LLM: 直接调用共享身份核验，样本全部取真实FD/叶stat；只选择样本组合，不捏造任何字段。
# 函数用途: 有界覆盖零链接分支的A/B/未知身份与已观察元数据关系，所有临时FD在finally关闭。
@pytest.mark.parametrize('relation', ['same_attached', 'same_detached', 'new_opened', 'three_inodes', 'unknown_detached'])
def test_zero_current_identity_relations_preserve_original_refusals(tmp_path, monkeypatch, relation):
    publish(tmp_path, b'old')
    before = (tmp_path / 'leaf').stat()
    first = os.open(tmp_path / 'leaf', os.O_RDONLY)
    descriptors = [first]
    try:
        if relation in ('new_opened', 'three_inodes', 'unknown_detached'):
            publish(tmp_path, b'mid')
            descriptor = os.open(tmp_path / 'leaf', os.O_RDONLY)
            descriptors.append(descriptor)
        else:
            descriptor = first
        opened = os.fstat(descriptor)
        publish(tmp_path)
        if relation in ('same_detached', 'unknown_detached'):
            opened = os.fstat(descriptor)
        if relation == 'three_inodes':
            third = os.open(tmp_path / 'leaf', os.O_RDONLY)
            descriptors.append(third)
            publish(tmp_path, b'end')
            current = os.fstat(third)
        else:
            current = os.fstat(descriptor)
        original_stat = os.stat
        monkeypatch.setattr(os, 'stat', lambda name, *a, **kw: current if name == 'leaf' else original_stat(name, *a, **kw))
        expected = fs.NoFollowPathError if relation in ('three_inodes', 'unknown_detached') else fs._SnapshotLeafChanged
        with pytest.raises(expected) as caught:
            fs._snapshot_leaf_identity(-1, 'leaf', before, opened, descriptor)
        assert type(caught.value) is expected
    finally:
        for descriptor in descriptors:
            os.close(descriptor)


@pytest.mark.parametrize('field', ['mode', 'size', 'mtime'])
@pytest.mark.parametrize('timing', ['before_opened', 'before_current', 'before_fresh'])
def test_zero_current_never_launders_observed_metadata_drift(tmp_path, monkeypatch, field, timing):
    publish(tmp_path, b'old')
    descriptor = os.open(tmp_path / 'leaf', os.O_RDWR)
    try:
        before = os.fstat(descriptor)

        def mutate():
            if field == 'mode':
                os.fchmod(descriptor, 0o640)
            elif field == 'size':
                with open(tmp_path / 'leaf', 'wb') as stream:
                    stream.write(b'longer')
            else:
                os.utime(tmp_path / 'leaf', ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000))

        if timing == 'before_opened':
            mutate()
        opened = os.fstat(descriptor)
        if timing == 'before_current':
            mutate()
        if timing == 'before_fresh':
            current_fd = os.open(tmp_path / 'leaf', os.O_RDONLY)
            try:
                publish(tmp_path)
                current = os.fstat(current_fd)
                if field == 'mode':
                    os.fchmod(descriptor, 0o640)
                elif field == 'size':
                    os.ftruncate(descriptor, 1)
                else:
                    os.utime(descriptor, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000))
            finally:
                os.close(current_fd)
        else:
            publish(tmp_path)
            current = os.fstat(descriptor)
        original_stat = os.stat
        monkeypatch.setattr(os, 'stat', lambda name, *a, **kw: current if name == 'leaf' else original_stat(name, *a, **kw))
        with pytest.raises(fs.NoFollowPathError) as caught:
            fs._snapshot_leaf_identity(-1, 'leaf', before, opened, descriptor)
        assert type(caught.value) is fs.NoFollowPathError
    finally:
        os.close(descriptor)


@pytest.mark.parametrize('kind', ['hardlink', 'directory', 'fifo'])
def test_zero_current_does_not_bypass_unsafe_opened_sample(tmp_path, monkeypatch, kind):
    publish(tmp_path, b'old')
    descriptor = os.open(tmp_path / 'leaf', os.O_RDONLY)
    try:
        before = os.fstat(descriptor)
        publish(tmp_path)
        current = os.fstat(descriptor)
        unsafe = tmp_path / 'unsafe'
        if kind == 'hardlink':
            unsafe.write_bytes(b'unsafe')
            os.link(unsafe, tmp_path / 'alias')
        elif kind == 'directory':
            unsafe.mkdir()
        else:
            os.mkfifo(unsafe)
        opened = unsafe.stat()
        original_stat = os.stat
        monkeypatch.setattr(os, 'stat', lambda name, *a, **kw: current if name == 'leaf' else original_stat(name, *a, **kw))
        with pytest.raises(fs.NoFollowPathError) as caught:
            fs._snapshot_leaf_identity(-1, 'leaf', before, opened, descriptor)
        assert type(caught.value) is fs.NoFollowPathError
    finally:
        os.close(descriptor)
