# LLM: 仅原终态writer与原读口可控交错，generation有限重读不授关闭/来源权限；默认P6及旧expected仍必须硬拒。
# 模块用途: 验证已列名责任在首读前由原writer归档的窗口及同名仍在、硬错误优先级。
import hashlib

import pytest

from agent_py_agent.agent.gateway_parts import multiprocess_drain_sources as sources
from agent_py_agent.agent.gateway_parts import multiprocess_generation_metadata as history
from agent_py_agent.agent.gateway_parts.io import write_json_file_atomic
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.gateway_parts.queue_service import archive_request


@pytest.fixture
def paths(tmp_path):
    root = tmp_path / 'gateway'
    root.mkdir(mode=0o700)
    return gateway_paths_from_root(root)


def test_original_terminal_writer_before_first_leaf_read_retries_complete_names(paths, monkeypatch):
    paths.processing.mkdir(parents=True)
    request_id = 'background-fixture'
    path = paths.processing / (request_id + '.json')
    write_json_file_atomic(path, {'id': request_id, 'kind': 'background', 'status': 'processing'})
    raw = path.read_bytes()
    read, calls = sources.read_bytes_beneath, []

    def terminalize(root, parts, **kwargs):
        if parts == ('requests', 'processing', path.name):
            calls.append(parts)
            receipt = archive_request(paths, path, paths.done, request_id,
                terminal_response={'id': request_id, 'kind': 'background', 'status': 'done', 'ok': True})
            assert receipt.terminal_committed
        return read(root, parts, **kwargs)

    monkeypatch.setattr(sources, 'read_bytes_beneath', terminalize)
    assert history.generation_source_files(paths) == {}
    assert calls == [('requests', 'processing', path.name)]
    assert not path.exists() and (paths.terminal / path.name).is_file()

    with pytest.raises(ValueError, match='generation_closed_source_changed'):
        history._require_closed_files({'requests/processing/' + path.name: hashlib.sha256(raw).hexdigest()}, {}, 'current-c')


def test_reader_none_with_same_name_in_after_is_hard(paths, monkeypatch):
    paths.processing.mkdir(parents=True)
    (paths.processing / 'a.json').write_bytes(b'{}')
    (paths.processing / 'z.json').write_bytes(b'{}')
    read, calls = sources.read_bytes_beneath, []

    def missing(root, parts, **kwargs):
        calls.append(parts[-1])
        return None if parts[-1] == 'a.json' else read(root, parts, **kwargs)

    monkeypatch.setattr(sources, 'read_bytes_beneath', missing)
    with pytest.raises(FileNotFoundError):
        history.generation_source_files(paths)
    assert calls == ['a.json', 'z.json']


@pytest.mark.parametrize('error', [ValueError('hard'), OSError('hard'), FileNotFoundError('hard')])
def test_later_reader_exception_is_hard_after_none(paths, monkeypatch, error):
    paths.processing.mkdir(parents=True)
    first = paths.processing / 'a.json'
    first.write_bytes(b'{}')
    (paths.processing / 'z.json').write_bytes(b'{}')
    read, calls = sources.read_bytes_beneath, []

    def fail(root, parts, **kwargs):
        calls.append(parts[-1])
        if parts[-1] == first.name:
            first.unlink()
            return read(root, parts, **kwargs)
        raise error

    monkeypatch.setattr(sources, 'read_bytes_beneath', fail)
    with pytest.raises(type(error)) as caught:
        history.generation_source_files(paths)
    assert caught.value is error and calls == ['a.json', 'z.json']


@pytest.mark.parametrize('mutation', ['chain', 'facility', 'unknown'])
def test_end_hard_checks_win_after_none(paths, monkeypatch, mutation):
    paths.processing.mkdir(parents=True)
    first = paths.processing / 'a.json'
    first.write_bytes(b'{}')
    facility = paths.processing / 'a.chunks.jsonl'
    facility.write_bytes(b'{}\n')
    read, calls = sources.read_bytes_beneath, []

    def change(root, parts, **kwargs):
        calls.append(parts[-1])
        first.unlink()
        content = read(root, parts, **kwargs)
        if mutation == 'chain':
            paths.processing.rename(paths.root / 'old-processing')
            paths.processing.mkdir()
        elif mutation == 'facility':
            replacement = paths.root / 'replacement'
            replacement.write_bytes(b'{}\n')
            replacement.replace(facility)
        else:
            (paths.processing / 'unexpected.bin').write_bytes(b'')
        return content

    monkeypatch.setattr(sources, 'read_bytes_beneath', change)
    reason = {'chain': 'drain_namespace_changed', 'facility': 'generation_source_facility_changed',
              'unknown': 'generation_source_name_invalid'}[mutation]
    with pytest.raises(ValueError, match=reason):
        history.generation_source_files(paths)
    assert calls == ['a.json']


def test_default_p6_missing_remains_hard(paths, monkeypatch):
    paths.processing.mkdir(parents=True)
    first = paths.processing / 'a.json'
    first.write_bytes(b'{}')
    read, calls = sources._descriptor_child_bytes, []

    def disappear(descriptor, name):
        calls.append(name)
        first.unlink()
        return read(descriptor, name)

    monkeypatch.setattr(sources, '_descriptor_child_bytes', disappear)
    with pytest.raises(FileNotFoundError):
        sources.original_directory_snapshot(paths.root, ('requests', 'processing'))
    assert calls == ['a.json']


def test_disappearance_every_attempt_keeps_original_three_attempt_bound(paths, monkeypatch):
    paths.processing.mkdir(parents=True)
    (paths.processing / 'a0.json').write_bytes(b'{}')
    read, calls = sources.read_bytes_beneath, []

    def disappear(root, parts, **kwargs):
        name = parts[-1]
        calls.append(name)
        (paths.processing / name).unlink()
        content = read(root, parts, **kwargs)
        (paths.processing / ('a' + str(len(calls)) + '.json')).write_bytes(b'{}')
        return content

    monkeypatch.setattr(sources, 'read_bytes_beneath', disappear)
    with pytest.raises(ValueError, match='drain_namespace_changed'):
        history.generation_source_files(paths)
    assert calls == ['a0.json', 'a1.json', 'a2.json']
