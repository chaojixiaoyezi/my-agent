"""用真实检查点、线程 CAS 和冷热读口验证安全前缀轮转及维护状态。"""

from __future__ import annotations

import hashlib
import io
import json
from dataclasses import replace

import pytest

from agent_py_agent.agent.conversation import compact_checkpoint as checkpoints
from agent_py_agent.agent.conversation import message_scan
from agent_py_agent.agent.conversation.models import ConversationCompactCommit
from agent_py_agent.agent.conversation.store_io import ImmutableJsonIndex
from agent_py_agent.tests.test_compact_scoped_checkpoint import compact_case as _compact_case

compact_case = _compact_case


@pytest.fixture
def rotations(monkeypatch):
    """只旁观真实轮转返回值，不替换覆盖、任务状态、文件写入或 CAS。"""
    results = []
    original = message_scan.rotate_committed_transcript

    def observe(*args, **kwargs):
        result = original(*args, **kwargs)
        results.append(result)
        return result

    monkeypatch.setattr(message_scan, 'rotate_committed_transcript', observe)
    return results


def _append_rows(case, count, *, metadata_by_row=None, body_chars=16):
    metadata_by_row = metadata_by_row or {}
    return [case.store.messages.append({
        'thread_id': case.thread_id, 'role': 'user' if number % 2 == 0 else 'assistant',
        'content': '历史' * body_chars, 'metadata': metadata_by_row.get(number, {}),
        'now': case.clock[0],
    }) for number in range(count)]


def _commit(case, rows, *, omitted=()):
    """检查点可精确漏掉中间行，但最后来源行和字节边界仍必须一致。"""
    before = case.store.threads.require(case.thread_id)
    boundary = rows[-2]
    through = case.store.messages.byte_offset_after(case.thread_id, boundary.message_id)
    compact_rows = tuple(row for number, row in enumerate(rows[:-1]) if number not in omitted)
    summary = f'摘要代次 {before.compact_generation + 1}'
    checkpoint_id = checkpoints.write_compact_checkpoint(case.agent, checkpoints.CompactCheckpointRequest(
        thread=before, summary=summary, operation_evidence={}, compact_rows=compact_rows,
        retained_tail=(rows[-1],), source_end_byte_offset=through,
        projected_tokens_before=900, projected_tokens_after=300, policy=case.policy, forced=False,
    ))
    return case.store.threads.update_compact_state(case.thread_id, commit=ConversationCompactCommit(
        summary=summary, operation_evidence={}, checkpoint_id=checkpoint_id,
        compacted_through_message_id=boundary.message_id, compacted_through_byte_offset=through,
        source_messages=before.compact_source_messages + len(compact_rows), source_tool_pairs=0,
    ), expected_generation=before.compact_generation, now=case.clock[0])


def _assert_layout(case, original, cold_rows):
    """逐字核对冷热拼接、累计链摘要、段地址和既有 manifest 字段。"""
    path = case.store.storage.message_path(case.thread_id)
    lines = list(io.BytesIO(original))
    prefix = b''.join(lines[:cold_rows])
    assert path.read_bytes() == b''.join(lines[cold_rows:])
    manifest = message_scan.transcript_manifest(path)
    if cold_rows:
        assert set(manifest) == {
            'schema', 'thread_id', 'hot_base', 'segment_count', 'cold_message_count',
            'index', 'chain_sha256', 'checkpoint_id', 'generation', 'boundary_message_id',
            'summary_sha256', 'hot_frontier', 'rotation_id', 'hot_identity',
        }
        assert manifest['hot_base'] == len(prefix)
        assert manifest['cold_message_count'] == cold_rows
        chain = hashlib.sha256(b'').digest()
        for line in lines[:cold_rows]:
            chain = hashlib.sha256(chain + line).digest()
        assert manifest['chain_sha256'] == chain.hex()
        index = ImmutableJsonIndex(path.parent, ('archive', case.thread_id, 'index'))
        start = 0
        for number in range(1, manifest['segment_count'] + 1):
            descriptor = index.get(manifest['index'], f'segment:{number}')
            segment = path.parent / 'archive' / case.thread_id / f'seg-{number:020d}.jsonl'
            raw = segment.read_bytes()
            assert descriptor == {
                'start': start, 'end': start + len(raw), 'sha256': hashlib.sha256(raw).hexdigest(),
            }
            assert raw == original[start:start + len(raw)] and raw.endswith(b'\n')
            start += len(raw)
        assert start == len(prefix)
    else:
        assert manifest is None
    with message_scan.TranscriptReader(path) as reader:
        assert reader.read() == original


def _assert_blocked(case, thread, reason):
    expected = {
        'status': 'blocked', 'error_type': reason, 'checkpoint_id': thread.compact_checkpoint_id,
    }
    assert thread.metadata['compact_transcript_maintenance'] == expected
    # 直接经过模型反序列化读回，证明三个既有字段能按原线程合同持久化。
    raw = json.loads(case.store.storage.thread_path(case.thread_id).read_bytes())
    assert type(thread).from_dict(raw).metadata['compact_transcript_maintenance'] == expected


@pytest.mark.parametrize('field', ['task_id', 'conversation_task_id', 'background_task_id'])
def test_task_pin_rotates_only_the_safe_prefix(compact_case, rotations, field):
    case = compact_case
    case.store.tasks.bind({'thread_id': case.thread_id, 'task_id': 'long-task', 'status': 'active'})
    rows = _append_rows(case, 136, metadata_by_row={128: {field: 'long-task'}})
    original = case.store.storage.message_path(case.thread_id).read_bytes()
    thread = _commit(case, rows)
    assert rotations[-1] == {
        'rotated_rows': 128, 'blocked': {'reason_code': 'task_pin', 'position': 128, 'task_id': 'long-task'},
    }
    _assert_layout(case, original, 128)
    _assert_blocked(case, thread, 'task_pin')
    for row in rows:
        found, errors = case.store.messages.by_id_report(case.thread_id, row.message_id)
        assert not errors and found == row


def test_uncovered_row_keeps_the_entire_following_suffix(compact_case, rotations):
    case = compact_case
    rows = _append_rows(case, 136)
    original = case.store.storage.message_path(case.thread_id).read_bytes()
    thread = _commit(case, rows, omitted=(128,))
    assert rotations[-1] == {
        'rotated_rows': 128, 'blocked': {'reason_code': 'uncovered', 'position': 128, 'task_id': None},
    }
    _assert_layout(case, original, 128)
    _assert_blocked(case, thread, 'uncovered')


@pytest.mark.parametrize('first', ['task_pin', 'uncovered'])
def test_first_blocker_wins_over_later_covered_or_pinned_rows(compact_case, rotations, first):
    case = compact_case
    case.store.tasks.bind({'thread_id': case.thread_id, 'task_id': 'long-task', 'status': 'active'})
    pin, uncovered = (128, 130) if first == 'task_pin' else (130, 128)
    rows = _append_rows(case, 136, metadata_by_row={pin: {'task_id': 'long-task'}})
    original = case.store.storage.message_path(case.thread_id).read_bytes()
    _commit(case, rows, omitted=(uncovered,))
    assert rotations[-1] == {
        'rotated_rows': 128,
        'blocked': {'reason_code': first, 'position': 128, 'task_id': 'long-task' if first == 'task_pin' else None},
    }
    _assert_layout(case, original, 128)


def test_unblocked_rotation_preserves_the_original_compact_boundary(compact_case, rotations):
    case = compact_case
    rows = _append_rows(case, 136)
    original = case.store.storage.message_path(case.thread_id).read_bytes()
    thread = _commit(case, rows)
    assert rotations[-1] == {'rotated_rows': 134, 'blocked': None}
    _assert_layout(case, original, 134)
    assert 'compact_transcript_maintenance' not in thread.metadata


@pytest.mark.parametrize('reason', ['task_pin', 'uncovered'])
@pytest.mark.parametrize('position,total', [(0, 136), (7, 136), (127, 136), (3, 10)])
def test_prefix_below_both_thresholds_reports_block_without_rotation(compact_case, rotations, reason, position, total):
    case = compact_case
    metadata, omitted = {}, ()
    if reason == 'task_pin':
        case.store.tasks.bind({'thread_id': case.thread_id, 'task_id': 'long-task', 'status': 'active'})
        metadata[position] = {'task_id': 'long-task'}
    else:
        omitted = (position,)
    rows = _append_rows(case, total, metadata_by_row=metadata)
    original = case.store.storage.message_path(case.thread_id).read_bytes()
    thread = _commit(case, rows, omitted=omitted)
    assert rotations[-1] == {
        'rotated_rows': 0,
        'blocked': {'reason_code': reason, 'position': position, 'task_id': 'long-task' if reason == 'task_pin' else None},
    }
    _assert_layout(case, original, 0)
    _assert_blocked(case, thread, reason)


def test_byte_threshold_splits_safe_prefix_into_original_segment_sizes(compact_case, rotations):
    case = compact_case
    case.store.tasks.bind({'thread_id': case.thread_id, 'task_id': 'long-task', 'status': 'active'})
    rows = _append_rows(case, 2, body_chars=100000)
    rows += _append_rows(case, 4, metadata_by_row={0: {'task_id': 'long-task'}})
    original = case.store.storage.message_path(case.thread_id).read_bytes()
    _commit(case, rows)
    assert rotations[-1] == {
        'rotated_rows': 2, 'blocked': {'reason_code': 'task_pin', 'position': 2, 'task_id': 'long-task'},
    }
    _assert_layout(case, original, 2)
    manifest = message_scan.transcript_manifest(case.store.storage.message_path(case.thread_id))
    assert manifest['segment_count'] == 2
    assert manifest['hot_base'] >= message_scan.TRANSCRIPT_ROTATE_BYTES


def test_pin_reports_active_identity_after_an_inactive_metadata_identity(compact_case, rotations):
    case = compact_case
    case.store.tasks.bind({'thread_id': case.thread_id, 'task_id': 'finished', 'status': 'completed'})
    case.store.tasks.bind({'thread_id': case.thread_id, 'task_id': 'long-task', 'status': 'active'})
    rows = _append_rows(case, 136, metadata_by_row={128: {
        'task_id': 'finished', 'background_task_id': 'long-task',
    }})
    _commit(case, rows)
    assert rotations[-1]['blocked'] == {'reason_code': 'task_pin', 'position': 128, 'task_id': 'long-task'}


@pytest.mark.parametrize('initial', ['blocked', 'pending'])
def test_successful_rotation_clears_old_maintenance_without_advancing_generation_twice(
    compact_case, rotations, monkeypatch, initial,
):
    case = compact_case
    metadata = {}
    if initial == 'blocked':
        case.store.tasks.bind({'thread_id': case.thread_id, 'task_id': 'long-task', 'status': 'active'})
        metadata[0] = {'task_id': 'long-task'}
    rows = _append_rows(case, 136, metadata_by_row=metadata)

    def disk_error(*args):
        raise OSError('test disk failure')

    with monkeypatch.context() as fault:
        if initial == 'pending':
            fault.setattr(message_scan, '_save_transcript_segment', disk_error)
        first = _commit(case, rows)
    assert first.metadata['compact_transcript_maintenance']['status'] == initial
    if initial == 'blocked':
        case.store.tasks.update_status({'task_id': 'long-task', 'status': 'completed'})
    rows += _append_rows(case, 8)
    original = case.store.storage.message_path(case.thread_id).read_bytes()
    second = _commit(case, rows)
    assert rotations[-1] == {'rotated_rows': len(rows) - 2, 'blocked': None}
    assert second.compact_generation == first.compact_generation + 1 == 2
    assert 'compact_transcript_maintenance' not in second.metadata
    assert case.store.threads.require(case.thread_id) == second
    _assert_layout(case, original, len(rows) - 2)


def test_noop_paths_return_the_same_structured_result(compact_case):
    case = compact_case
    thread = case.store.threads.require(case.thread_id)
    assert message_scan.rotate_committed_transcript(case.store.storage, thread) == {'rotated_rows': 0, 'blocked': None}
    rows = _append_rows(case, 2)
    assert message_scan.rotate_committed_transcript(case.store.storage, thread) == {'rotated_rows': 0, 'blocked': None}
    invalid = replace(thread, compact_checkpoint_id='uncommitted', compacted_through_message_id='absent')
    assert message_scan.rotate_committed_transcript(case.store.storage, invalid) == {'rotated_rows': 0, 'blocked': None}
    updated = _commit(case, rows)
    assert message_scan.rotate_committed_transcript(case.store.storage, updated) == {'rotated_rows': 0, 'blocked': None}


@pytest.mark.parametrize('status', ['pending', 'blocked'])
def test_absent_maintenance_receipt_preserves_the_previous_status(compact_case, monkeypatch, status):
    case = compact_case
    previous = {'status': status, 'error_type': 'task_pin', 'checkpoint_id': 'previous-head'}
    case.store.threads.update_atomic(case.thread_id, lambda thread: replace(
        thread, metadata={**thread.metadata, 'compact_transcript_maintenance': previous},
    ))
    rows = _append_rows(case, 136)
    path = case.store.storage.message_path(case.thread_id)
    original = path.read_bytes()
    monkeypatch.setattr(message_scan, 'rotate_committed_transcript', lambda *_args, **_kwargs: None)
    updated = _commit(case, rows)
    assert updated.compact_generation == 1
    assert updated.metadata['compact_transcript_maintenance'] == previous
    assert path.read_bytes() == original and message_scan.transcript_manifest(path) is None


def test_deterministic_hundred_day_pin_release_converges_the_hot_file(compact_case, rotations):
    case = compact_case
    daily_rows = 8
    rows = _append_rows(case, message_scan.TRANSCRIPT_ROTATE_ROWS)
    case.store.tasks.bind({'thread_id': case.thread_id, 'task_id': 'long-task', 'status': 'active'})
    original = case.store.storage.message_path(case.thread_id).read_bytes()
    prefix_bytes = len(original)
    for day in range(1, 101):
        case.clock[0] = float(day * 86400)
        if day == 51:
            case.store.tasks.update_status({'task_id': 'long-task', 'status': 'completed', 'now': case.clock[0]})
        metadata = {0: {'task_id': 'long-task'}} if day == 1 else {}
        added = _append_rows(case, daily_rows, metadata_by_row=metadata)
        path = case.store.storage.message_path(case.thread_id)
        hot = path.read_bytes()
        added_bytes = b''.join(list(io.BytesIO(hot))[-daily_rows:])
        original += added_bytes
        rows += added
        thread = _commit(case, rows)
        manifest = message_scan.transcript_manifest(path)
        hot = path.read_bytes()
        if day <= 50:
            assert manifest['hot_base'] == prefix_bytes
            assert manifest['cold_message_count'] == message_scan.TRANSCRIPT_ROTATE_ROWS
            assert hot == original[prefix_bytes:]
            assert hot.count(b'\n') == day * daily_rows
            assert rotations[-1]['blocked'] == {
                'reason_code': 'task_pin', 'position': 128 if day == 1 else 0, 'task_id': 'long-task',
            }
            _assert_blocked(case, thread, 'task_pin')
        else:
            assert rotations[-1]['blocked'] is None
            assert 'compact_transcript_maintenance' not in thread.metadata
            assert hot.count(b'\n') <= message_scan.TRANSCRIPT_ROTATE_ROWS + daily_rows
            assert len(hot) <= message_scan.TRANSCRIPT_ROTATE_BYTES + len(added_bytes)
            if day == 51:
                assert rotations[-1]['rotated_rows'] > 0 and hot.count(b'\n') == 2
        _assert_layout(case, original, manifest['cold_message_count'])
        # 后续提交只读取当前热集；完整历史只在上面的独立证据断言中读取。
        rows = [type(added[0]).from_dict(json.loads(line)) for line in io.BytesIO(hot)]
