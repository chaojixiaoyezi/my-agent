# LLM: 仅合成原目录/原读口交错；有限重读不授来源权，旧expected与默认P6仍硬拒，须最终双OS普通执行。
# 模块用途: 验证已发表责任名称变动后的整次稳定读取及跨轮目录、设施和完整旧来源边界。
import hashlib

import pytest

from agent_py_agent.agent.gateway_parts import multiprocess_drain_sources as sources
from agent_py_agent.agent.gateway_parts import multiprocess_generation_metadata as history
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root


@pytest.fixture
def paths(tmp_path):
    root = tmp_path / 'gateway'
    root.mkdir(mode=0o700)
    return gateway_paths_from_root(root)


@pytest.mark.parametrize('namespace', ['admission', 'processing', 'control', 'executor'])
def test_one_publication_rereads_complete_current_names(paths, monkeypatch, namespace):
    parent, first, second = {
        'admission': (paths.root / 'admission/reservations', 'old.json', 'new.json'),
        'processing': (paths.processing, 'old.json', 'new.json'),
        'control': (paths.root / 'control_operations', 'gwctl-msg-' + 'a' * 32 + '.json',
                    'gwctl-msg-' + 'b' * 32 + '.json'),
        'executor': (paths.root / 'workers/executors/w0', 'a' * 64 + '.json', 'b' * 64 + '.json'),
    }[namespace]
    parent.mkdir(parents=True)
    raw = b'{"route":{"c_run_id":"new-c"},"c_run_id":"new-c"}'
    (parent / first).write_bytes(raw)
    read, calls = sources.read_bytes_beneath, []

    def publish(root, parts, **kwargs):
        content = read(root, parts, **kwargs)
        calls.append(parts[-1])
        if len(calls) == 1:
            (parent / second).write_bytes(raw)
        return content

    monkeypatch.setattr(sources, 'read_bytes_beneath', publish)
    files = history.generation_source_files(paths)
    prefix = parent.relative_to(paths.root).as_posix()
    assert files == {f'{prefix}/{first}': raw, f'{prefix}/{second}': raw}
    assert calls.count(first) == 2 and calls.count(second) == 1
    history._require_closed_files({f'{prefix}/{first}': hashlib.sha256(raw).hexdigest()}, files, 'new-c')


def test_removed_current_name_can_stabilize_but_original_expected_remains_required(paths, monkeypatch):
    paths.processing.mkdir(parents=True)
    path = paths.processing / 'old.json'
    raw = b'{"route":{"c_run_id":"new-c"}}'
    path.write_bytes(raw)
    read = sources.read_bytes_beneath

    def remove(root, parts, **kwargs):
        content = read(root, parts, **kwargs)
        path.unlink()
        return content

    monkeypatch.setattr(sources, 'read_bytes_beneath', remove)
    files = history.generation_source_files(paths)
    assert files == {}
    with pytest.raises(ValueError, match='generation_closed_source_changed'):
        history._require_closed_files({'requests/processing/old.json': hashlib.sha256(raw).hexdigest()}, files, 'new-c')


def test_continuous_name_changes_exhaust_three_complete_attempts(paths, monkeypatch):
    paths.processing.mkdir(parents=True)
    anchor = paths.processing / 'a.json'
    anchor.write_bytes(b'{}')
    extra = paths.processing / 'z.json'
    read, passes = sources.read_bytes_beneath, []

    def change(root, parts, **kwargs):
        content = read(root, parts, **kwargs)
        if parts[-1] == anchor.name:
            passes.append(1)
            if extra.exists():
                # 只在完整读取后改集合，不能靠叶missing制造可重读冲突。
                pass
            else:
                extra.write_bytes(b'{}')
        if parts[-1] == extra.name:
            extra.unlink()
        return content

    monkeypatch.setattr(sources, 'read_bytes_beneath', change)
    with pytest.raises(ValueError, match='drain_namespace_changed'):
        history.generation_source_files(paths)
    assert len(passes) == 3


@pytest.mark.parametrize('error', [ValueError('hard'), OSError('hard'), FileNotFoundError('hard')])
def test_arbitrary_leaf_failure_is_never_retried(paths, monkeypatch, error):
    paths.processing.mkdir(parents=True)
    (paths.processing / 'old.json').write_bytes(b'{}')
    calls = []

    def reject(*args, **kwargs):
        calls.append(1)
        raise error

    monkeypatch.setattr(sources, 'read_bytes_beneath', reject)
    with pytest.raises(type(error)) as caught:
        history.generation_source_files(paths)
    assert caught.value is error and calls == [1]


@pytest.mark.parametrize('mutation', ['facility', 'parent', 'ancestor', 'root'])
def test_hard_identity_change_wins_over_simultaneous_name_conflict(paths, monkeypatch, mutation):
    paths.processing.mkdir(parents=True)
    anchor = paths.processing / 'a.json'
    anchor.write_bytes(b'{}')
    facility = paths.processing / 'a.chunks.jsonl'
    facility.write_bytes(b'{}\n')
    read, calls = sources.read_bytes_beneath, []

    def mutate(root, parts, **kwargs):
        content = read(root, parts, **kwargs)
        calls.append(1)
        (paths.processing / 'new.json').write_bytes(b'{}')
        if mutation == 'facility':
            replacement = paths.root / 'replacement'
            replacement.write_bytes(b'{}\n')
            replacement.replace(facility)
        elif mutation == 'parent':
            paths.processing.rename(paths.root / 'old-processing')
            paths.processing.mkdir()
        elif mutation == 'ancestor':
            requests = paths.processing.parent
            requests.rename(paths.root / 'old-requests')
            requests.mkdir()
            (paths.root / 'old-requests/processing').rename(requests / 'processing')
        else:
            paths.root.rename(paths.root.with_name('old-root'))
            paths.root.mkdir()
            (paths.root.with_name('old-root') / 'requests').rename(paths.root / 'requests')
        return content

    monkeypatch.setattr(sources, 'read_bytes_beneath', mutate)
    expected = 'generation_source_facility_changed' if mutation == 'facility' else 'drain_namespace_changed'
    with pytest.raises(ValueError, match=expected):
        history.generation_source_files(paths)
    assert calls == [1]


def test_facility_disappearance_does_not_erase_first_observed_identity(paths, monkeypatch):
    paths.processing.mkdir(parents=True)
    (paths.processing / 'a.json').write_bytes(b'{}')
    facility = paths.processing / 'a.chunks.jsonl'
    facility.write_bytes(b'{}\n')
    read, names, passes = sources.read_bytes_beneath, sources._generation_source_names, []

    def publish(root, parts, **kwargs):
        content = read(root, parts, **kwargs)
        if parts[-1] == 'a.json':
            passes.append(1)
            if len(passes) == 1:
                facility.rename(paths.root / 'old-facility')
                (paths.processing / 'z.json').write_bytes(b'{}')
        return content

    count = 0

    def reappear(root, parts, descriptor):
        nonlocal count
        count += 1
        if count == 3:
            facility.write_bytes(b'{}\n')
        return names(root, parts, descriptor)

    monkeypatch.setattr(sources, 'read_bytes_beneath', publish)
    monkeypatch.setattr(sources, '_generation_source_names', reappear)
    with pytest.raises(ValueError, match='generation_source_facility_changed'):
        history.generation_source_files(paths)
    assert passes == [1]


def test_default_p6_still_rejects_name_change_without_generation_retry(paths, monkeypatch):
    paths.processing.mkdir(parents=True)
    (paths.processing / 'old.json').write_bytes(b'{}')
    read, calls = sources._descriptor_child_bytes, []

    def publish(descriptor, name):
        content = read(descriptor, name)
        calls.append(1)
        (paths.processing / 'new.json').write_bytes(b'{}')
        return content

    monkeypatch.setattr(sources, '_descriptor_child_bytes', publish)
    with pytest.raises(ValueError, match='drain_namespace_changed'):
        sources.original_directory_snapshot(paths.root, ('requests', 'processing'))
    assert calls == [1]


@pytest.mark.parametrize('prefix', ['parent', 'ancestor', 'root'])
def test_retry_never_rebinds_the_first_directory_chain(paths, monkeypatch, prefix):
    paths.processing.mkdir(parents=True)
    (paths.processing / 'a.json').write_bytes(b'{}')
    read, names = sources.read_bytes_beneath, sources._generation_source_names
    seen = 0

    def publish(root, parts, **kwargs):
        content = read(root, parts, **kwargs)
        (paths.processing / 'z.json').write_bytes(b'{}')
        return content

    def replace(root, parts, descriptor):
        nonlocal seen
        seen += 1
        if seen == 3:
            if prefix == 'parent':
                paths.processing.rename(paths.root / 'old-processing')
                paths.processing.mkdir()
                (paths.processing / 'a.json').write_bytes(b'{}')
            elif prefix == 'ancestor':
                paths.processing.parent.rename(paths.root / 'old-requests')
                paths.processing.parent.mkdir()
                (paths.root / 'old-requests/processing').rename(paths.processing)
            else:
                paths.root.rename(paths.root.with_name('old-root'))
                paths.root.mkdir()
                (paths.root.with_name('old-root') / 'requests').rename(paths.root / 'requests')
        return names(root, parts, descriptor)

    monkeypatch.setattr(sources, 'read_bytes_beneath', publish)
    monkeypatch.setattr(sources, '_generation_source_names', replace)
    with pytest.raises(ValueError, match='drain_namespace_changed'):
        history.generation_source_files(paths)


def test_leaf_missing_on_later_complete_attempt_is_still_hard_failure(paths, monkeypatch):
    paths.processing.mkdir(parents=True)
    anchor = paths.processing / 'a.json'
    anchor.write_bytes(b'{}')
    read, calls = sources.read_bytes_beneath, []

    def remove(root, parts, **kwargs):
        calls.append(parts[-1])
        # 同名仍由末次names列出时，reader的None仍是硬缺失；真实已离开由新原writer回归覆盖。
        content = None if len(calls) == 2 else read(root, parts, **kwargs)
        (paths.processing / 'z.json').write_bytes(b'{}')
        return content

    monkeypatch.setattr(sources, 'read_bytes_beneath', remove)
    with pytest.raises(FileNotFoundError):
        history.generation_source_files(paths)
    assert calls == ['a.json', 'a.json', 'z.json']  # 缺叶后仍完整读取其余责任，再由末names硬拒。


def test_reread_never_refreshes_original_complete_raw_digest(paths, monkeypatch):
    paths.processing.mkdir(parents=True)
    anchor = paths.processing / 'a.json'
    raw = b'{"route":{"c_run_id":"new-c"}}'
    anchor.write_bytes(raw)
    read, calls = sources.read_bytes_beneath, []

    def change(root, parts, **kwargs):
        content = read(root, parts, **kwargs)
        calls.append(1)
        if len(calls) == 1:
            anchor.write_bytes(raw + b' ')
            (paths.processing / 'z.json').write_bytes(raw)
        return content

    monkeypatch.setattr(sources, 'read_bytes_beneath', change)
    files = history.generation_source_files(paths)
    assert files == {'requests/processing/a.json': raw + b' ', 'requests/processing/z.json': raw}
    with pytest.raises(ValueError, match='generation_closed_source_changed'):
        history._require_closed_files({'requests/processing/a.json': hashlib.sha256(raw).hexdigest()}, files, 'new-c')
