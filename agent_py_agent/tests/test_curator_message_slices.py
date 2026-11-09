"""真实 canonical 读口证明首定位分片、EOF边界、原Curator续办和线程公平。"""
from __future__ import annotations

import json
import os
import time
from dataclasses import replace
from pathlib import Path

import pytest

from agent_py_agent.agent.conversation import message_cursor
from agent_py_agent.agent.conversation.message_cursor_slice import read_after_slice
from agent_py_agent.agent.io.cursor_cache import CursorCache
from agent_py_agent.agent.memory_store.curator_inputs import CuratorMessageSource, _collect_messages
from agent_py_agent.agent.memory_store.retention_scan_budget import RetentionScanBudget
from agent_py_agent.tests.support.curator_scan_cases import encode_row, make_messages, settle_files


def budget(rows=128, *, size=8 * 1024 * 1024):
    return RetentionScanBudget(rows, size, time.monotonic() + 30)


@pytest.fixture(autouse=True)
def fresh_hints(monkeypatch):
    from agent_py_agent.agent.memory_store import curator_inputs
    monkeypatch.setattr("agent_py_agent.agent.conversation.message_cursor_slice.cache_entry_trustworthy", lambda stamp: True)
    monkeypatch.setattr(message_cursor, '_MESSAGE_CURSORS', CursorCache(32768))
    monkeypatch.setattr(curator_inputs, '_COLD_THREAD_EOF', CursorCache(16384))
    monkeypatch.setattr(curator_inputs, '_THREAD_DISCOVERY', CursorCache(16384))


@pytest.mark.parametrize('count', [10000, 100000])
def test_cold_location_continues_without_reparsing_prefix(tmp_path, monkeypatch, count):
    store, tid, rows = make_messages(tmp_path, count=count, content_chars=10)
    path = store.storage.message_path(tid)
    settle_files([path])
    original = json.loads
    parsed = []
    def loads(raw, *args, **kwargs):
        value = original(raw, *args, **kwargs)
        if isinstance(value, dict) and value.get('message_id'):
            parsed.append(value['message_id'])
        return value
    monkeypatch.setattr(json, 'loads', loads)
    partials = 0
    for _ in range(count // 512 + 4):
        slice_budget = budget(512)
        result = store.messages.after_slice_report(tid, after_message_id=rows[-2].message_id, limit=1, budget=slice_budget)
        assert slice_budget.entries <= 512
        assert not result.load_errors
        if result.partial:
            partials += 1
            assert not result.rows
            assert result.continuation is not None
        else:
            assert result.rows == (rows[-1],)
            break
    else:
        pytest.fail('bounded location made no finite progress')
    assert partials and len(parsed) <= count + 2
    assert store.messages.after_report(tid, after_message_id=rows[-2].message_id, limit=1) == ([rows[-1]], [])


@pytest.mark.parametrize('mutation', ['replace', 'truncate', 'bad_prefix', 'fresh_same_stat'])
def test_unusable_source_never_inherits_old_scan_coverage(tmp_path, monkeypatch, mutation):
    store, tid, rows = make_messages(tmp_path, count=40)
    path = store.storage.message_path(tid)
    settle_files([path])
    first = store.messages.after_slice_report(tid, after_message_id=rows[-2].message_id, limit=1, budget=budget(10))
    assert first.partial and first.continuation.offset > 0
    raw = path.read_bytes()
    if mutation == 'replace':
        other = path.with_suffix('.new')
        other.write_bytes(raw)
        os.replace(other, path)
    elif mutation == 'truncate':
        path.write_bytes(encode_row(rows[0].to_dict()))
    elif mutation == 'bad_prefix':
        path.write_bytes(b'{broken}\n' + raw)
    else:
        # 粗时钟窗口即使所有stat字段相同也不得重用先前偏移。
        monkeypatch.setattr('agent_py_agent.agent.conversation.message_cursor_slice.cache_entry_trustworthy', lambda stamp: False)
    if mutation != 'fresh_same_stat':
        settle_files([path])
    result = store.messages.after_slice_report(tid, after_message_id=rows[-2].message_id, limit=1, budget=budget(10))
    if mutation in {'bad_prefix', 'truncate'}:
        assert result.load_errors and not result.rows
    else:
        assert result.partial and result.continuation.offset == first.continuation.offset


def test_duplicate_id_keeps_first_match_and_prefix_blank_error(tmp_path):
    store, tid, rows = make_messages(tmp_path, count=20)
    path = store.storage.message_path(tid)
    rows[8] = replace(rows[8], message_id=rows[2].message_id)
    path.write_bytes(b''.join(encode_row(row.to_dict()) for row in rows))
    settle_files([path])
    expected, errors = store.messages.after_report(tid, after_message_id=rows[2].message_id, limit=10)
    result = store.messages.after_slice_report(tid, after_message_id=rows[2].message_id, limit=10, budget=budget())
    assert not errors and result.rows == tuple(expected)
    path.write_bytes(b'\n' + path.read_bytes())
    settle_files([path])
    result = store.messages.after_slice_report(tid, after_message_id=rows[2].message_id, limit=10, budget=budget())
    assert result.load_errors and not result.rows


def test_display_only_tail_is_partial_until_real_eof(tmp_path):
    from agent_py_agent.agent.conversation.display_checkpoint import DISPLAY_CHECKPOINT_ROLE
    store, tid, rows = make_messages(tmp_path, count=20)
    path = store.storage.message_path(tid)
    rows = [rows[0], *(replace(row, role=DISPLAY_CHECKPOINT_ROLE) for row in rows[1:])]
    path.write_bytes(b''.join(encode_row(row.to_dict()) for row in rows))
    settle_files([path])
    for index in range(6):
        result = store.messages.after_slice_report(tid, after_message_id=rows[0].message_id, limit=10, budget=budget(5))
        assert not result.rows and not result.load_errors
        if not result.partial:
            assert index >= 3
            break
    else:
        pytest.fail('display-only tail never reached proved EOF')


def test_thread_discovery_and_slow_location_share_one_budget_and_progress(tmp_path):
    store, tid, rows = make_messages(tmp_path, count=1000)
    fast = store.threads.get_or_create({'canonical_user_id': 'synthetic-owner', 'channel': 'chat',
        'channel_user_id': 'synthetic-owner', 'channel_conversation_id': 'fast', 'now': 3.0})
    message = store.messages.append({'thread_id': fast.thread_id, 'role': 'user', 'content': 'fast'})
    settle_files([store.storage.message_path(tid), store.storage.message_path(fast.thread_id)])
    cursors = {tid: rows[-1].message_id}
    seen = set()
    for _ in range(100):
        state = {}
        total = budget(64)
        selected, errors = _collect_messages(source=CuratorMessageSource(store), cursors=cursors,
            limit=20, max_chars=20000, budget=total, scan_state=state)
        assert not errors and total.entries <= 64
        seen.update(item.message_id for item in selected)
        assert state.get('partial') is not None
        if message.message_id in seen:
            break
    assert message.message_id in seen
    assert cursors[tid] == rows[-1].message_id


def test_expired_budget_performs_no_source_open(tmp_path, monkeypatch):
    path = tmp_path / 'not-opened.jsonl'
    monkeypatch.setattr(Path, 'open', lambda *a, **k: pytest.fail('expired budget opened source'))
    result = read_after_slice(path, 'target', 1, RetentionScanBudget(128, 8388608, time.monotonic() - 1))
    assert result.partial and not result.rows and not result.load_errors


def _seed_cursor(service, cursors):
    from agent_py_agent.agent.common.json_io import locked_json_path
    with locked_json_path(service.state_store.path):
        current = service.state_store._load_unlocked()
        service.state_store._write_unlocked(replace(current, per_thread_cursors=cursors))


def test_service_partial_keeps_lease_reason_consumption_and_finalize_pending(tmp_path):
    from agent_py_agent.tests.test_memory_curator_v2 import (
        _service,
        _StaticStructuredBackend,
        _valid_output,
    )
    store, tid, rows = make_messages(tmp_path, count=5000)
    settle_files([store.storage.message_path(tid)])
    backend = _StaticStructuredBackend(_valid_output(tid, rows[-1].message_id, rows[-1].content))
    service = _service(tmp_path, backend, store)
    _seed_cursor(service, {tid: rows[-1].message_id})
    service.request('daily_finalize')
    before = service.state_store.load()
    first = service.run(reason='daily_finalize')
    after = service.state_store.load()
    assert first.status == 'busy' and 'message_scan_partial' in first.warnings
    assert backend.calls == 0 and service.run_log.list() == []
    assert after.per_thread_cursors == before.per_thread_cursors
    assert after.pending_reasons == before.pending_reasons and not after.active_lease
    assert not after.last_daily_finalize_date and not after.last_success_at
    for _ in range(2 * 5000 // service.config.retention_scan_slice_entries + 10):
        result = service.run_if_due()
        if result.status == 'succeeded':
            break
    assert result.status == 'succeeded' and backend.calls == 0
    done = service.state_store.load()
    assert not done.pending_reasons and done.last_daily_finalize_date
    assert done.per_thread_cursors == before.per_thread_cursors


def test_other_thread_commit_does_not_consume_partial_reason(tmp_path):
    from agent_py_agent.tests.test_memory_curator_v2 import (
        _service,
        _StaticStructuredBackend,
        _valid_output,
    )
    store, tid, rows = make_messages(tmp_path, count=5000)
    fast = store.threads.get_or_create({'canonical_user_id': 'synthetic-owner', 'channel': 'chat',
        'channel_user_id': 'synthetic-owner', 'channel_conversation_id': 'fast', 'now': 3.0})
    message = store.messages.append({'thread_id': fast.thread_id, 'role': 'user', 'content': '我的个人电脑使用 macOS。'})
    settle_files([store.storage.message_path(tid), store.storage.message_path(fast.thread_id)])
    backend = _StaticStructuredBackend(_valid_output(fast.thread_id, message.message_id, message.content))
    service = _service(tmp_path, backend, store)
    _seed_cursor(service, {tid: rows[-1].message_id})
    service.request('daily_finalize')
    result = service.run(reason='daily_finalize')
    state = service.state_store.load()
    assert result.status == 'succeeded' and backend.calls == 1
    assert 'message_scan_partial' in result.warnings
    assert state.per_thread_cursors[tid] == rows[-1].message_id
    assert state.per_thread_cursors[fast.thread_id] == message.message_id
    assert 'daily_finalize' in state.pending_reasons and not state.last_daily_finalize_date
    for _ in range(2 * 5000 // service.config.retention_scan_slice_entries + 10):
        result = service.run_if_due()
        if result.status == 'succeeded' and not service.state_store.load().pending_reasons:
            break
    assert not service.state_store.load().pending_reasons
    assert backend.calls == 1


@pytest.mark.parametrize('scheduled', [False, True])
def test_verified_audit_skip_commits_during_unfinished_message_scan(tmp_path, scheduled):
    from datetime import datetime, timezone

    from agent_py_agent.tests.test_memory_curator_v2 import (
        _service,
        _StaticStructuredBackend,
        _valid_output,
    )

    store, tid, rows = make_messages(tmp_path, count=1000)
    skipped = store.threads.create()
    store.threads.update_atomic(skipped.thread_id, lambda row: replace(row, metadata={'learning_excluded': True}))
    backend = _StaticStructuredBackend(_valid_output(tid, rows[-1].message_id, rows[-1].content))
    service = _service(tmp_path, backend, store)
    _seed_cursor(service, {tid: rows[-1].message_id})
    service.audit_dir.mkdir(exist_ok=True, parents=True)
    (service.audit_dir / '2026-01-01.jsonl').write_text(json.dumps({
        'event_id': 'excluded-on-purpose', 'thread_id': skipped.thread_id}) + '\n', encoding='utf-8')
    if scheduled:
        result = service.run_if_due(now=datetime(2026, 1, 5, 10, tzinfo=timezone.utc))
    else:
        service.request('daily_finalize')
        result = service.run(reason='daily_finalize')
    state = service.state_store.load()
    assert result.status == 'succeeded' and 'message_scan_partial' in result.warnings
    assert state.last_processed_audit_event_id == 'excluded-on-purpose' and backend.calls == 0
    assert state.per_thread_cursors == {tid: rows[-1].message_id}
    assert state.pending_reasons and not state.last_daily_finalize_date


@pytest.mark.parametrize('portable', [False, True])
def test_discovery_pages_all_threads_without_loading_entire_history(tmp_path, monkeypatch, portable):
    from agent_py_agent.agent.memory_store import curator_inputs
    from agent_py_agent.agent.user_space import directory_paging
    store, tid, _rows = make_messages(tmp_path, count=1)
    # 同规范目录写1000份有效线程，避免用fake目录页替代实际枚举。
    original = json.loads(store.storage.thread_path(tid).read_text())
    for number in range(1000):
        identifier = f'thread-{number:016x}'
        store.storage.thread_path(identifier).write_text(json.dumps({**original, 'thread_id': identifier}))
    if portable:
        monkeypatch.setattr(directory_paging, '_native_directory_supported', lambda: False)
    seen = set()
    real = store.threads._load_path_report
    per_call = []
    def observe(path, **kwargs):
        per_call.append(path.stem)
        return real(path, **kwargs)
    monkeypatch.setattr(store.threads, '_load_path_report', observe)
    for _ in range(80):
        per_call.clear()
        rows, errors, window = curator_inputs._message_thread_window(store, budget(128))
        assert not errors and len(per_call) <= 16
        seen.update(row.thread_id for row in rows)
        key, checkpoint = window
        checkpoint['waiting'] = []
        curator_inputs._THREAD_DISCOVERY.put(key, checkpoint)
        if checkpoint['complete']:
            break
    assert len(seen) == 1001


def test_closed_owner_progress_is_not_evicted_by_thread_eof_entries(tmp_path):
    from agent_py_agent.agent.memory_store import curator_inputs
    store, _tid, _ = make_messages(tmp_path, count=1)
    _rows, _errors, window = curator_inputs._message_thread_window(store, budget())
    key, checkpoint = window
    curator_inputs._THREAD_DISCOVERY.put(key, checkpoint)
    for number in range(20000):
        curator_inputs._COLD_THREAD_EOF.put(('thread-eof', number), True)
    assert curator_inputs._THREAD_DISCOVERY.get(key) is checkpoint


def test_cold_canonical_id_uses_existing_index_and_keeps_missing_segment_unknown(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation import message_scan
    from agent_py_agent.tests.test_rework_r9_conversation_transcript_retention import (
        _owner,
        _rotated_conversation,
    )
    home, _retention = _owner(tmp_path)
    store, thread, path, original = _rotated_conversation(home, monkeypatch)
    source = [json.loads(line) for line in original.splitlines()]
    manifest = message_scan.transcript_manifest(path)
    assert manifest
    result = store.messages.after_slice_report(thread.thread_id, after_message_id=source[0]['message_id'], limit=1, budget=budget())
    assert not result.load_errors and result.rows[0].message_id == source[1]['message_id']
    assert result.rows[0].content == source[1]['content']
    # 原冷副本缺失不能把热尾当作完整历史或EOF；路径取原清单的正式段写者合同。
    segment = path.parent / 'archive' / thread.thread_id / 'seg-00000000000000000001.jsonl'
    assert segment.is_file()
    segment.unlink()
    result = store.messages.after_slice_report(thread.thread_id, after_message_id=source[0]['message_id'], limit=1, budget=budget())
    assert result.load_errors and not result.rows


def test_write_during_slice_discards_coverage_and_closes_reader(tmp_path, monkeypatch):
    import threading

    from agent_py_agent.agent.conversation import message_cursor_slice
    store, tid, rows = make_messages(tmp_path, count=100)
    path = store.storage.message_path(tid)
    settle_files([path])
    entered, release = threading.Event(), threading.Event()
    real = message_cursor_slice._line
    outputs = []
    first = True
    def pause(handle, scan_budget):
        nonlocal first
        raw = real(handle, scan_budget)
        if first:
            first = False
            entered.set()
            assert release.wait(5)
        return raw
    monkeypatch.setattr(message_cursor_slice, '_line', pause)
    worker = threading.Thread(target=lambda: outputs.append(store.messages.after_slice_report(tid,
        after_message_id=rows[-1].message_id, limit=1, budget=budget(10))))
    worker.start()
    try:
        assert entered.wait(5)
        raw = path.read_bytes()
        path.write_bytes(b'x' + raw[1:])
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive() and outputs[0].partial and not outputs[0].rows
    settle_files([path])
    result = store.messages.after_slice_report(tid, after_message_id=rows[-1].message_id, limit=1, budget=budget(10))
    assert result.load_errors and not result.rows


def test_actual_message_handles_close_after_partial_success_and_error(tmp_path, monkeypatch):
    store, tid, rows = make_messages(tmp_path, count=30)
    path = store.storage.message_path(tid)
    settle_files([path])
    opened = []
    original = Path.open
    def observe(self, *args, **kwargs):
        handle = original(self, *args, **kwargs)
        if self == path and args and args[0] == 'rb':
            opened.append(handle)
        return handle
    monkeypatch.setattr(Path, 'open', observe)
    for _ in range(5):
        result = store.messages.after_slice_report(tid, after_message_id=rows[-1].message_id, limit=1, budget=budget(10))
        assert not result.load_errors
        assert opened and all(handle.closed for handle in opened)
        if not result.partial:
            break
    path.write_bytes(b'{bad}\n')
    settle_files([path])
    assert store.messages.after_slice_report(tid, after_message_id=rows[-1].message_id, limit=1, budget=budget(10)).load_errors
    assert all(handle.closed for handle in opened)


def test_large_hot_target_then_small_message_resumes_after_suffix_anchor(tmp_path):
    from agent_py_agent.agent.conversation.message_cursor_slice import MessageReadAllowance
    store, tid, rows = make_messages(tmp_path, count=2, content_chars=0)
    rows[0] = replace(rows[0], content='x' * (9 * 1024 * 1024))
    rows[1] = replace(rows[1], content='only next business message')
    path = store.storage.message_path(tid)
    path.write_bytes(b''.join(encode_row(row.to_dict()) for row in rows))
    settle_files([path])
    first = store.messages.after_slice_report(tid, after_message_id=rows[0].message_id, limit=1,
        budget=MessageReadAllowance(budget()))
    assert first.partial and not first.rows and first.continuation.phase == 'tail'
    anchor = first.continuation.anchor
    assert anchor[1] - anchor[0] == 65536 and len(anchor[2]) == 32
    assert len(first.continuation.seen) <= 8192
    second = store.messages.after_slice_report(tid, after_message_id=rows[0].message_id, limit=1,
        budget=MessageReadAllowance(budget()))
    assert not second.partial and not second.load_errors and second.rows == (rows[1],)


def test_shared_atomic_allowance_is_once_for_entire_message_collection():
    from agent_py_agent.agent.conversation.message_cursor_slice import MessageReadAllowance
    parent = MessageReadAllowance(budget())
    first = MessageReadAllowance(budget(), parent)
    second = MessageReadAllowance(budget(), parent)
    assert first.claim_atomic_record()
    assert not second.claim_atomic_record()
    # 同一原子行由冷段加载到逐行返回，只复用同一例外；读过其它行不能再领。
    first.charge(entries=1)
    assert first.claim_atomic_record(current_probe_entries=1)
    first.charge(entries=1)
    assert not first.claim_atomic_record(current_probe_entries=1)


def test_first_sample_with_recent_ctime_is_not_cached(tmp_path, monkeypatch):
    from agent_py_agent.agent.common.cache_freshness import cache_entry_trustworthy
    from agent_py_agent.agent.conversation import message_cursor_slice
    from agent_py_agent.agent.io.cursor_cache import cursor_key
    store, tid, rows = make_messages(tmp_path, count=100)
    path = store.storage.message_path(tid)
    settle_files([path])
    monkeypatch.setattr(message_cursor_slice, 'cache_entry_trustworthy', cache_entry_trustworthy)
    assert cache_entry_trustworthy(path.stat().st_mtime_ns)
    assert not cache_entry_trustworthy(path.stat().st_ctime_ns)
    first = store.messages.after_slice_report(tid, after_message_id=rows[-1].message_id, budget=budget(10))
    assert first.partial and first.continuation.offset > 0
    assert message_cursor._MESSAGE_CURSORS.get((*cursor_key(path, rows[-1].message_id), 'bounded-read')) is None
    second = store.messages.after_slice_report(tid, after_message_id=rows[-1].message_id, budget=budget(10))
    assert second.partial and second.continuation.offset == first.continuation.offset


def test_host_scan_configuration_is_not_replaced_by_batch_size(tmp_path, monkeypatch):
    from agent_py_agent.agent.memory_store import curator_inputs
    from agent_py_agent.agent.memory_store.curator_models import MemoryCuratorConfig
    from agent_py_agent.agent.settings.config import AgentConfig
    from agent_py_agent.tests.test_memory_curator_v2 import (
        _service,
        _StaticStructuredBackend,
        _valid_output,
    )
    host = AgentConfig(retention_scan_slice_entries=2, retention_scan_slice_bytes=4096, retention_scan_slice_ms=17)
    config = MemoryCuratorConfig.from_agent_config(host)
    assert (config.retention_scan_slice_entries, config.retention_scan_slice_bytes, config.retention_scan_slice_ms) == (2, 4096, 17)
    store, tid, rows = make_messages(tmp_path, count=10)
    service = _service(tmp_path, _StaticStructuredBackend(_valid_output(tid, rows[-1].message_id, rows[-1].content)), store, config=config)
    original = curator_inputs._collect_messages
    seen = []
    def observe(**kwargs):
        live = kwargs['budget']
        seen.append((live.max_entries, live.max_bytes, live.deadline - time.monotonic()))
        result = original(**kwargs)
        assert live.entries <= 2 and live.size <= 4096
        return result
    monkeypatch.setattr(curator_inputs, '_collect_messages', observe)
    service._collect(service.state_store.load(), include_formal=False)
    assert seen and seen[0][0:2] == (2, 4096) and 0 < seen[0][2] <= .017


def test_one_entry_budget_eventually_resumes_instead_of_repaying_discovery_forever(tmp_path):
    from agent_py_agent.agent.memory_store.curator_models import MemoryCuratorConfig
    from agent_py_agent.tests.test_memory_curator_v2 import (
        _service,
        _StaticStructuredBackend,
        _valid_output,
    )
    store, tid, rows = make_messages(tmp_path, count=5)
    settle_files([store.storage.message_path(tid)])
    backend = _StaticStructuredBackend(_valid_output(tid, rows[-1].message_id, rows[-1].content))
    service = _service(tmp_path, backend, store, config=MemoryCuratorConfig(retention_scan_slice_entries=1))
    _seed_cursor(service, {tid: rows[-1].message_id})
    service.request('admin')
    for _ in range(40):
        result = service.run_if_due()
        if result.status == 'succeeded':
            break
        assert result.status == 'busy'
        assert service.state_store.load().per_thread_cursors[tid] == rows[-1].message_id
    assert result.status == 'succeeded' and backend.calls == 0


def test_exact_metadata_byte_budget_defers_once_then_serves_big_and_small(tmp_path):
    from agent_py_agent.agent.memory_store import curator_inputs
    store, big, _rows = make_messages(tmp_path, count=1)
    store.threads.update_atomic(big, lambda row: replace(row, title='x' * 4096))
    small = store.threads.get_or_create({'canonical_user_id': 'synthetic-owner', 'channel': 'chat',
        'channel_user_id': 'synthetic-owner', 'channel_conversation_id': 'small', 'now': 3.0})
    store.messages.append({'thread_id': small.thread_id, 'role': 'user', 'content': 'small'})
    maximum = store.storage.thread_path(big).stat().st_size
    root_key = ('curator-thread-discovery', str(store.storage.root))
    curator_inputs._THREAD_DISCOVERY.put(root_key,
        {'cursor': {}, 'waiting': [big, small.thread_id], 'complete': True, 'round_partial': False})
    ids = set()
    for _ in range(6):
        shared = budget(128, size=maximum)
        state = {}
        selected, errors = _collect_messages(source=CuratorMessageSource(store), cursors={}, limit=20,
            max_chars=20000, budget=shared, scan_state=state)
        assert not errors and shared.size <= maximum
        ids.update(item.thread_id for item in selected)
        if ids == {big, small.thread_id}:
            break
    assert ids == {big, small.thread_id}
    assert store.threads.require(big).title == 'x' * 4096
    memo = curator_inputs._THREAD_DISCOVERY.get(root_key).get('memo')
    if memo is not None:
        assert len(memo['payload'].encode()) <= 8192 and 'xxxx' not in memo['payload']


def test_missing_bounded_capability_never_falls_back_to_full_list(tmp_path, monkeypatch):
    from agent_py_agent.tests.test_memory_curator_v2 import (
        _service,
        _StaticStructuredBackend,
        _valid_output,
    )
    store, tid, rows = make_messages(tmp_path, count=1)
    service = _service(tmp_path, _StaticStructuredBackend(_valid_output(tid, rows[0].message_id, rows[0].content)), store)
    monkeypatch.delattr(type(store.threads), 'page_report')
    monkeypatch.setattr(store.threads, 'list_report', lambda **kwargs: pytest.fail('missing bounded reader caused full scan'))
    with pytest.raises(TypeError, match='bounded Memory Curator'):
        service._collect(service.state_store.load(), include_formal=False)


@pytest.mark.parametrize('change', ['none', 'delete'])
def test_large_cold_target_proof_resumes_without_body_retention_or_false_eof(tmp_path, monkeypatch, change):
    from agent_py_agent.agent.common import nofollow_fs
    from agent_py_agent.agent.conversation.message_cursor_slice import MessageReadAllowance
    from agent_py_agent.tests import test_rework_r9_conversation_transcript_retention as fixtures
    class Bodies:
        def __init__(self):
            self.count = 0
        def __radd__(self, prefix):
            self.count += 1
            return prefix + ('x' * (9 * 1024 * 1024) if self.count == 1 else 'small business message')
    monkeypatch.setattr(fixtures, 'ROWS', 3)
    monkeypatch.setattr(fixtures, 'ROW_BODY', Bodies())
    home, _retention = fixtures._owner(tmp_path)
    store, thread, path, original = fixtures._rotated_conversation(home, monkeypatch)
    source = [json.loads(line) for line in original.splitlines()]
    assert len(source[0]['content']) > 8 * 1024 * 1024
    reads = []
    actual = nofollow_fs.read_bytes_snapshot_beneath
    def observe(*args, **kwargs):
        result = actual(*args, **kwargs)
        if result is not None and args[1][-1].startswith('seg-'):
            reads.append(len(result.content))
        return result
    monkeypatch.setattr(nofollow_fs, 'read_bytes_snapshot_beneath', observe)
    first = store.messages.after_slice_report(thread.thread_id, after_message_id=source[0]['message_id'], limit=1,
        budget=MessageReadAllowance(budget()))
    assert first.partial and first.continuation.cold_sources and not first.rows
    assert len(reads) == 1
    if change == 'delete':
        (path.parent / 'archive' / thread.thread_id / 'seg-00000000000000000001.jsonl').unlink()
    second = store.messages.after_slice_report(thread.thread_id, after_message_id=source[0]['message_id'], limit=1,
        budget=MessageReadAllowance(budget()))
    if change == 'delete':
        assert second.load_errors and not second.rows
    else:
        assert not second.partial and not second.load_errors and second.rows[0].content == source[1]['content']
        assert len(reads) == 1


def test_permission_error_on_new_source_cannot_become_empty_eof(tmp_path, monkeypatch):
    store, tid, _rows = make_messages(tmp_path, count=1)
    path = store.storage.message_path(tid)
    real = Path.stat
    def denied(self, *args, **kwargs):
        if self == path:
            raise PermissionError('injected source denial')
        return real(self, *args, **kwargs)
    monkeypatch.setattr(Path, 'stat', denied)
    result = store.messages.after_slice_report(tid, budget=budget())
    assert result.load_errors and not result.rows and not result.partial
