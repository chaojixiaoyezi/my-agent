"""W9：用临时会话和真实回收事务复现持久身份漂移、补偿竞争与跨卷失败。"""
from __future__ import annotations

import errno
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.common import nofollow_fs
from agent_py_agent.agent.conversation import ConversationStore, message_scan
from agent_py_agent.agent.conversation.compact_checkpoint import (
    CompactCheckpointRequest,
    write_compact_checkpoint,
)
from agent_py_agent.agent.conversation.models import ConversationCompactCommit
from agent_py_agent.agent.gateway_parts.io import update_json_file_atomic
from agent_py_agent.agent.memory_store import retention_apply as apply
from agent_py_agent.agent.memory_store import retention_scan as scan
from agent_py_agent.agent.memory_store.retention_models import MemoryRetentionAction
from agent_py_agent.agent.runtime_errors import DataCorruptionError
from agent_py_agent.tests.test_compact_scoped_checkpoint import compact_case as _compact_case
from agent_py_agent.tests.test_transcript_cold_rotation import _commit_rotation

compact_case = _compact_case
NOW = datetime(2026, 10, 31, tzinfo=timezone.utc)
OLD = datetime(2024, 1, 1, tzinfo=timezone.utc)


def _remount(path):
    """移动副本保留字节，改变真实 inode，并模拟清单中的设备号属于上次启动。"""
    manifest_path = path.with_suffix('.manifest.json')
    state = json.loads(manifest_path.read_bytes())

    def drift(record):
        if not isinstance(record, dict):
            return
        for name in ('hot_identity', 'old_identity', 'new_identity'):
            if name in record:
                record[name] = [record[name][0] + 1000, record[name][1] + 1000]
        for name in ('before', 'after'):
            drift(record.get(name))

    drift(state)
    manifest_path.write_text(json.dumps(state) + '\n')
    stage = path.with_suffix('.remount-test')
    stage.write_bytes(path.read_bytes())
    stage.replace(path)
    return state


def _pending_rotation(case, monkeypatch, point):
    original = nofollow_fs.write_text_atomic_beneath
    injected = []
    target = f'{case.thread_id}.manifest.json'

    def fail(root, parts, text, **kwargs):
        if tuple(parts) == (target,) and not injected:
            prepared = json.loads(text).get('phase') == 'prepared'
            if (point == 'old' and prepared) or (point == 'new' and not prepared):
                injected.append(True)
                if prepared:
                    original(root, parts, text, **kwargs)
                raise OSError('prepared failure')
        return original(root, parts, text, **kwargs)

    with monkeypatch.context() as fault:
        fault.setattr(nofollow_fs, 'write_text_atomic_beneath', fail)
        result = _commit_rotation(case, monkeypatch)
    assert injected
    return result


def test_read_and_append_accept_remounted_committed_layout(compact_case, monkeypatch):
    thread, _messages, original, path = _commit_rotation(compact_case, monkeypatch)
    before = _remount(path)
    manifest_bytes = path.with_suffix('.manifest.json').read_bytes()
    with message_scan.TranscriptReader(path) as reader:
        assert reader.read() == original
    assert path.with_suffix('.manifest.json').read_bytes() == manifest_bytes, '读取不能偷偷写清单'
    late = compact_case.store.messages.append({'thread_id': thread.thread_id, 'role': 'user', 'content': '重挂载后追加'})
    state = json.loads(path.with_suffix('.manifest.json').read_bytes())
    assert state['generation'] == before['generation']
    assert state['hot_identity'] == [path.stat().st_dev, path.stat().st_ino]
    with message_scan.TranscriptReader(path) as reader:
        whole = reader.read()
    assert whole.startswith(original) and whole.count(late.message_id.encode()) == 1


@pytest.mark.parametrize('point', ['old', 'new'])
def test_prepared_recovery_accepts_remount_and_keeps_late_append(compact_case, monkeypatch, point):
    thread, _messages, original, path = _pending_rotation(compact_case, monkeypatch, point)
    _remount(path)
    with message_scan.TranscriptReader(path) as reader:
        assert reader.read() == original
    late = compact_case.store.messages.append({'thread_id': thread.thread_id, 'role': 'assistant', 'content': '恢复后的尾行'})
    state = json.loads(path.with_suffix('.manifest.json').read_bytes())
    assert state.get('phase') != 'prepared'
    with message_scan.TranscriptReader(path) as reader:
        whole = reader.read()
    assert whole.startswith(original) and whole.count(late.message_id.encode()) == 1


def test_next_rotation_writes_portable_generation_after_remount(compact_case, monkeypatch):
    case = compact_case
    thread, _messages, original, path = _commit_rotation(case, monkeypatch)
    old_base = json.loads(path.with_suffix('.manifest.json').read_bytes())['hot_base']
    _remount(path)
    new_rows = [case.store.messages.append({
        'thread_id': thread.thread_id, 'role': 'user', 'content': f'next-{number}:' + '正文' * 100,
    }) for number in range(5)]
    all_rows, errors = case.store.messages.recent_report(thread.thread_id, limit=0)
    assert not errors
    boundary = case.store.messages.byte_offset_after(thread.thread_id, new_rows[-2].message_id)
    thread = case.store.threads.require(thread.thread_id)
    checkpoint = write_compact_checkpoint(case.agent, CompactCheckpointRequest(
        thread=thread, summary='第二轮覆盖', operation_evidence={}, compact_rows=tuple(all_rows[:-1]),
        retained_tail=(new_rows[-1],), source_end_byte_offset=boundary,
        projected_tokens_before=900, projected_tokens_after=300, policy=case.policy, forced=False,
    ))
    updated = case.store.threads.update_compact_state(thread.thread_id, commit=ConversationCompactCommit(
        summary='第二轮覆盖', operation_evidence={}, checkpoint_id=checkpoint,
        compacted_through_message_id=new_rows[-2].message_id, compacted_through_byte_offset=boundary,
        source_messages=len(all_rows) - 1, source_tool_pairs=0,
    ), expected_generation=thread.compact_generation)
    state = json.loads(path.with_suffix('.manifest.json').read_bytes())
    assert 'compact_transcript_maintenance' not in updated.metadata
    assert state['generation'] == updated.compact_generation and state['hot_base'] > old_base
    with message_scan.TranscriptReader(path) as reader:
        whole = reader.read()
    assert whole.startswith(original) and whole.count(new_rows[-1].message_id.encode()) == 1


@pytest.mark.parametrize('point', ['committed', 'old', 'new'])
@pytest.mark.parametrize('damage', ['prefix', 'generation'])
def test_portable_layout_still_rejects_tampering(compact_case, monkeypatch, point, damage):
    if point == 'committed':
        _thread, _messages, _original, path = _commit_rotation(compact_case, monkeypatch)
    else:
        _thread, _messages, _original, path = _pending_rotation(compact_case, monkeypatch, point)
    if damage == 'prefix':
        raw = path.read_bytes()
        changed = raw.replace(b'row-', b'bad-', 1)
        assert changed != raw and len(changed) == len(raw)
        path.write_bytes(changed)
    else:
        manifest = path.with_suffix('.manifest.json')
        state = json.loads(manifest.read_bytes())
        (state if point == 'committed' else state['after'])['generation'] += 1
        manifest.write_text(json.dumps(state) + '\n')
    with pytest.raises(DataCorruptionError):
        with message_scan.TranscriptReader(path) as reader:
            reader.read()
    with pytest.raises(DataCorruptionError):
        compact_case.store.messages.append({'thread_id': compact_case.thread_id, 'role': 'user', 'content': '不应追加'})


# LLM: 归档事务只针对非活跃线程；保留真实绑定保护，不在故障注入前被保护门短路。
# 函数用途: 构造可以合法进入回收事务的临时过期对象。
def _retention_case(tmp_path, kind):
    owner = tmp_path / 'owner'
    owner.mkdir()
    home = SimpleNamespace(owner_home_dir=owner, owner_trash_dir=owner / 'trash')
    destination = home.owner_trash_dir / 'retention' / kind / 'case'
    common = dict(action_id='w9', category=kind, reason='expired', destination=destination,
                  cutoff_timestamp=NOW.timestamp() - 86400)
    if kind == 'conversation':
        root = owner / 'conversations'
        store = ConversationStore(root)
        thread = store.threads.get_or_create({
            'canonical_user_id': 'old-user', 'channel': 'internal',
            'channel_conversation_id': 'old-chat', 'channel_user_id': 'old-user', 'now': OLD.timestamp(),
        })
        store.messages.append({'thread_id': thread.thread_id, 'role': 'user', 'content': '原文', 'now': OLD.timestamp()})
        for name in ('channel_bindings.json', 'user_latest_threads.json'):
            update_json_file_atomic(root / name, lambda current: {
                key: value for key, value in current.items() if str(value) != thread.thread_id
            })
            assert thread.thread_id not in json.loads((root / name).read_bytes()).values()
        thread = store.threads.require(thread.thread_id)
        related, errors = scan._conversation_related_paths(root, thread)
        assert not errors
        source = store.storage.thread_path(thread.thread_id)
        action = MemoryRetentionAction(**common, operation='conversation_trash', path=source,
            record_id=thread.thread_id, related_paths=related, authority_path=source,
            authority_fingerprint=scan.file_fingerprint(source), source_fingerprint=scan.paths_fingerprint(related))
        executor = apply._trash_conversation
    elif kind == 'tree':
        source = owner / 'task'
        source.mkdir()
        (source / 'result.txt').write_text('保留数据')
        authority = source / 'state.json'
        authority.write_text(json.dumps({'status': 'DONE', 'updated_at': OLD.isoformat()}))
        action = MemoryRetentionAction(**common, operation='trash_tree', path=source,
            authority_path=authority, authority_status='DONE', authority_fingerprint=scan.file_fingerprint(authority),
            source_fingerprint=scan.tree_fingerprint(source))
        executor = apply._trash_tree
    else:
        source = owner / 'files' / 'old.txt'
        source.parent.mkdir()
        source.write_text('保留原文')
        os.utime(source, (OLD.timestamp(), OLD.timestamp()))
        action = MemoryRetentionAction(**common, operation='trash_file', path=source,
            source_fingerprint=scan.file_fingerprint(source))
        executor = apply._trash_file
    return home, action, executor


def _original_bytes(action):
    paths = action.related_paths or (tuple(action.path.rglob('*')) if action.path.is_dir() else (action.path,))
    return {path: path.read_bytes() for path in paths if path.is_file()}


@pytest.mark.parametrize('rebind', [False, True])
def test_index_compensation_is_delta_based_and_keeps_interleaved_writes(tmp_path, monkeypatch, rebind):
    home, action, executor = _retention_case(tmp_path, 'conversation')
    root = action.path.parent.parent
    indexes = (root / 'channel_bindings.json', root / 'user_latest_threads.json')
    for path in indexes:
        update_json_file_atomic(path, lambda current: {**current, 'untouched': 'stable'})
    originals = _original_bytes(action)
    original_update, original_write = apply.update_json_file_atomic, apply._write_tombstone
    injected = set()

    def update(path, updater, **kwargs):
        if path in indexes and path not in injected:
            injected.add(path)
            # 模拟通过绑定检查后才到达的并发写入，不预先把候选变成受保护的活跃线程。
            original_update(path, lambda current: {
                **current, 'restore-me': action.record_id, 'rebound': action.record_id,
                'late-target': action.record_id,
            })
        return original_update(path, updater, **kwargs)

    def fail(destination, **kwargs):
        if kwargs['status'] == 'trashed':
            for path in indexes:
                original_update(path, lambda current: {
                    **current, 'concurrent-new': 'new-thread', **({'rebound': 'replacement-thread'} if rebind else {}),
                })
            raise OSError('final tombstone failure')
        return original_write(destination, **kwargs)

    monkeypatch.setattr(apply, 'update_json_file_atomic', update)
    monkeypatch.setattr(apply, '_write_tombstone', fail)
    with pytest.raises(OSError, match='final tombstone failure'):
        executor(home=home, action=action, now=NOW)
    assert injected == set(indexes)
    for path in indexes:
        current = json.loads(path.read_bytes())
        assert current['concurrent-new'] == 'new-thread'
        assert current['restore-me'] == current['late-target'] == action.record_id
        assert current['rebound'] == ('replacement-thread' if rebind else action.record_id)
        assert current['untouched'] == 'stable'
    assert {path: path.read_bytes() for path in originals} == originals


@pytest.mark.parametrize('kind', ['tree', 'file', 'conversation'])
@pytest.mark.parametrize('existing', [False, True])
@pytest.mark.parametrize('occupied', [False, True])
def test_first_tombstone_failure_only_prunes_own_empty_container(tmp_path, monkeypatch, kind, existing, occupied):
    home, action, executor = _retention_case(tmp_path, kind)
    destination = action.destination
    originals = _original_bytes(action)
    if existing:
        destination.mkdir(parents=True)
        if occupied:
            (destination / 'keep.txt').write_text('已有文件')

    def fail(container, **_kwargs):
        if occupied:
            (container / 'keep.txt').write_text('已有文件')
        raise OSError('first tombstone failure')

    monkeypatch.setattr(apply, '_write_tombstone', fail)
    with pytest.raises(OSError):
        executor(home=home, action=action, now=NOW)
    assert destination.exists() is (existing or occupied)
    if occupied:
        assert (destination / 'keep.txt').read_text() == '已有文件'
    assert {path: path.read_bytes() for path in originals} == originals


@pytest.mark.parametrize('kind', ['tree', 'file', 'conversation'])
def test_exdev_is_refused_without_copying_or_removing_source(tmp_path, monkeypatch, kind):
    home, action, executor = _retention_case(tmp_path, kind)
    originals = _original_bytes(action)
    original_rename, original_replace = os.rename, os.replace
    calls = []

    def reject(operation):
        def move(source, destination, *args, **kwargs):
            if Path(source) == action.path:
                calls.append((source, destination))
                raise OSError(errno.EXDEV, 'cross-device link')
            return operation(source, destination, *args, **kwargs)
        return move

    monkeypatch.setattr(os, 'rename', reject(original_rename))
    monkeypatch.setattr(os, 'replace', reject(original_replace))
    with pytest.raises(OSError) as failure:
        executor(home=home, action=action, now=NOW)
    assert failure.value.errno == errno.EXDEV and calls
    assert {path: path.read_bytes() for path in originals} == originals
    if action.destination.exists():
        assert not (action.destination / 'payload').exists()


@pytest.mark.parametrize('status', ['moving', 'recovery_required', 'trashed', '', 'unknown'])
def test_trash_age_scan_only_collects_committed_trashed_records(tmp_path, status):
    home = SimpleNamespace(owner_trash_dir=tmp_path / 'trash')
    container = home.owner_trash_dir / 'retention' / 'conversation' / 'old'
    container.mkdir(parents=True)
    (container / 'payload').write_text('唯一完整副本')
    (container / 'tombstone.json').write_text(json.dumps({
        'moved_at': OLD.isoformat(), 'status': status, 'source_path': str(tmp_path / 'original'),
    }))
    errors = []
    actions = scan._trash_actions(home=home, days=1, now=NOW, errors=errors,
                                 protection=(scan._RetentionPins(tmp_path), True))
    assert bool(actions) is (status == 'trashed')
    assert (container / 'payload').read_text() == '唯一完整副本'


@pytest.mark.parametrize('kind', ['tree', 'file', 'conversation'])
def test_rollback_failure_keeps_recoverable_payload_and_nonpurgeable_tombstone(tmp_path, monkeypatch, kind):
    home, action, executor = _retention_case(tmp_path, kind)
    originals = _original_bytes(action)
    original_write, original_rename, original_replace = apply._write_tombstone, os.rename, os.replace
    failures = []
    payload = action.destination / 'payload'
    if kind == 'conversation':
        root = action.path.parent.parent
        indexes = (root / 'channel_bindings.json', root / 'user_latest_threads.json')
        original_update = apply.update_json_file_atomic
        injected = set()

        def update(path, updater, **kwargs):
            if path in indexes and path not in injected:
                injected.add(path)
                original_update(path, lambda current: {**current, 'late-binding': action.record_id})
            return original_update(path, updater, **kwargs)

        monkeypatch.setattr(apply, 'update_json_file_atomic', update)

    def fail_final(container, **kwargs):
        if kwargs['status'] == 'trashed':
            failures.append('commit')
            raise OSError('commit failure')
        return original_write(container, **kwargs)

    def reject_restore(operation):
        def move(source, destination, *args, **kwargs):
            if Path(source) == payload or payload in Path(source).parents:
                failures.append('rollback')
                raise OSError(errno.EACCES, 'rollback unavailable')
            return operation(source, destination, *args, **kwargs)
        return move

    monkeypatch.setattr(apply, '_write_tombstone', fail_final)
    monkeypatch.setattr(os, 'rename', reject_restore(original_rename))
    monkeypatch.setattr(os, 'replace', reject_restore(original_replace))
    with pytest.raises(OSError):
        executor(home=home, action=action, now=NOW)
    assert 'commit' in failures and 'rollback' in failures
    marker = json.loads((action.destination / 'tombstone.json').read_bytes())
    assert marker['status'] in {'moving', 'recovery_required'}
    for source, raw in originals.items():
        if kind == 'conversation':
            target = payload / source.relative_to(action.path.parent.parent)
        elif kind == 'tree':
            target = payload / source.relative_to(action.path)
        else:
            target = payload
        assert (source.exists() and source.read_bytes() == raw) or (target.exists() and target.read_bytes() == raw)
    assert scan._trash_actions(home=home, days=1, now=NOW, errors=[],
                               protection=(scan._RetentionPins(home.owner_home_dir), True)) == []
    if kind == 'conversation':
        root = action.path.parent.parent
        assert injected == set(indexes)
        assert action.record_id in json.loads((root / 'user_latest_threads.json').read_bytes()).values()


@pytest.mark.parametrize('status', ['moving', 'recovery_required'])
def test_delete_executor_rechecks_tombstone_commit_state(tmp_path, status):
    home = SimpleNamespace(owner_trash_dir=tmp_path / 'trash')
    container = home.owner_trash_dir / 'retention' / 'tree' / 'old'
    container.mkdir(parents=True)
    payload = container / 'payload'
    payload.write_text('不应删除')
    tombstone = container / 'tombstone.json'
    tombstone.write_text(json.dumps({'moved_at': OLD.isoformat(), 'status': status}))
    action = MemoryRetentionAction(action_id='old', category='trash', operation='delete_tree', path=container,
        reason='expired', authority_path=tombstone, authority_fingerprint=scan.file_fingerprint(tombstone),
        source_fingerprint=scan.tree_fingerprint(container), cutoff_timestamp=NOW.timestamp())
    assert apply._delete_tree(action).status == 'state_changed'
    assert payload.read_text() == '不应删除'


def test_partial_conversation_exdev_restores_already_moved_members(tmp_path, monkeypatch):
    home, action, executor = _retention_case(tmp_path, 'conversation')
    originals = _original_bytes(action)
    assert len(originals) >= 2
    reject_source = action.related_paths[1]
    original_rename, original_replace = os.rename, os.replace
    failed = []

    def reject(operation):
        def move(source, destination, *args, **kwargs):
            if Path(source) == reject_source:
                # 此异常发生在索引删除之前；资格检查后才到达的新绑定必须保持原样。
                latest = action.path.parent.parent / 'user_latest_threads.json'
                apply.update_json_file_atomic(latest, lambda current: {**current, 'late-binding': action.record_id})
                failed.append(source)
                raise OSError(errno.EXDEV, 'later member crosses volume')
            return operation(source, destination, *args, **kwargs)
        return move

    monkeypatch.setattr(os, 'rename', reject(original_rename))
    monkeypatch.setattr(os, 'replace', reject(original_replace))
    with pytest.raises(OSError) as failure:
        executor(home=home, action=action, now=NOW)
    assert failure.value.errno == errno.EXDEV and failed
    assert {path: path.read_bytes() for path in originals} == originals
    latest = action.path.parent.parent / 'user_latest_threads.json'
    assert action.record_id in json.loads(latest.read_bytes()).values()


@pytest.mark.parametrize('kind', ['tree', 'file', 'conversation'])
def test_compensation_never_replaces_a_recreated_source(tmp_path, monkeypatch, kind):
    home, action, executor = _retention_case(tmp_path, kind)
    originals = _original_bytes(action)
    original_write = apply._write_tombstone
    recreated = []

    def fail_commit(container, **kwargs):
        if kwargs['status'] == 'trashed':
            action.path.parent.mkdir(parents=True, exist_ok=True)
            action.path.write_text('并发重建的源文件')
            recreated.append(action.path)
            raise OSError('commit failure after concurrent creation')
        return original_write(container, **kwargs)

    monkeypatch.setattr(apply, '_write_tombstone', fail_commit)
    with pytest.raises(OSError, match='commit failure after concurrent creation'):
        executor(home=home, action=action, now=NOW)
    assert recreated == [action.path] and action.path.read_text() == '并发重建的源文件'
    marker = json.loads((action.destination / 'tombstone.json').read_bytes())
    assert marker['status'] == 'recovery_required'
    payload = action.destination / 'payload'
    source = action.path if kind != 'tree' else next(iter(originals))
    target = (payload / source.relative_to(action.path.parent.parent) if kind == 'conversation' else
              payload / source.relative_to(action.path) if kind == 'tree' else payload)
    assert target.read_bytes() == originals[source]


def test_legacy_manifest_is_upgraded_only_inside_message_writer(compact_case, monkeypatch):
    thread, _messages, original, path = _commit_rotation(compact_case, monkeypatch)
    manifest = path.with_suffix('.manifest.json')
    legacy = json.loads(manifest.read_bytes())
    legacy.pop('hot_frontier')
    legacy.pop('rotation_id')
    manifest.write_text(json.dumps(legacy) + '\n')
    legacy_bytes = manifest.read_bytes()
    with message_scan.TranscriptReader(path) as reader:
        assert reader.read() == original
    assert manifest.read_bytes() == legacy_bytes
    compact_case.store.messages.append({'thread_id': thread.thread_id, 'role': 'user', 'content': '升级旧清单'})
    upgraded = json.loads(manifest.read_bytes())
    assert upgraded['hot_frontier']['bytes'] > 0 and upgraded['rotation_id']
    _remount(path)
    with message_scan.TranscriptReader(path) as reader:
        assert reader.read().startswith(original)


@pytest.mark.parametrize('damage', ['null_old_frontier', 'missing_old_frontier', 'null_after'])
def test_prepared_null_evidence_never_downgrades_to_unverified_layout(compact_case, monkeypatch, damage):
    point = 'new' if damage == 'null_after' else 'old'
    _thread, _messages, _original, path = _pending_rotation(compact_case, monkeypatch, point)
    manifest = path.with_suffix('.manifest.json')
    state = json.loads(manifest.read_bytes())
    if damage == 'null_old_frontier':
        state['old_frontier'] = None
        raw = path.read_bytes()
        path.write_bytes(raw.replace(b'row-0', b'bad-0', 1))
    elif damage == 'missing_old_frontier':
        del state['old_frontier']
    else:
        state['after'] = None
    manifest.write_text(json.dumps(state) + '\n')
    with pytest.raises(DataCorruptionError):
        with message_scan.TranscriptReader(path) as reader:
            reader.read()
    with pytest.raises(DataCorruptionError):
        compact_case.store.messages.append({'thread_id': compact_case.thread_id, 'role': 'user', 'content': '拒绝追加'})


@pytest.mark.parametrize('kind', ['tree', 'file', 'conversation'])
def test_postpublish_tombstone_error_and_failed_recovery_never_authorize_purge(tmp_path, monkeypatch, kind):
    home, action, executor = _retention_case(tmp_path, kind)
    originals = _original_bytes(action)
    original_write, original_move = apply._write_tombstone, apply._move_retention_payload
    failures = []
    payload = action.destination / 'payload'

    def fail_write(container, **kwargs):
        if kwargs['status'] == 'trashed':
            original_write(container, **kwargs)
            failures.append('published')
            raise OSError('fsync failed after tombstone replacement')
        if kwargs['status'] == 'recovery_required':
            failures.append('recovery-marker')
            raise OSError('recovery marker cannot be written')
        return original_write(container, **kwargs)

    def fail_rollback(source, target):
        if source == payload or payload in source.parents:
            failures.append('rollback')
            raise OSError('cannot restore payload')
        return original_move(source, target)

    monkeypatch.setattr(apply, '_write_tombstone', fail_write)
    monkeypatch.setattr(apply, '_move_retention_payload', fail_rollback)
    with pytest.raises(OSError, match='fsync failed after tombstone replacement'):
        executor(home=home, action=action, now=OLD)
    assert set(failures) == {'published', 'rollback', 'recovery-marker'}
    for source, raw in originals.items():
        target = (payload / source.relative_to(action.path.parent.parent) if kind == 'conversation' else
                  payload / source.relative_to(action.path) if kind == 'tree' else payload)
        assert target.read_bytes() == raw
    assert scan._trash_actions(home=home, days=1, now=NOW, errors=[],
                               protection=(scan._RetentionPins(home.owner_home_dir), True)) == []
    tombstone = action.destination / 'tombstone.json'
    purge = MemoryRetentionAction(action_id='purge', category='trash', operation='delete_tree',
        path=action.destination, reason='expired', authority_path=tombstone,
        authority_fingerprint=scan.file_fingerprint(tombstone),
        source_fingerprint=scan.tree_fingerprint(action.destination), cutoff_timestamp=NOW.timestamp())
    assert apply._delete_tree(purge).status == 'state_changed'


@pytest.mark.parametrize('published', [False, True])
def test_unacknowledged_index_removal_does_not_resurrect_concurrent_deletion(tmp_path, monkeypatch, published):
    home, action, executor = _retention_case(tmp_path, 'conversation')
    root = action.path.parent.parent
    path = root / 'channel_bindings.json'
    original_update = apply.update_json_file_atomic
    failures = []

    def fail_update(target, updater, **kwargs):
        if target != path or failures:
            return original_update(target, updater, **kwargs)
        failures.append(target)
        # 绑定仅在资格检查后、原索引事务开始前到达，仍覆盖未确认删除的补偿竞争。
        original_update(path, lambda current: {**current, 'victim': action.record_id})
        if published:
            original_update(target, updater, **kwargs)
        else:
            def fail_before_replace(current):
                updater(current)
                raise OSError('index replacement not acknowledged')
            with pytest.raises(OSError, match='not acknowledged'):
                original_update(target, fail_before_replace, **kwargs)
        # 另一个写者在原索引锁释放后完成自己的删除。
        original_update(target, lambda current: {key: value for key, value in current.items() if key != 'victim'})
        raise OSError('index replacement not acknowledged')

    monkeypatch.setattr(apply, 'update_json_file_atomic', fail_update)
    with pytest.raises(OSError, match='not acknowledged'):
        executor(home=home, action=action, now=NOW)
    assert failures == [path]
    assert 'victim' not in json.loads(path.read_bytes())
    assert action.path.exists()
    assert json.loads((action.destination / 'tombstone.json').read_bytes())['status'] == 'recovery_required'


def test_remounted_legacy_without_integrity_evidence_is_not_silently_resealed(compact_case, monkeypatch):
    _thread, _messages, _original, path = _commit_rotation(compact_case, monkeypatch)
    manifest = path.with_suffix('.manifest.json')
    legacy = json.loads(manifest.read_bytes())
    legacy.pop('hot_frontier')
    legacy.pop('rotation_id')
    manifest.write_text(json.dumps(legacy) + '\n')
    _remount(path)
    before = manifest.read_bytes(), path.read_bytes()
    with pytest.raises(DataCorruptionError, match='lacks a portable integrity frontier'):
        with message_scan.TranscriptReader(path) as reader:
            reader.read()
    with pytest.raises(DataCorruptionError, match='lacks a portable integrity frontier'):
        compact_case.store.messages.append({'thread_id': compact_case.thread_id, 'role': 'user', 'content': '不得凭空补封'})
    assert (manifest.read_bytes(), path.read_bytes()) == before


@pytest.mark.parametrize('index_name', ['channel_bindings.json', 'user_latest_threads.json'])
def test_bound_conversation_never_enters_trash_transaction(tmp_path, monkeypatch, index_name):
    """旧的活跃绑定仍拒绝归档，不能为恢复故障注入测试而移除生产保护。"""
    home, action, executor = _retention_case(tmp_path, 'conversation')
    root = action.path.parent.parent
    update_json_file_atomic(root / index_name, lambda current: {**current, 'active': action.record_id})
    indexes = {root / name: (root / name).read_bytes()
               for name in ('channel_bindings.json', 'user_latest_threads.json')}
    originals = _original_bytes(action)

    def forbidden(*args, **kwargs):
        pytest.fail('活跃线程不应进入回收或搬移')

    monkeypatch.setattr(apply, '_begin_trash', forbidden)
    monkeypatch.setattr(apply, '_move_retention_payload', forbidden)
    assert executor(home=home, action=action, now=NOW).status == 'state_changed'
    assert {path: path.read_bytes() for path in originals} == originals
    assert not action.destination.exists()
    assert {path: path.read_bytes() for path in indexes} == indexes
