# LLM: 仅合成临时目录、原 writer 与受控线程；不启动 Gateway/native/模型，不把分类当关闭许可；全部行为须最终双OS包pyc实跑。
# 模块用途: 验证 generation 只收已发表责任，同时保完整历史摘要、未知名称及原默认目录快照拒绝。
import hashlib
import os
import threading
from contextlib import contextmanager

import pytest

from agent_py_agent.agent.common import json_io, nofollow_fs
from agent_py_agent.agent.contracts.tool_approval import ToolApprovalRequest
from agent_py_agent.agent.gateway_parts import multiprocess_drain_sources as sources
from agent_py_agent.agent.gateway_parts import multiprocess_generation_metadata as history
from agent_py_agent.agent.gateway_parts.command_stream_protocol import command_approval_path
from agent_py_agent.agent.gateway_parts.control_operation_service import (
    gateway_control_operation_path,
)
from agent_py_agent.agent.gateway_parts.paths import (
    claimed_request_chunk_path,
    gateway_paths_from_root,
)
from agent_py_agent.agent.gateway_parts.permission_bridge import gateway_permission_decision_path
from agent_py_agent.agent.gateway_parts.stream_writer import write_chunk_event
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity


@pytest.fixture
def paths(tmp_path):
    root = tmp_path / 'gateway'
    root.mkdir(mode=0o700)
    return gateway_paths_from_root(root)


# LLM: 线程只运行真实原atomic writer，事件确保观察自然暂存阶段；所有异常保留并在finally释放，不伪造权限或业务产物。
# 函数用途: 在有限等待内观察写入中间态，并确保线程正常结束和异常回传。
@contextmanager
def writing(action, entered, resume):
    errors = []

    def run():
        try:
            action()
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=run, name='namespace-writer')
    worker.start()
    try:
        assert entered.wait(5), 'original writer did not reach controlled stage'
        yield
    finally:
        resume.set()
        worker.join(5)
        assert not worker.is_alive()
        if errors:
            raise errors[0]


def test_original_multiline_chunk_producer_is_not_single_json_responsibility(paths):
    paths.processing.mkdir(parents=True)
    chunk = claimed_request_chunk_path(paths.processing / 'legacy.request-1.json', 'legacy.request-1')
    write_chunk_event(chunk, {'kind': 'first', 'text': '合成一'})
    write_chunk_event(chunk, {'kind': 'second', 'text': '合成二'})
    assert len(chunk.read_bytes().splitlines()) == 2
    files = history.generation_source_files(paths)
    history._require_closed_files({}, files, 'new-c')
    assert files == {}


def test_original_uuid_writer_zero_byte_stage_keeps_old_canonical_raw(paths, monkeypatch):
    paths.processing.mkdir(parents=True)
    path = paths.processing / 'legacy.request-1.json'
    old = b'{"id":"legacy.request-1","route":{"c_run_id":"new-c"}}'
    path.write_bytes(old)
    entered, resume = threading.Event(), threading.Event()
    original = json_io.serialize_private_json_no_newline

    def pause(payload, **kwargs):
        entered.set()
        assert resume.wait(5)
        return original(payload, **kwargs)

    monkeypatch.setattr(json_io, 'serialize_private_json_no_newline', pause)
    with writing(lambda: json_io.write_private_json_file_atomic_no_newline_unlocked(
            path, {'id': 'legacy.request-1', 'route': {'c_run_id': 'new-c'}, 'updated': True}), entered, resume):
        stages = list(path.parent.glob('.*.tmp'))
        assert len(stages) == 1 and stages[0].stat().st_size == 0
        files = history.generation_source_files(paths)
        history._require_closed_files({}, files, 'new-c')
        assert files == {'requests/processing/' + path.name: old}
    assert history.generation_source_files(paths)['requests/processing/' + path.name] != old


def test_original_token_writer_partial_stage_keeps_old_executor_raw(paths, monkeypatch):
    parts = ('workers', 'executors', 'w0', 'a' * 64 + '.json')
    path = paths.root.joinpath(*parts)
    path.parent.mkdir(parents=True)
    old = b'{"c_run_id":"new-c","original":true}'
    path.write_bytes(old)
    entered, resume = threading.Event(), threading.Event()
    original = os.write

    def partial(fd, content):
        if threading.current_thread().name == 'namespace-writer' and not entered.is_set():
            written = original(fd, content[:1])
            entered.set()
            assert resume.wait(5)
            return written
        return original(fd, content)

    monkeypatch.setattr(nofollow_fs.os, 'write', partial)
    with writing(lambda: nofollow_fs.write_bytes_atomic_beneath(
            paths.root, parts, b'{"c_run_id":"new-c","updated":true}'), entered, resume):
        stages = list(path.parent.glob('.*.tmp-*'))
        assert len(stages) == 1 and stages[0].read_bytes() == b'{'
        files = history.generation_source_files(paths)
        history._require_closed_files({}, files, 'new-c')
        assert files == {'/'.join(parts): old}
    assert path.read_bytes() == b'{"c_run_id":"new-c","updated":true}'


@pytest.mark.parametrize('namespace', ['admission', 'processing', 'control', 'executor'])
def test_canonical_raw_and_exact_writer_facilities(paths, namespace):
    parent, name, stage = {
        'admission': (paths.root / 'admission/reservations', 'legacy.request-1.json', '.legacy.request-1.json.' + 'a' * 32 + '.tmp'),
        'processing': (paths.processing, 'legacy.request-1.json', '.legacy.request-1.json.' + 'a' * 32 + '.tmp'),
        'control': (paths.root / 'control_operations', 'gwctl-msg-' + 'a' * 32 + '.json', '.gwctl-msg-' + 'a' * 32 + '.json.' + 'b' * 32 + '.tmp'),
        'executor': (paths.root / 'workers/executors/w0', 'a' * 64 + '.json', '.' + 'a' * 64 + '.json.tmp-' + 'b' * 32),
    }[namespace]
    parent.mkdir(parents=True)
    raw = b'{"route":{"c_run_id":"new-c"},"c_run_id":"new-c"}'
    (parent / name).write_bytes(raw)
    (parent / stage).write_bytes(b'{')
    if namespace in {'processing', 'control'}:
        (parent / (name + '.lock')).write_bytes(b'')
    if namespace == 'executor':
        (parent / 'ledger.lock').write_bytes(b'')
    files = history.generation_source_files(paths)
    assert files == {(parent / name).relative_to(paths.root).as_posix(): raw}
    history._require_closed_files({}, files, 'new-c')


@pytest.mark.parametrize('name', ['unexpected', 'unknown.lock', '.unknown', 'empty.json.' + 'a' * 32 + '.tmp',
                                  '.legacy.json.' + 'A' * 32 + '.tmp', '.legacy.json.' + 'a' * 31 + '.tmp',
                                  '.legacy.json.tmp-' + 'a' * 32, '.legacy.json.' + 'a' * 32 + '.tmp.extra'])
def test_unknown_or_wrong_writer_names_are_not_ignored(paths, name):
    paths.processing.mkdir(parents=True)
    (paths.processing / name).write_bytes(b'')
    with pytest.raises((ValueError, nofollow_fs.NoFollowPathError)):
        history.generation_source_files(paths)


@pytest.mark.parametrize('kind', ['symlink', 'hardlink', 'directory', 'fifo'])
@pytest.mark.parametrize('facility', ['stage', 'chunk', 'lock', 'canonical'])
def test_facility_names_cannot_hide_dangerous_shapes(paths, kind, facility):
    paths.processing.mkdir(parents=True)
    name = {'stage': '.legacy.json.' + 'a' * 32 + '.tmp', 'chunk': 'legacy.chunks.jsonl',
            'lock': 'legacy.json.lock', 'canonical': 'legacy.json'}[facility]
    path = paths.processing / name
    target = paths.root / 'synthetic-target'
    target.write_bytes(b'')
    if kind == 'symlink':
        path.symlink_to(target)
    elif kind == 'hardlink':
        os.link(target, path)
    elif kind == 'directory':
        path.mkdir()
    else:
        os.mkfifo(path)
    with pytest.raises((ValueError, OSError)):
        history.generation_source_files(paths)


@pytest.mark.parametrize('directory', ['.commands', '.approvals'])
def test_fixed_typed_auxiliary_directories_are_not_top_level_request_records(paths, directory):
    paths.processing.mkdir(parents=True)
    if directory == '.commands':
        path = command_approval_path(paths, OwnerIdentity('local', 'main', 'owner'), 'command-legacy')
    else:
        request = ToolApprovalRequest(
            permission_id='permission-legacy', request_id='request-legacy', tool_name='synthetic',
            round=1, call_index=0, title='合成审批', description='仅路径合同',
            binding={'tool_name': 'synthetic', 'run_id': 'run-legacy', 'operation_id': 'operation-legacy',
                     'idempotency_key': 'idempotency-legacy', 'args_hash': 'a' * 64},
            options=({'id': 'yes', 'decision': 'approved', 'label': '同意'},
                     {'id': 'no', 'decision': 'denied', 'label': '拒绝'}),
        )
        path = gateway_permission_decision_path(paths.processing / 'legacy.chunks.jsonl', request)
    path.parent.mkdir(parents=True)
    path.write_bytes(b'synthetic auxiliary, not a responsibility')
    assert path.relative_to(paths.processing).parts[0] == directory
    assert history.generation_source_files(paths) == {}


@pytest.mark.parametrize('directory', ['.commands', '.approvals'])
@pytest.mark.parametrize('kind', ['file', 'symlink'])
def test_fixed_auxiliary_directory_names_still_require_real_directories(paths, directory, kind):
    paths.processing.mkdir(parents=True)
    path = paths.processing / directory
    if kind == 'file':
        path.write_bytes(b'')
    else:
        path.symlink_to(paths.root, target_is_directory=True)
    with pytest.raises((ValueError, OSError)):
        history.generation_source_files(paths)


def test_default_snapshot_keeps_original_full_names_and_raw(paths):
    paths.processing.mkdir(parents=True)
    contents = {'legacy.json': b'{}', 'legacy.chunks.jsonl': b'{}\n{}\n',
                '.legacy.json.' + 'a' * 32 + '.tmp': b'', 'unknown.lock': b''}
    for name, raw in contents.items():
        (paths.processing / name).write_bytes(raw)
    default = sources.original_directory_snapshot(paths.root, ('requests', 'processing'))
    explicit = sources.original_directory_snapshot(paths.root, ('requests', 'processing'), published_sources=False)
    assert default == explicit
    assert dict(default[1]) == contents


def test_default_snapshot_still_rejects_auxiliary_directory(paths):
    (paths.processing / '.commands').mkdir(parents=True)
    with pytest.raises((ValueError, OSError)):
        sources.original_directory_snapshot(paths.root, ('requests', 'processing'))


@pytest.mark.parametrize('parts', [('requests', 'pending'), ('control_operations', 'extra'),
                                  ('workers', 'executors', 'w01'), ('workers', 'executors', 'w-1'), ()])
def test_opt_in_rejects_noncanonical_namespace_even_when_absent(paths, parts):
    with pytest.raises(ValueError):
        sources.original_directory_snapshot(paths.root, parts, published_sources=True)


@pytest.mark.parametrize('raw', [b'', b'{', b'{}\n{}\n', b'[]', b'{"route":{"c_run_id":"old-c"}}'])
def test_bad_canonical_json_and_wrong_generation_remain_refused(paths, raw):
    paths.processing.mkdir(parents=True)
    (paths.processing / 'unknown-but-candidate.json').write_bytes(raw)
    files = history.generation_source_files(paths)
    assert files == {'requests/processing/unknown-but-candidate.json': raw}
    with pytest.raises(ValueError):
        history._require_closed_files({}, files, 'new-c')


@pytest.mark.parametrize('change', ['missing', 'changed', 'facility'])
def test_original_expected_complete_hashes_cannot_be_filtered_or_migrated(paths, change):
    paths.processing.mkdir(parents=True)
    name = 'legacy.json' if change != 'facility' else 'legacy.chunks.jsonl'
    raw = b'{"route":{"c_run_id":"old-c"}}'
    expected = {'requests/processing/' + name: hashlib.sha256(raw).hexdigest()}
    if change == 'changed':
        (paths.processing / name).write_bytes(raw + b' ')
    elif change == 'facility':
        (paths.processing / name).write_bytes(raw)
    with pytest.raises(ValueError, match='generation_closed_source_changed'):
        history._require_closed_files(expected, history.generation_source_files(paths), 'new-c')


@pytest.mark.parametrize('change', ['add', 'remove', 'rename_to_chunk', 'raw', 'facility_replace',
                                   'facility_bad_type', 'parent', 'ancestor', 'root'])
def test_mid_snapshot_change_preserves_source_and_namespace_rejection(paths, monkeypatch, change):
    paths.processing.mkdir(parents=True)
    path = paths.processing / 'legacy.json'
    path.write_bytes(b'{}')
    facility = paths.processing / 'legacy.chunks.jsonl'
    facility.write_bytes(b'{}\n')
    original = sources.read_bytes_beneath
    fired = False

    def altered(root, parts, **kwargs):
        name = parts[-1]
        nonlocal fired
        raw = original(root, parts, **kwargs)
        if not fired and name == path.name:
            fired = True
            if change == 'add':
                (path.parent / 'new.json').write_bytes(b'{}')
            elif change == 'remove':
                path.unlink()
            elif change == 'rename_to_chunk':
                path.replace(path.parent / 'renamed.chunks.jsonl')
            elif change == 'raw':
                path.write_bytes(b'{"changed":true}')
            elif change == 'facility_replace':
                replacement = paths.root / 'replacement'
                replacement.write_bytes(b'{}\n')
                replacement.replace(facility)
            elif change == 'facility_bad_type':
                facility.unlink()
                facility.symlink_to(path)
            elif change == 'parent':
                path.parent.rename(path.parent.with_name('old-processing'))
                path.parent.mkdir()
            elif change == 'ancestor':
                requests = path.parent.parent
                requests.rename(paths.root / 'old-requests')
                requests.mkdir()
                (paths.root / 'old-requests/processing').rename(requests / 'processing')
            else:
                paths.root.rename(paths.root.with_name('old-gateway'))
                paths.root.mkdir()
                (paths.root.with_name('old-gateway') / 'requests').rename(paths.root / 'requests')
        return raw

    if change == 'raw':
        read = nofollow_fs._read_descriptor

        def changing_read(fd, limit):
            content = read(fd, limit)
            path.write_bytes(b'{"changed":true}')
            return content

        monkeypatch.setattr(nofollow_fs, '_read_descriptor', changing_read)
    else:
        monkeypatch.setattr(sources, 'read_bytes_beneath', altered)
    if change in ('add', 'remove', 'rename_to_chunk'):
        # 名称集可整次稳定重读；原旧责任缺失/无合法来源的新责任仍由完整历史门拒绝。
        files = history.generation_source_files(paths)
        expected = {'requests/processing/legacy.json': b'{}', 'requests/processing/new.json': b'{}'} if change == 'add' else {}
        assert files == expected
        reason = 'generation_closed_source_added' if change == 'add' else 'generation_closed_source_changed'
        with pytest.raises(ValueError, match=reason):
            history._require_closed_files({'requests/processing/legacy.json': hashlib.sha256(b'{}').hexdigest()}, files, 'new-c')
    else:
        with pytest.raises((ValueError, OSError)):
            history.generation_source_files(paths)


@pytest.mark.parametrize('change', ['birth', 'delete', 'append'])
def test_exact_auxiliary_lifecycle_does_not_change_responsibility_set(paths, monkeypatch, change):
    paths.processing.mkdir(parents=True)
    canonical = paths.processing / 'legacy.json'
    canonical.write_bytes(b'{}')
    chunk = paths.processing / 'legacy.chunks.jsonl'
    if change != 'birth':
        chunk.write_bytes(b'{}\n')
    original = sources.read_bytes_beneath

    def altered(root, parts, **kwargs):
        name = parts[-1]
        raw = original(root, parts, **kwargs)
        if name == canonical.name:
            if change == 'delete':
                chunk.unlink()
            else:
                with chunk.open('ab') as stream:
                    stream.write(b'{}\n')
        return raw

    monkeypatch.setattr(sources, 'read_bytes_beneath', altered)
    assert history.generation_source_files(paths) == {'requests/processing/legacy.json': b'{}'}


@pytest.mark.parametrize('namespace,name', [
    ('control_operations', 'unknown.json'),
    ('control_operations', '.unknown.json.' + 'a' * 32 + '.tmp'),
    ('control_operations', 'gwctl-msg-' + 'A' * 32 + '.json'),
    ('workers/executors/w0', 'unknown.json'),
    ('workers/executors/w0', '.' + 'A' * 64 + '.json.tmp-' + 'a' * 32),
    ('workers/executors/w0', '.' + 'a' * 64 + '.json.' + 'b' * 32 + '.tmp'),
    ('admission/reservations', 'unexpected.lock'),
])
def test_control_executor_and_admission_do_not_gain_generic_facility_exemptions(paths, namespace, name):
    parent = paths.root / namespace
    parent.mkdir(parents=True)
    (parent / name).write_bytes(b'{}')
    with pytest.raises(ValueError):
        history.generation_source_files(paths)


def test_original_control_locator_and_nonstandard_request_ids_remain_canonical(paths):
    control = gateway_control_operation_path(paths, 'gwctl-msg-' + 'a' * 32)
    control.parent.mkdir(parents=True)
    control.write_bytes(b'{"dispatch":{"c_run_id":"new-c"}}')
    paths.processing.mkdir(parents=True)
    request = paths.processing / 'legacy.with:colon.json'
    request.write_bytes(b'{"route":{"c_run_id":"new-c"}}')
    files = history.generation_source_files(paths)
    assert set(files) == {control.relative_to(paths.root).as_posix(), request.relative_to(paths.root).as_posix()}
    history._require_closed_files({}, files, 'new-c')


def test_only_exact_facility_can_disappear_during_open(paths, monkeypatch):
    paths.processing.mkdir(parents=True)
    stage = paths.processing / ('.legacy.json.' + 'a' * 32 + '.tmp')
    stage.write_bytes(b'')
    original = sources._openat_file

    def removed(parent, name, flags, mode):
        if name == stage.name:
            stage.unlink()
        return original(parent, name, flags, mode)

    monkeypatch.setattr(sources, '_openat_file', removed)
    assert history.generation_source_files(paths) == {}


def test_facility_permission_errors_are_not_treated_as_disappearance(paths, monkeypatch):
    paths.processing.mkdir(parents=True)
    stage = paths.processing / ('.legacy.json.' + 'a' * 32 + '.tmp')
    stage.write_bytes(b'')
    original = sources._openat_file

    def denied(parent, name, flags, mode):
        if name == stage.name:
            raise PermissionError('controlled read refusal')
        return original(parent, name, flags, mode)

    monkeypatch.setattr(sources, '_openat_file', denied)
    with pytest.raises(PermissionError):
        history.generation_source_files(paths)


def test_canonical_read_limit_is_not_truncated_or_ignored(paths):
    paths.processing.mkdir(parents=True)
    (paths.processing / 'legacy.json').write_bytes(b' ' * (sources._DRAIN_SOURCE_READ_MAX_BYTES + 1))
    with pytest.raises((ValueError, OSError)):
        history.generation_source_files(paths)


@pytest.mark.parametrize('change', ['grow', 'replace'])
def test_repeated_fixed_directory_identity_is_checked_without_freezing_internal_contents(paths, monkeypatch, change):
    paths.processing.mkdir(parents=True)
    path = paths.processing / 'legacy.json'
    path.write_bytes(b'{}')
    directory = paths.processing / '.commands'
    directory.mkdir()
    original = sources.read_bytes_beneath

    def altered(root, parts, **kwargs):
        name = parts[-1]
        content = original(root, parts, **kwargs)
        if change == 'grow':
            (directory / 'synthetic-child').mkdir()
        else:
            directory.rename(paths.root / 'old-commands')
            directory.mkdir()
        return content

    monkeypatch.setattr(sources, 'read_bytes_beneath', altered)
    if change == 'replace':
        with pytest.raises(ValueError, match='generation_source_facility_changed'):
            history.generation_source_files(paths)
    else:
        assert history.generation_source_files(paths) == {'requests/processing/legacy.json': b'{}'}



def test_original_writer_publication_during_snapshot_changes_responsibility_names(paths, monkeypatch):
    paths.processing.mkdir(parents=True)
    anchor = paths.processing / 'anchor.json'
    anchor.write_bytes(b'{}')
    canonical = paths.processing / 'new.json'
    entered, resume, published = threading.Event(), threading.Event(), threading.Event()
    serialize = json_io.serialize_private_json_no_newline
    read = sources.read_bytes_beneath

    def paused(payload, **kwargs):
        entered.set()
        assert resume.wait(5)
        return serialize(payload, **kwargs)

    def write():
        try:
            json_io.write_private_json_file_atomic_no_newline_unlocked(canonical, {'id': 'new'})
        finally:
            published.set()

    def interleaved(root, parts, **kwargs):
        name = parts[-1]
        content = read(root, parts, **kwargs)
        if name == anchor.name:
            resume.set()
            assert published.wait(5)
        return content

    monkeypatch.setattr(json_io, 'serialize_private_json_no_newline', paused)
    monkeypatch.setattr(sources, 'read_bytes_beneath', interleaved)
    with writing(write, entered, resume):
        files = history.generation_source_files(paths)
    assert files == {'requests/processing/anchor.json': b'{}', 'requests/processing/new.json': b'{\n  "id": "new"\n}'}
    # 稳定读取并不为新责任授代次；该合成writer正文没有合法route，原完整历史门仍拒绝。
    with pytest.raises(ValueError, match='generation_closed_source_added'):
        history._require_closed_files({'requests/processing/anchor.json': hashlib.sha256(b'{}').hexdigest()}, files, 'new-c')
    assert canonical.read_bytes() == b'{\n  "id": "new"\n}'


@pytest.mark.parametrize('namespace', ['admission', 'processing', 'control', 'executor'])
@pytest.mark.parametrize('window', ['A', 'B', 'after_read'])
def test_generation_canonical_leaf_reads_stable_original_atomic_replacement(paths, monkeypatch, namespace, window):
    supported = nofollow_fs._supports_dir_fd()
    monkeypatch.setattr(nofollow_fs, '_supports_dir_fd', lambda: supported)
    parent, name = {
        'admission': (paths.root / 'admission/reservations', 'legacy.json'),
        'processing': (paths.processing, 'legacy.json'),
        'control': (paths.root / 'control_operations', 'gwctl-msg-' + 'a' * 32 + '.json'),
        'executor': (paths.root / 'workers/executors/w0', 'a' * 64 + '.json'),
    }[namespace]
    parent.mkdir(parents=True)
    path = parent / name
    old = {'route': {'c_run_id': 'new-c'}, 'c_run_id': 'new-c', 'version': 1}
    new = {**old, 'version': 2}
    json_io.write_private_json_file_atomic_no_newline_unlocked(path, old)
    original_open, original_read = os.open, nofollow_fs._read_descriptor
    attempts, reads = [], []
    fired = False

    def publish():
        nonlocal fired
        fired = True
        json_io.write_private_json_file_atomic_no_newline_unlocked(path, new)

    def opening(child, flags, mode=0o777, **kwargs):
        target = child == name and flags & os.O_ACCMODE == os.O_RDONLY
        if target:
            attempts.append(child)
            if window == 'A' and not fired:
                publish()
        fd = original_open(child, flags, mode, **kwargs)
        if target and window == 'B' and not fired:
            publish()
        return fd

    def reading(fd, limit):
        content = original_read(fd, limit)
        if os.fstat(fd).st_ino == path.stat().st_ino or not fired:
            reads.append(content)
            if window == 'after_read' and not fired:
                publish()
        return content

    monkeypatch.setattr(os, 'open', opening)
    monkeypatch.setattr(nofollow_fs, '_read_descriptor', reading)
    monkeypatch.setattr(sources, '_read_descriptor', reading)
    files = history.generation_source_files(paths)
    assert fired and files == {path.relative_to(paths.root).as_posix(): path.read_bytes()}
    assert b'"version": 2' in next(iter(files.values()))
    assert len(attempts) == 2
    history._require_closed_files({}, files, 'new-c')


def test_generation_missing_published_leaf_is_not_an_empty_snapshot(paths, monkeypatch):
    paths.processing.mkdir(parents=True)
    path = paths.processing / 'legacy.json'
    path.write_bytes(b'{}')
    original = sources.read_bytes_beneath
    observations, reads = [], []

    def republished(root, parts, **kwargs):
        if parts == ('requests', 'processing', 'legacy.json'):
            path.unlink()
            content = original(root, parts, **kwargs)
            observations.append(content)
            json_io.write_private_json_file_atomic_no_newline_unlocked(path, {'version': 2})
            reads.append(parts)
            return content
        return original(root, parts, **kwargs)

    monkeypatch.setattr(sources, 'read_bytes_beneath', republished)
    with pytest.raises(FileNotFoundError):
        history.generation_source_files(paths)
    assert observations == [None] and reads == [('requests', 'processing', 'legacy.json')]
    assert path.read_bytes() == b'{\n  "version": 2\n}'


def test_generation_confirmed_departure_discards_whole_round_with_same_parent(paths, monkeypatch):
    paths.processing.mkdir(parents=True)
    path = paths.processing / 'legacy.json'
    path.write_bytes(b'{}')
    survivor = paths.processing / 'survivor.json'
    survivor.write_bytes(b'{}')
    original_names, original_read = sources._generation_source_names, sources.read_bytes_beneath
    observations, parents, reads = [], [], []

    def disappearing(root, parts, descriptor):
        result = original_names(root, parts, descriptor)
        if parts == ('requests', 'processing'):
            observations.append(result[0])
            parents.append(descriptor)
            if len(observations) == 1:
                path.unlink()
        return result

    def reading(root, parts, **kwargs):
        reads.append(parts)
        return original_read(root, parts, **kwargs)

    monkeypatch.setattr(sources, '_generation_source_names', disappearing)
    monkeypatch.setattr(sources, 'read_bytes_beneath', reading)
    files = history.generation_source_files(paths)
    assert observations == [('legacy.json', 'survivor.json'), ('survivor.json',),
                            ('survivor.json',), ('survivor.json',)]
    assert len(set(parents)) == 1 and not path.exists()
    assert reads.count(('requests', 'processing', 'legacy.json')) == 1
    assert reads.count(('requests', 'processing', 'survivor.json')) == 2
    assert files == {'requests/processing/survivor.json': b'{}'}
    with pytest.raises(ValueError):
        history._require_closed_files({'requests/processing/legacy.json': hashlib.sha256(b'{}').hexdigest()},
                                      files, 'new-c')


def test_generation_continuous_atomic_replacement_exhausts_original_three_attempts(paths, monkeypatch):
    supported = nofollow_fs._supports_dir_fd()
    monkeypatch.setattr(nofollow_fs, '_supports_dir_fd', lambda: supported)
    paths.processing.mkdir(parents=True)
    path = paths.processing / 'legacy.json'
    path.write_bytes(b'{}')
    original = os.open
    attempts, parents = [], []

    def opening(name, flags, mode=0o777, **kwargs):
        if name == path.name and flags & os.O_ACCMODE == os.O_RDONLY:
            attempts.append(name)
            parents.append(kwargs.get('dir_fd'))
            json_io.write_private_json_file_atomic_no_newline_unlocked(path, {'version': len(attempts)})
        return original(name, flags, mode, **kwargs)

    monkeypatch.setattr(os, 'open', opening)
    with pytest.raises(nofollow_fs.NoFollowPathError):
        history.generation_source_files(paths)
    assert attempts == [path.name] * 3 and len(set(parents)) == 1
