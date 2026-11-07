"""仅在临时真实会话中注入坏行或冷层丢失，显式历史与身份校验继续拒绝。"""
from __future__ import annotations

import json
import shutil
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation import message_scan
from agent_py_agent.agent.conversation.message_selection import select_message_snapshot
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.conversation.store_io import ImmutableJsonIndex
from agent_py_agent.agent.runtime_errors import DataCorruptionError
from agent_py_agent.tests.test_compact_scoped_checkpoint import compact_case as compact_case
from agent_py_agent.tests.test_transcript_cold_rotation import _commit_rotation


@pytest.mark.parametrize('segmented', [False, True])
@pytest.mark.parametrize('bad', [b'broken\n', b'[]\n', b'null\n', b'\xff\n'])
def test_non_object_transcript_row_is_skipped(compact_case, monkeypatch, caplog, segmented, bad):
    case = compact_case
    if segmented:
        _, rows, _, path = _commit_rotation(case, monkeypatch)
    else:
        rows = [case.store.messages.append({'thread_id': case.thread_id, 'role': 'user', 'content': 'kept'})]
        path = case.store.storage.message_path(case.thread_id)
    with path.open('ab') as stream:
        stream.write(bad)
    before = path.read_bytes()
    assert tuple(select_message_snapshot(case.store.messages, case.thread_id)) == tuple(rows)
    assert 'JSONL_ROW_SKIPPED' in caplog.text and f'thread={case.thread_id}' in caplog.text
    assert path.read_bytes() == before


@pytest.mark.parametrize('segmented', [False, True])
@pytest.mark.parametrize('fault', ['foreign', 'missing_id', 'duplicate', 'model'])
def test_object_identity_errors_still_fail_closed(compact_case, monkeypatch, segmented, fault):
    case = compact_case
    if segmented:
        _, rows, _, path = _commit_rotation(case, monkeypatch)
    else:
        rows = [case.store.messages.append({'thread_id': case.thread_id, 'role': 'user', 'content': 'kept'})]
        path = case.store.storage.message_path(case.thread_id)
    value = rows[-1].to_dict()
    if fault == 'foreign':
        value['thread_id'] = 'foreign-thread'
    elif fault == 'missing_id':
        value['message_id'] = ''
    elif fault == 'model':
        value['created_at'] = 'not-a-number'
    with path.open('ab') as stream:
        stream.write(json.dumps(value).encode() + b'\n')
    with pytest.raises(DataCorruptionError):
        select_message_snapshot(case.store.messages, case.thread_id, selector_factory=lambda _: lambda row: False)


def test_absent_transcript_ignores_only_legacy_boundary(tmp_path, caplog):
    store = ConversationStore(tmp_path / 'conversations')
    thread = store.threads.get_or_create({'canonical_user_id': 'owner'})
    row = store.messages.append({'thread_id': thread.thread_id, 'role': 'user', 'content': 'lost'})
    path = store.storage.message_path(thread.thread_id)
    path.unlink()
    assert tuple(select_message_snapshot(store.messages, thread.thread_id,
                 exclude_through_message_ids=(row.message_id,))) == ()
    assert 'TRANSCRIPT_ABSENT_LEGACY_BOUNDARY_IGNORED' in caplog.text
    assert not path.exists()


def _rotation(case, monkeypatch):
    # 使用原真实writer/CAS，一次提交切出多个小段，验证中间段丢失不抹掉两侧幸存行。
    monkeypatch.setattr(message_scan, '_TRANSCRIPT_SEGMENT_BYTES', 1800)
    thread, rows, original, path = _commit_rotation(case, monkeypatch)
    archive = path.parent / 'archive' / case.thread_id
    assert len(list(archive.glob('seg-*.jsonl'))) >= 2
    return thread, rows, original, path, archive


@pytest.mark.parametrize('fault', ['archive', 'thread', 'index', 'index_node'])
def test_missing_cold_layer_degrades_only_turn_path(compact_case, monkeypatch, caplog, fault):
    case = compact_case
    _, rows, _, path, archive = _rotation(case, monkeypatch)
    if fault == 'archive':
        shutil.rmtree(archive.parent)
    elif fault == 'thread':
        shutil.rmtree(archive)
    elif fault == 'index':
        shutil.rmtree(archive / 'index')
    else:
        manifest = message_scan.transcript_manifest(path)
        (archive / 'index' / manifest['index']['ref']['ref']).unlink()
    assert tuple(select_message_snapshot(case.store.messages, case.thread_id)) == tuple(rows[-2:])
    with pytest.raises((DataCorruptionError, OSError)):
        _strict_read(path)
    assert case.store.messages.history_page_report(case.thread_id, limit=100).errors
    first = case.store.messages.append_once({'thread_id': case.thread_id, 'role': 'user', 'content': 'new'},
                                           dedupe_key='new-key')
    replay = case.store.messages.append_once({'thread_id': case.thread_id, 'role': 'user', 'content': 'new'},
                                            dedupe_key='new-key')
    assert first == replay
    old_retry = case.store.messages.append_once({'thread_id': case.thread_id, 'role': 'user', 'content': 'retry'},
                                               dedupe_key='key-0')
    assert old_retry.message_id != rows[0].message_id
    assert case.store.messages.append_once({'thread_id': case.thread_id, 'role': 'user', 'content': 'retry'},
                                          dedupe_key='key-0') == old_retry
    plain = case.store.messages.append({'thread_id': case.thread_id, 'role': 'user', 'content': 'plain'})
    assert tuple(select_message_snapshot(case.store.messages, case.thread_id)) == (*rows[-2:], first, old_retry, plain)
    assert 'TRANSCRIPT_COLD_LAYER_MISSING' in caplog.text and 'op=dedupe' in caplog.text and 'op=select' in caplog.text


def _strict_read(path):
    with message_scan.TranscriptReader(path) as reader:
        return reader.read()


def test_deleted_middle_segment_preserves_survivors_and_legacy_ends(compact_case, monkeypatch, caplog):
    case = compact_case
    _, rows, _, path, archive = _rotation(case, monkeypatch)
    segments = sorted(archive.glob('seg-*.jsonl'))
    lost_path = segments[len(segments) // 2]
    lost = {json.loads(raw)['message_id'] for raw in lost_path.read_bytes().splitlines()}
    lost_row = next(row for row in rows if row.message_id in lost)
    lost_path.unlink()
    expected = tuple(row for row in rows if row.message_id not in lost)
    selected = select_message_snapshot(case.store.messages, case.thread_id,
                                       exclude_through_message_ids=(lost_row.message_id,))
    assert tuple(selected) == expected == tuple(selected)
    assert 'TRANSCRIPT_COLD_LAYER_MISSING' in caplog.text
    assert case.store.messages.history_page_report(case.thread_id, limit=100).errors
    retry = case.store.messages.append_once({'thread_id': case.thread_id, 'role': 'user', 'content': 'retry'},
                                           dedupe_key=lost_row.metadata['dedupe_key'])
    assert retry.message_id != lost_row.message_id


def test_corrupt_segment_is_never_treated_as_missing(compact_case, monkeypatch):
    case = compact_case
    _, rows, _, path, archive = _rotation(case, monkeypatch)
    segment = sorted(archive.glob('seg-*.jsonl'))[0]
    original = segment.read_bytes()
    assert b'row-0:' in original
    segment.write_bytes(original.replace(b'row-0:', b'bad-0:'))
    assert segment.stat().st_size == len(original)
    with pytest.raises(DataCorruptionError):
        select_message_snapshot(case.store.messages, case.thread_id)
    with pytest.raises(DataCorruptionError):
        case.store.messages.append_once({'thread_id': case.thread_id, 'role': 'user', 'content': 'retry'},
                                       dedupe_key=rows[0].metadata['dedupe_key'])


def test_missing_index_node_is_distinct_from_corruption(tmp_path):
    index = ImmutableJsonIndex(tmp_path, ('index',))
    root = index.put(None, 'key', {'value': 1})
    node = tmp_path / 'index' / root['ref']['ref']
    original = node.read_bytes()
    node.unlink()
    with pytest.raises(FileNotFoundError, match='immutable index node is missing') as failure:
        index.get(root, 'key')
    assert type(failure.value).__name__ == 'ImmutableIndexNodeMissing'
    node.write_bytes(original.replace(b'value', b'other'))
    with pytest.raises(OSError) as corrupt:
        index.get(root, 'key')
    assert not isinstance(corrupt.value, FileNotFoundError)


def test_provider_switch_with_lost_archive_keeps_hot_rows(compact_case, monkeypatch, caplog):
    from agent_py_agent.agent.conversation.compact import load_conversation_compact_source
    from agent_py_agent.agent.conversation.compact_remote import provider_compaction_record
    from agent_py_agent.agent.conversation.compact_scope import THREAD_COMPACT_SCOPE

    case = compact_case
    case.agent.config = SimpleNamespace()
    case.agent.backend.provider_compaction_scope = lambda: {
        'protocol': 'openai_responses', 'endpoint': 'https://provider.invalid/v1',
    }
    provider = provider_compaction_record(case.agent.backend, {'type': 'compaction', 'encrypted_content': 'test-only'})
    thread, rows, _, path = _commit_rotation(case, monkeypatch, provider=provider)
    shutil.rmtree(path.parent / 'archive')
    case.agent.backend = SimpleNamespace(name='anthropic_compatible', model_name='other')
    source = load_conversation_compact_source(case.agent, case.store, thread, scope=THREAD_COMPACT_SCOPE)
    assert tuple(source.messages) == tuple(rows[-2:])
    source.messages.validate()
    assert 'TRANSCRIPT_COLD_LAYER_MISSING' in caplog.text


def test_lost_segment_does_not_disable_survivor_rewrite_guard(compact_case, monkeypatch):
    case = compact_case
    _, _, _, path, archive = _rotation(case, monkeypatch)
    segments = sorted(archive.glob('seg-*.jsonl'))
    segments[len(segments) // 2].unlink()

    def change_unselected_source(_positions):
        raw = segments[0].read_bytes()
        assert b'row-0:' in raw
        segments[0].write_bytes(raw.replace(b'row-0:', b'bad-0:'))
        return lambda _row: False

    with pytest.raises(DataCorruptionError):
        select_message_snapshot(case.store.messages, case.thread_id, selector_factory=change_unselected_source)


def test_missing_hot_is_not_degraded_to_empty(compact_case, monkeypatch):
    case = compact_case
    _, _, _, path, archive = _rotation(case, monkeypatch)
    shutil.rmtree(archive)
    path.unlink()
    with pytest.raises(FileNotFoundError):
        select_message_snapshot(case.store.messages, case.thread_id)
    assert case.store.messages.history_page_report(case.thread_id, limit=100).errors


def test_missing_segment_signal_keeps_original_end(compact_case, monkeypatch):
    _, _, _, path, archive = _rotation(compact_case, monkeypatch)
    manifest = message_scan.transcript_manifest(path)
    index = ImmutableJsonIndex(path.parent, ('archive', compact_case.thread_id, 'index'))
    descriptor = index.get(manifest['index'], 'segment:1')
    sorted(archive.glob('seg-*.jsonl'))[0].unlink()
    with message_scan.TranscriptReader(path) as reader:
        with pytest.raises(DataCorruptionError) as failure:
            reader.readline()
    assert type(failure.value).__name__ == 'TranscriptColdSegmentMissing'
    assert failure.value.end == descriptor['end']


def test_lost_compact_coverage_index_uses_missing_signal(compact_case):
    from agent_py_agent.agent.conversation import compact
    from agent_py_agent.agent.conversation import compact_checkpoint as checkpoints
    from agent_py_agent.agent.conversation.compact_scope import THREAD_COMPACT_SCOPE
    from agent_py_agent.agent.conversation.models import ConversationCompactCommit
    from agent_py_agent.agent.conversation.store_io import ImmutableIndexNodeMissing

    case = compact_case
    case.agent.config = SimpleNamespace()
    rows = [case.store.messages.append({'thread_id': case.thread_id, 'role': 'user',
                                       'content': f'evidence-row-{number}'}) for number in range(270)]
    boundary = case.store.messages.byte_offset_after(case.thread_id, rows[-2].message_id)
    before = case.store.threads.require(case.thread_id)
    checkpoint_id = checkpoints.write_compact_checkpoint(case.agent, checkpoints.CompactCheckpointRequest(
        thread=before, summary='covered', operation_evidence={'events': list(range(300))},
        compact_rows=tuple(rows[:-1]), retained_tail=(rows[-1],), source_end_byte_offset=boundary,
        projected_tokens_before=900, projected_tokens_after=300, policy=case.policy, forced=False,
        scope=THREAD_COMPACT_SCOPE,
    ))
    thread = case.store.threads.update_compact_state(case.thread_id, commit=ConversationCompactCommit(
        summary='covered', operation_evidence={'events': list(range(300))}, checkpoint_id=checkpoint_id,
        compacted_through_message_id=rows[-2].message_id, compacted_through_byte_offset=boundary,
        source_messages=len(rows) - 1, source_tool_pairs=0, publish_thread_view=True,
    ), expected_generation=0)
    source = compact.load_conversation_compact_source(case.agent, case.store, thread, scope=THREAD_COMPACT_SCOPE)
    assert tuple(source.messages) == (rows[-1],)
    shutil.rmtree(case.root / 'compact' / 'evidence' / case.thread_id / 'indexes')
    if getattr(compact, '_reset_unreachable_compact_head', None) is None:
        with pytest.raises(ImmutableIndexNodeMissing):
            compact.load_conversation_compact_source(case.agent, case.store, thread, scope=THREAD_COMPACT_SCOPE)
        assert case.store.threads.require(case.thread_id).compact_checkpoint_id == checkpoint_id
    else:
        source = compact.load_conversation_compact_source(case.agent, case.store, thread, scope=THREAD_COMPACT_SCOPE)
        assert tuple(source.messages) == tuple(rows)
        assert not case.store.threads.require(case.thread_id).compact_checkpoint_id


# LLM: 真实writer/CAS与转录字节不伪造；接口探针同时适用于独立W9和已集成W2的分支。
# 函数用途: 保证冷转必须确认冷副本，且接口内部异常不得触发第二次热回退读取。
@pytest.mark.parametrize('damage', ['missing', 'present', 'reader_type_error'])
def test_rotation_requires_cold_copy_with_fallback_aware_reader(compact_case, monkeypatch, damage):
    import hashlib

    from agent_py_agent.agent.conversation import compact_checkpoint as checkpoints
    from agent_py_agent.agent.conversation.compact_checkpoint_scan import checkpoint_row_lookup

    case = compact_case
    original_reader = checkpoints._cold_checkpoint
    calls, maintaining = [], False

    # LLM: 冷路径存在才委托真实读取器，避免已集成W2时叠加其默认热回退；热查找使用真实账本。
    # 函数用途: 在独立分支重现W2的新接口，并注入仅发生在维护读取内部的TypeError。
    def fallback_reader(root, thread_id, checkpoint_id, *, fallback_hot=True):
        calls.append(fallback_hot)
        if maintaining and damage == 'reader_type_error':
            raise TypeError('invalid archived checkpoint object')
        name = hashlib.sha256(checkpoint_id.encode()).hexdigest() + '.checkpoint.json'
        copy = root / 'archive' / thread_id / name
        row = original_reader(root, thread_id, checkpoint_id) if copy.exists() else None
        if row is None and fallback_hot:
            with checkpoint_row_lookup(root / 'conversations' / f'{thread_id}.jsonl') as lookup:
                row = lookup(checkpoint_id)
        return row

    # LLM: 仅在真实writer返回后操作临时冷副本；热账本与CAS保持原路径，准备期调用不计入维护次数。
    # 函数用途: 为真实冷转准备缺副本、好副本和读取内部错误三个对照场景。
    def prepare_copy(checkpoint_id):
        nonlocal maintaining
        calls.clear()
        maintaining = True
        if damage == 'missing':
            name = hashlib.sha256(checkpoint_id.encode()).hexdigest() + '.checkpoint.json'
            copy = case.root / 'compact' / 'archive' / case.thread_id / name
            assert copy.exists()
            copy.rename(copy.with_suffix('.missing-test'))

    monkeypatch.setattr(checkpoints, '_cold_checkpoint', fallback_reader)
    updated, _, original, path = _commit_rotation(case, monkeypatch, before_commit=prepare_copy)
    assert updated.compact_generation == 1 and updated.summary == '已覆盖旧行'
    assert case.store.threads.require(case.thread_id) == updated
    if damage == 'present':
        assert 'compact_transcript_maintenance' not in updated.metadata
        assert path.with_suffix('.manifest.json').exists() and path.read_bytes() != original
        with message_scan.TranscriptReader(path) as reader:
            assert reader.read() == original
    else:
        assert updated.metadata.get('compact_transcript_maintenance') == {
            'status': 'pending', 'error_type': 'DataCorruptionError', 'checkpoint_id': updated.compact_checkpoint_id,
        }
        assert path.read_bytes() == original and not path.with_suffix('.manifest.json').exists()
    assert calls == [False]
