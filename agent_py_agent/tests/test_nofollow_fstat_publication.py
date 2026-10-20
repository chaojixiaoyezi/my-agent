# LLM: 只在临时普通目录安排真实FD采样后原子发表；不模拟stat结果、读正文或安全异常，不启动服务/模型/native。
# 模块用途: 覆盖strict snapshot的fstat→current stat窗口及原目录、元数据、链接、预算和FD清理硬边界。
import errno
import os
import stat

import pytest

from agent_py_agent.agent.common import nofollow_fs as fs


# LLM: 使用真实同目录临时FD完整写入/fsync后os.replace；只改本测试fixture，不替换reader实现。
# 函数用途: 提供与原atomic writer相同发表语义的第二种真实写法。
def replace_leaf(root, payload=b'new'):
    candidate = root / 'candidate'
    descriptor = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        assert os.write(descriptor, payload) == len(payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(candidate, root / 'leaf')


# LLM: 观察器不改原metadata/正文；准确窗口只调用真实写者，原leaf读取函数和全部身份校验继续执行。
# 函数用途: 记录同parent FD的真实attempt、采样及正文，确定性安排一次或每轮发表。
def at_fstat(monkeypatch, root, window, action, *, every=False, before_sample=None, fresh_error=None):
    original_fstat, original_leaf, original_read = os.fstat, fs._read_snapshot_leaf, fs._read_descriptor
    target = 1 if window == 'before_read' else 2
    state = {'attempts': 0, 'parents': [], 'events': [], 'reads': [], 'leaf_fds': set(),
             'active': False, 'sample': 0, 'injected': False, 'ever': False, 'error_raised': False}

    # LLM: 只标记当前原leaf调用，所有参数/异常原样转发；parent FD不替换或重捕。
    # 函数用途: 观察原三次预算的实际使用，不用计数代替真实读取。
    def reading_leaf(*args):
        state['attempts'] += 1
        state['parents'].append(args[0])
        state.update(active=True, sample=0, injected=False)
        try:
            return original_leaf(*args)
        finally:
            state['active'] = False

    # LLM: 先完成真实fstat，再可发表；返回原stat_result，不改nlink/时间戳来伪造交错。
    # 函数用途: 固定OS采样完成到下一次current stat之间的窗口。
    def sampling(descriptor):
        value = original_fstat(descriptor)
        if not state['active'] or not stat.S_ISREG(value.st_mode):
            return value
        state['leaf_fds'].add(descriptor)
        if state['injected'] and fresh_error is not None and not state['error_raised']:
            state['error_raised'] = True
            raise fresh_error
        if state['injected']:
            return value
        state['sample'] += 1
        if state['sample'] == target and (every or not state['ever']):
            state.update(injected=True, ever=True)
            if before_sample is not None:
                before_sample()
                value = original_fstat(descriptor)
            action()
            state['events'].append({'sample': value, 'fresh': original_fstat(descriptor),
                                    'current': os.stat('leaf', dir_fd=state['parents'][-1], follow_symlinks=False)})
        return value

    # LLM: 原_read_descriptor继续真实读取；记录只用于验证读后竞争必须丢弃旧正文。
    # 函数用途: 观察实际读取版本，不提供替代bytes。
    def reading(*args):
        content = original_read(*args)
        state['reads'].append(content)
        return content

    monkeypatch.setattr(os, 'fstat', sampling)
    monkeypatch.setattr(fs, '_read_snapshot_leaf', reading_leaf)
    monkeypatch.setattr(fs, '_read_descriptor', reading)
    return state


# LLM: 检查每个真实叶FD已归还，不通过仅计close次数伪造释放。
# 函数用途: 验证异常和重读路径没有叶FD泄漏。
def assert_closed(state):
    for descriptor in state['leaf_fds']:
        with pytest.raises(OSError) as caught:
            os.fstat(descriptor)
        assert caught.value.errno == errno.EBADF


@pytest.mark.parametrize('window', ['before_read', 'after_read'])
@pytest.mark.parametrize('writer', ['os_replace', 'original_atomic'])
def test_sampled_attached_fd_then_real_atomic_publication_reads_new_version(tmp_path, monkeypatch, window, writer):
    fs.write_bytes_atomic_beneath(tmp_path, ('leaf',), b'old')
    publish = (lambda: replace_leaf(tmp_path)) if writer == 'os_replace' else (lambda: fs.write_bytes_atomic_beneath(tmp_path, ('leaf',), b'new'))
    state = at_fstat(monkeypatch, tmp_path, window, publish)
    assert fs.read_bytes_beneath(tmp_path, ('leaf',), max_bytes=3, require_dir_fd=True, snapshot_retries=2) == b'new'
    assert state['attempts'] == 2 and len(set(state['parents'])) == 1
    assert state['reads'] == ([b'new'] if window == 'before_read' else [b'old', b'new'])
    event = state['events'][0]
    assert event['sample'].st_nlink == 1 and event['fresh'].st_nlink == 0 and event['current'].st_nlink == 1
    assert event['sample'].st_ino == event['fresh'].st_ino != event['current'].st_ino
    assert_closed(state)


@pytest.mark.parametrize('window', ['before_read', 'after_read'])
def test_sampled_fd_continuous_publication_exhausts_original_three_attempts(tmp_path, monkeypatch, window):
    fs.write_bytes_atomic_beneath(tmp_path, ('leaf',), b'old')
    state = at_fstat(monkeypatch, tmp_path, window, lambda: replace_leaf(tmp_path), every=True)
    with pytest.raises(fs._SnapshotLeafChanged):
        fs.read_bytes_beneath(tmp_path, ('leaf',), max_bytes=3, snapshot_retries=2)
    assert state['attempts'] == len(state['events']) == 3 and len(set(state['parents'])) == 1
    assert_closed(state)


@pytest.mark.parametrize('window', ['before_read', 'after_read'])
def test_renamed_original_still_single_link_is_hard_refusal(tmp_path, monkeypatch, window):
    fs.write_bytes_atomic_beneath(tmp_path, ('leaf',), b'old')

    # LLM: 原inode仍真实命名于旁名，不能因current不同就授予可重读的detachment。
    # 函数用途: 构造单链接但未脱链的原FD反例。
    def publish():
        os.rename(tmp_path / 'leaf', tmp_path / 'retained')
        replace_leaf(tmp_path)

    state = at_fstat(monkeypatch, tmp_path, window, publish)
    with pytest.raises(fs.NoFollowPathError) as caught:
        fs.read_bytes_beneath(tmp_path, ('leaf',), snapshot_retries=2)
    assert type(caught.value) is fs.NoFollowPathError and state['attempts'] == 1
    assert state['events'][0]['fresh'].st_nlink == 1
    assert (tmp_path / 'retained').read_bytes() == b'old'
    assert_closed(state)


@pytest.mark.parametrize('window', ['before_read', 'after_read'])
@pytest.mark.parametrize('kind', ['symlink', 'hardlink', 'fifo', 'directory'])
def test_unsafe_current_after_fd_sample_is_never_retried(tmp_path, monkeypatch, window, kind):
    fs.write_bytes_atomic_beneath(tmp_path, ('leaf',), b'old')
    outside = tmp_path / 'outside'
    outside.write_bytes(b'untouched')

    # LLM: 危险形状由真实文件系统操作产生，reader仍必须在内容读取/重试前硬拒。
    # 函数用途: 验current不能把链接或非regular叶洗成安全替换。
    def publish():
        (tmp_path / 'leaf').unlink()
        if kind == 'symlink':
            (tmp_path / 'leaf').symlink_to(outside)
        elif kind == 'hardlink':
            os.link(outside, tmp_path / 'leaf')
        elif kind == 'fifo':
            os.mkfifo(tmp_path / 'leaf')
        else:
            (tmp_path / 'leaf').mkdir()

    state = at_fstat(monkeypatch, tmp_path, window, publish)
    with pytest.raises(fs.NoFollowPathError) as caught:
        fs.read_bytes_beneath(tmp_path, ('leaf',), snapshot_retries=2)
    assert type(caught.value) is fs.NoFollowPathError and state['attempts'] == 1
    assert outside.read_bytes() == b'untouched'
    assert_closed(state)


@pytest.mark.parametrize('window', ['before_read', 'after_read'])
@pytest.mark.parametrize('field', ['size', 'mtime', 'mode'])
@pytest.mark.parametrize('timing', ['same_inode', 'before_sample_and_replace', 'after_sample_and_replace'])
def test_observed_same_inode_metadata_drift_cannot_be_laundered(tmp_path, monkeypatch, window, field, timing):
    fs.write_bytes_atomic_beneath(tmp_path, ('leaf',), b'old')
    initial = (tmp_path / 'leaf').stat()

    # LLM: 修改真实原inode的一个确定可观察字段，mtime显式移一秒避免依赖平台时钟精度。
    # 函数用途: 验证已观察到的mode/size/mtime漂移不能凭后续detachment洗掉。
    def mutate():
        if field == 'size':
            (tmp_path / 'leaf').write_bytes(b'longer')
        elif field == 'mtime':
            os.utime(tmp_path / 'leaf', ns=(initial.st_atime_ns, initial.st_mtime_ns + 1_000_000_000))
        else:
            (tmp_path / 'leaf').chmod(0o640)

    # LLM: 只有明确timing使用真实replace，其它只在同inode修改；无合成stat对象。
    # 函数用途: 分别覆盖原样本前漂移、样本后漂移和纯原位写。
    def publish():
        if timing != 'before_sample_and_replace':
            mutate()
        if timing != 'same_inode':
            replace_leaf(tmp_path)

    state = at_fstat(monkeypatch, tmp_path, window, publish, before_sample=mutate if timing == 'before_sample_and_replace' else None)
    with pytest.raises(fs.NoFollowPathError) as caught:
        fs.read_bytes_beneath(tmp_path, ('leaf',), snapshot_retries=2)
    assert type(caught.value) is fs.NoFollowPathError and state['attempts'] == 1
    assert_closed(state)


@pytest.mark.parametrize('window', ['before_read', 'after_read'])
def test_original_parent_replacement_is_hard_refusal_after_safe_leaf_candidate(tmp_path, monkeypatch, window):
    root = tmp_path / 'root'
    root.mkdir()
    fs.write_bytes_atomic_beneath(root, ('leaf',), b'old')

    # LLM: 叶在原parent内合法发表，但规范root真实换身份；有限重读不能重锚到新root。
    # 函数用途: 验父链硬门仍高于叶可重读候选。
    def publish():
        moved = tmp_path / 'moved'
        os.rename(root, moved)
        root.mkdir()
        (root / 'leaf').write_bytes(b'other')
        replace_leaf(moved)

    state = at_fstat(monkeypatch, root, window, publish)
    with pytest.raises(fs.NoFollowPathError) as caught:
        fs.read_bytes_beneath(root, ('leaf',), snapshot_retries=2)
    assert type(caught.value) is fs.NoFollowPathError and state['attempts'] == 1
    assert (root / 'leaf').read_bytes() == b'other'
    assert_closed(state)


def test_two_observed_leaf_replacements_remain_hard_refusal(tmp_path, monkeypatch):
    fs.write_bytes_atomic_beneath(tmp_path, ('leaf',), b'old')
    supported, original_open = fs._supports_dir_fd(), os.open
    opened = []

    # LLM: 真实A→B在before/open之间，B→C在fstat/current之间；before/opened不同的原硬拒分支不扩大。
    # 函数用途: 验已观察到多版本不稳定不能冒充已知before的脱链。
    def opening(name, flags, mode=0o777, **kwargs):
        if name == 'leaf' and flags & os.O_ACCMODE == os.O_RDONLY and not opened:
            opened.append(name)
            replace_leaf(tmp_path, b'mid')
        return original_open(name, flags, mode, **kwargs)

    monkeypatch.setattr(fs, '_supports_dir_fd', lambda: supported)
    monkeypatch.setattr(os, 'open', opening)
    state = at_fstat(monkeypatch, tmp_path, 'before_read', lambda: replace_leaf(tmp_path))
    with pytest.raises(fs.NoFollowPathError) as caught:
        fs.read_bytes_beneath(tmp_path, ('leaf',), snapshot_retries=2)
    assert type(caught.value) is fs.NoFollowPathError and state['attempts'] == 1
    assert_closed(state)


@pytest.mark.parametrize('window', ['before_read', 'after_read'])
def test_fresh_fd_probe_error_is_original_primary_and_closes_leaf(tmp_path, monkeypatch, window):
    fs.write_bytes_atomic_beneath(tmp_path, ('leaf',), b'old')
    primary = OSError(errno.EIO, 'fixed fresh descriptor failure')
    state = at_fstat(monkeypatch, tmp_path, window, lambda: replace_leaf(tmp_path), fresh_error=primary)
    with pytest.raises(OSError) as caught:
        fs.read_bytes_beneath(tmp_path, ('leaf',), snapshot_retries=2)
    assert caught.value is primary and state['attempts'] == 1 and state['error_raised']
    assert_closed(state)


def test_new_typed_candidate_cleanup_error_preserves_primary_and_stops_retry(tmp_path, monkeypatch):
    fs.write_bytes_atomic_beneath(tmp_path, ('leaf',), b'old')
    original_close = os.close
    state = at_fstat(monkeypatch, tmp_path, 'before_read', lambda: replace_leaf(tmp_path))
    failed = []

    # LLM: 先真实close才抛次生失败；只针对已发表新窗口的原leaf FD，父FD仍真实归还。
    # 函数用途: 验原cleanup_failed合同保主typed对象且禁止重读。
    def closing(descriptor):
        original_close(descriptor)
        if state['events'] and descriptor in state['leaf_fds'] and not failed:
            failed.append(descriptor)
            raise OSError(errno.EIO, 'fixed secondary close failure')

    monkeypatch.setattr(os, 'close', closing)
    with pytest.raises(fs._SnapshotLeafChanged) as caught:
        fs.read_bytes_beneath(tmp_path, ('leaf',), snapshot_retries=2)
    assert caught.value.cleanup_failed is True and state['attempts'] == 1 and len(failed) == 1
    assert_closed(state)
