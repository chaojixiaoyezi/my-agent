"""删除派生压缩状态时恢复原文；身份、摘要及有效游标损坏仍失败关闭。"""
from __future__ import annotations

import logging
import os
import shutil
from dataclasses import fields, replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation import compact, compact_checkpoint, compact_summary_view
from agent_py_agent.agent.conversation.compact_scope import THREAD_COMPACT_SCOPE
from agent_py_agent.agent.conversation.models import ConversationCompactCommit, ConversationThread
from agent_py_agent.tests.test_compact_scoped_checkpoint import compact_case as _compact_case
from agent_py_agent.tests.test_compact_source_lifetime import _prepare
from agent_py_agent.tests.test_transcript_cold_rotation import _commit_rotation

compact_case = _compact_case


def _seed(case, count=6):
    return [case.store.messages.append({
        'thread_id': case.thread_id, 'role': 'user' if index % 2 == 0 else 'assistant',
        'content': f'原始消息 {index}', 'now': index + 1,
    }) for index in range(count)]


def _load(case, thread):
    return compact.load_conversation_compact_source(
        case.agent, case.store, thread, scope=THREAD_COMPACT_SCOPE,
    )


def _commit(case, rows, covered=4):
    thread = case.store.threads.require(case.thread_id)
    boundary = case.store.messages.byte_offset_after(case.thread_id, rows[covered - 1].message_id)
    checkpoint_id = compact_checkpoint.write_compact_checkpoint(case.agent, compact_checkpoint.CompactCheckpointRequest(
        thread=thread, summary='已覆盖的历史', operation_evidence={'count': covered},
        compact_rows=tuple(rows[:covered]), retained_tail=tuple(rows[covered:]),
        source_end_byte_offset=boundary, projected_tokens_before=900, projected_tokens_after=300,
        policy=case.policy, forced=False, scope=THREAD_COMPACT_SCOPE,
    ))
    return case.store.threads.update_compact_state(case.thread_id, commit=ConversationCompactCommit(
        summary='已覆盖的历史', operation_evidence={'count': covered}, checkpoint_id=checkpoint_id,
        compacted_through_message_id=rows[covered - 1].message_id, compacted_through_byte_offset=boundary,
        source_messages=covered, source_tool_pairs=0,
    ), expected_generation=thread.compact_generation, now=case.clock[0])


def _warnings(caplog, code):
    return [record.getMessage() for record in caplog.records if record.getMessage().startswith(code + ' ')]


def _reset_fields():
    # 独立抄自 C-RES-T2-COMPACT，不从实现反推期望字段，避免多清或漏清也变绿。
    return (
        'compacted_through_message_id', 'compacted_through_byte_offset', 'compact_generation',
        'compact_updated_at', 'compact_source_messages', 'compact_source_tool_pairs',
        'compact_checkpoint_id', 'compact_consecutive_failures', 'compact_failure_updated_at',
        'compact_failure_code', 'compact_vision_failed_generation', 'compact_operation_evidence',
    )


def _assert_reset(case, before, source):
    current = case.store.threads.require(case.thread_id)
    defaults = ConversationThread(thread_id='', canonical_user_id='')
    assert source.thread == current
    for field in fields(ConversationThread):
        expected = getattr(defaults if field.name in _reset_fields() else before, field.name)
        assert getattr(current, field.name) == expected, field.name


def test_missing_head_resets_only_declared_fields_and_preserves_transcript(compact_case, caplog):
    case = compact_case
    rows = _seed(case)
    before = case.store.threads.update_atomic(case.thread_id, lambda row: replace(
        row, compact_checkpoint_id='compact-v3-' + 'a' * 32, compact_generation=1,
        compacted_through_message_id=rows[3].message_id, compacted_through_byte_offset=123,
        compact_updated_at=3.0, compact_source_messages=4, compact_source_tool_pairs=2,
        compact_consecutive_failures=2, compact_failure_updated_at=4.0, compact_failure_code='test',
        compact_vision_failed_generation=1, compact_operation_evidence={'count': 2},
        title='保留标题', summary='保留原字段', metadata={
            'plugin_host_v2': {'revision': 17, 'user_scope': {'label': '保留插件字段'}},
            'user_note': '保留用户元数据', 'nested': {'label': '保留其它键'},
        },
        model_context_usage={'input_tokens': 99}, model_metrics={'output_tokens': 5},
    ))
    path = case.store.storage.message_path(case.thread_id)
    raw = path.read_bytes()
    with caplog.at_level(logging.WARNING):
        source = _load(case, before)
    assert list(source.messages) == rows
    # RES-W3-CONT 只允许原子更新器的原有修订推进及回执规范化；其它 metadata 仍精确相等。
    expected_metadata = {**before.metadata, 'plugin_host_v2': {
        **before.metadata['plugin_host_v2'], 'revision': 18, 'receipts': {},
    }}
    _assert_reset(case, replace(before, metadata=expected_metadata), source)
    assert path.read_bytes() == raw
    assert not (case.root / 'compact').exists()
    warning, = _warnings(caplog, 'COMPACT_HEAD_RESET')
    assert f'thread={case.thread_id}' in warning and f'checkpoint={before.compact_checkpoint_id}' in warning
    assert 'generation=1' in warning and 'reason=conversation compact checkpoint chain is incomplete' in warning
    assert '原始消息' not in warning


def test_deleted_compact_directory_restarts_a_resolvable_generation_one_chain(compact_case, caplog):
    case = compact_case
    rows = _seed(case)
    before = _commit(case, rows)
    path = case.store.storage.message_path(case.thread_id)
    raw = path.read_bytes()
    shutil.rmtree(case.root / 'compact')
    with caplog.at_level(logging.WARNING):
        source = _load(case, before)
    assert list(source.messages) == rows
    _assert_reset(case, before, source)
    assert path.read_bytes() == raw and not (case.root / 'compact').exists()
    assert len(_warnings(caplog, 'COMPACT_HEAD_RESET')) == 1
    following = _commit(case, rows)
    chain = compact_checkpoint.committed_compact_checkpoint_chain(case.agent, following)
    assert following.compact_generation == 1 and len(chain) == 1
    assert chain[0]['checkpoint_id'] == following.compact_checkpoint_id
    view = compact_summary_view.resolve_compact_summary_view(case.agent, following, THREAD_COMPACT_SCOPE)
    assert view.summary == following.summary
    assert list(_load(case, following).messages) == rows[4:]


@pytest.mark.parametrize('moved', ['checkpoint', 'generation'])
def test_concurrent_head_wins_cas_and_retry_uses_its_thread(compact_case, monkeypatch, caplog, moved):
    case = compact_case
    rows = _seed(case)
    winner = _commit(case, rows)
    observed = replace(winner, **({'compact_checkpoint_id': 'compact-v3-' + 'b' * 32}
                                 if moved == 'checkpoint' else {'compact_generation': 0}))
    case.store.threads.write(observed)
    original = case.store.threads.update_atomic
    resolve = compact_summary_view.resolve_compact_summary_view
    calls = []

    def initial_missing(agent, thread, scope):
        # 单独隔离 CAS 的两个比较键；旧观察的缺失只在第一次解析注入。
        if thread == observed:
            raise OSError('conversation compact checkpoint chain is incomplete')
        return resolve(agent, thread, scope)

    def competing(thread_id, updater):
        calls.append(thread_id)
        original(thread_id, lambda _row: winner)
        return original(thread_id, updater)

    monkeypatch.setattr(case.store.threads, 'update_atomic', competing)
    monkeypatch.setattr(compact_summary_view, 'resolve_compact_summary_view', initial_missing)
    with caplog.at_level(logging.WARNING):
        source = _load(case, observed)
    assert calls == [case.thread_id]
    assert source.thread == case.store.threads.require(case.thread_id) == winner
    assert list(source.messages) == rows[4:]
    assert not _warnings(caplog, 'COMPACT_HEAD_RESET')


@pytest.mark.parametrize('reason', [
    'conversation compact checkpoint chain is incomplete',
    'conversation compact checkpoint chain ended early',
    'conversation compact pointer is incomplete',
])
def test_only_one_resolution_retry_after_a_recognized_error(compact_case, monkeypatch, caplog, reason):
    case = compact_case
    _seed(case)
    before = case.store.threads.update_atomic(case.thread_id, lambda row: replace(row, compact_generation=1))
    seen = []

    def fail(_agent, thread, _scope):
        seen.append(thread)
        raise OSError(reason)

    monkeypatch.setattr(compact_summary_view, 'resolve_compact_summary_view', fail)
    with caplog.at_level(logging.WARNING), pytest.raises(OSError, match=reason):
        _load(case, before)
    assert [thread.compact_generation for thread in seen] == [1, 0]
    assert len(_warnings(caplog, 'COMPACT_HEAD_RESET')) == 1


@pytest.mark.parametrize('reason', [
    'conversation compact checkpoint thread identity mismatches',
    'conversation compact checkpoint content identity mismatches',
    'conversation compact checkpoint version mismatches',
])
def test_integrity_resolution_errors_do_not_reset(compact_case, monkeypatch, caplog, reason):
    case = compact_case
    rows = _seed(case)
    before = _commit(case, rows)
    path = case.store.storage.thread_path(case.thread_id)
    raw = path.read_bytes()

    def fail(*_args):
        raise OSError(reason)

    monkeypatch.setattr(compact_summary_view, 'resolve_compact_summary_view', fail)
    with caplog.at_level(logging.WARNING), pytest.raises(OSError, match=reason):
        _load(case, before)
    assert path.read_bytes() == raw and not _warnings(caplog, 'COMPACT_HEAD_RESET')


@pytest.mark.parametrize('damage', ['truncate', 'restore'])
@pytest.mark.parametrize('boundary', ['present', 'missing'])
def test_beyond_end_cursor_reads_remaining_rows_without_writing(compact_case, caplog, damage, boundary):
    case = compact_case
    rows = _seed(case)
    before = _commit(case, rows)
    if boundary == 'present':
        before = case.store.threads.update_atomic(case.thread_id, lambda row: replace(
            row, compacted_through_message_id=rows[0].message_id,
        ))
    path = case.store.storage.message_path(case.thread_id)
    old = path.read_bytes().splitlines(keepends=True)
    if damage == 'truncate':
        path.write_bytes(b'')
    else:
        replacement = path.with_suffix('.restore')
        replacement.write_bytes(b''.join(old[:2]))
        os.replace(replacement, path)
    raw = path.read_bytes()
    thread_raw = case.store.storage.thread_path(case.thread_id).read_bytes()
    with caplog.at_level(logging.WARNING):
        actual = compact._uncompacted_conversation_rows(case.store, before)
    expected = [] if damage == 'truncate' else rows[1:2] if boundary == 'present' else rows[:2]
    assert actual == expected
    assert path.read_bytes() == raw
    assert case.store.storage.thread_path(case.thread_id).read_bytes() == thread_raw
    warning, = _warnings(caplog, 'COMPACT_CURSOR_BEYOND_TRANSCRIPT')
    assert f'thread={case.thread_id}' in warning
    assert f'cursor={before.compacted_through_byte_offset}' in warning and f'end={len(raw)}' in warning


def test_missing_rotation_manifest_uses_hot_rows_after_boundary(compact_case, monkeypatch, caplog):
    case = compact_case
    before, _, _, path = _commit_rotation(case, monkeypatch)
    manifest = path.with_suffix('.manifest.json')
    assert manifest.exists()
    manifest.unlink()
    rows, errors = case.store.messages.recent_report(case.thread_id, limit=0)
    assert not errors and before.compacted_through_message_id in {row.message_id for row in rows}
    assert before.compacted_through_byte_offset > path.stat().st_size
    expected = compact._messages_after_cursor(rows, before.compacted_through_message_id)
    raw = case.store.storage.thread_path(case.thread_id).read_bytes()
    with caplog.at_level(logging.WARNING):
        assert compact._uncompacted_conversation_rows(case.store, before) == expected
    assert expected and len(_warnings(caplog, 'COMPACT_CURSOR_BEYOND_TRANSCRIPT')) == 1
    assert not manifest.exists() and case.store.storage.thread_path(case.thread_id).read_bytes() == raw


def test_regular_compaction_replaces_stale_cursor_after_truncation(compact_case, monkeypatch):
    from agent_py_agent.agent.backends.base import ModelResponse
    from agent_py_agent.agent.conversation import compact_request_budget

    case = compact_case
    before = _commit(case, _seed(case))
    path = case.store.storage.message_path(case.thread_id)
    path.write_bytes(b'')
    assert compact._uncompacted_conversation_rows(case.store, before) == []
    case.agent.conversation_store = case.store
    case.agent.config = SimpleNamespace(model_context_window_tokens=20_000)
    case.agent.backend.max_tokens = 128
    case.agent.prompts = SimpleNamespace(build=lambda prompt, *_args, **_kwargs: prompt)
    for index in range(4):
        case.store.messages.append({'thread_id': case.thread_id,
            'role': 'user' if index % 2 == 0 else 'assistant', 'content': f'新消息{index}' + 'x' * 32768})
    monkeypatch.setattr(compact_request_budget, 'generate_auxiliary_model_response',
                        lambda _request: ModelResponse(text='保留新历史及待办。', backend='fake'))
    result = _prepare(case.agent, before, _load(case, before))
    end, errors = case.store.messages.complete_offset_report(case.thread_id)
    assert result.compacted and not errors
    assert 0 < result.thread.compacted_through_byte_offset <= end
    assert result.thread.compacted_through_byte_offset != before.compacted_through_byte_offset
    assert case.store.messages.after_compact_report(result.thread)[1] == []


def test_garbage_after_an_in_range_cursor_still_fails_closed(compact_case, caplog):
    case = compact_case
    before = _commit(case, _seed(case))
    path = case.store.storage.message_path(case.thread_id)
    with path.open('ab') as handle:
        handle.write(b'not-json\n')
    raw = case.store.storage.thread_path(case.thread_id).read_bytes()
    with caplog.at_level(logging.WARNING), pytest.raises(OSError, match='transcript could not be read reliably'):
        compact._uncompacted_conversation_rows(case.store, before)
    assert not _warnings(caplog, 'COMPACT_CURSOR_BEYOND_TRANSCRIPT')
    assert case.store.storage.thread_path(case.thread_id).read_bytes() == raw


@pytest.mark.parametrize('failure', ['end', 'fallback'])
def test_cursor_fallback_read_errors_still_fail_closed(compact_case, monkeypatch, failure):
    case = compact_case
    before = _commit(case, _seed(case))
    case.store.storage.message_path(case.thread_id).write_bytes(b'')
    if failure == 'end':
        monkeypatch.setattr(case.store.messages, 'complete_offset_report', lambda _thread: (0, [{'error': 'unreadable'}]))
    else:
        monkeypatch.setattr(case.store.messages, 'recent_report', lambda *_args, **_kwargs: ([], [{'error': 'unreadable'}]))
    with pytest.raises(OSError, match='transcript could not be read reliably'):
        compact._uncompacted_conversation_rows(case.store, before)


def test_missing_cold_payload_resets_once_but_explicit_view_stays_strict(compact_case, monkeypatch, caplog):
    case = compact_case
    rows = _seed(case)
    before = _commit(case, rows)
    files = {path: path.read_bytes() for path in (case.root / 'compact').rglob('*') if path.is_file()}
    calls = []

    def missing(*_args):
        calls.append(True)
        raise FileNotFoundError('conversation compact cold payload is missing')

    monkeypatch.setattr(compact_checkpoint, '_read_cold_payload', missing)
    with pytest.raises(FileNotFoundError):
        compact_summary_view.resolve_compact_summary_view(case.agent, before, THREAD_COMPACT_SCOPE)
    with caplog.at_level(logging.WARNING):
        source = _load(case, before)
    assert calls and list(source.messages) == rows
    _assert_reset(case, before, source)
    assert len(_warnings(caplog, 'COMPACT_HEAD_RESET')) == 1
    assert {path: path.read_bytes() for path in files} == files


@pytest.mark.parametrize('missing', [True, False])
def test_coverage_lookup_missing_resets_but_digest_mismatch_propagates(compact_case, monkeypatch, caplog, missing):
    class MissingIndex(FileNotFoundError):
        pass

    case = compact_case
    rows = _seed(case)
    before = _commit(case, rows)
    raw = case.store.storage.thread_path(case.thread_id).read_bytes()
    calls = []

    def lookup(_self, identity):
        calls.append(identity)
        if missing:
            raise MissingIndex('immutable index node is missing')
        raise OSError('immutable index node digest or length mismatches')

    monkeypatch.setattr(compact_summary_view._IndexedMessageCoverage, '__contains__', lookup)
    with caplog.at_level(logging.WARNING):
        if missing:
            source = _load(case, before)
            assert list(source.messages) == rows
            _assert_reset(case, before, source)
            assert len(_warnings(caplog, 'COMPACT_HEAD_RESET')) == 1
        else:
            with pytest.raises(OSError, match='digest or length mismatches'):
                _load(case, before)
            assert case.store.storage.thread_path(case.thread_id).read_bytes() == raw
            assert not _warnings(caplog, 'COMPACT_HEAD_RESET')
    assert calls
