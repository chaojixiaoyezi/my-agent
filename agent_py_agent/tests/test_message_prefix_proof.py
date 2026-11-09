"""纯提示的否定证明：碰撞只能丢优化，覆盖、身份和容量不能猜。"""
import threading

from agent_py_agent.agent.conversation.message_prefix_proof import HotPrefixCache
from agent_py_agent.agent.io.cursor_cache import FileStamp, note_seen

STAMP = FileStamp(1, 2, 1000, 3, 4)


def _begin(cache, path='source', start=0, stamp=STAMP, changed=5, trusted=True):
    return cache.begin(path, stamp, changed, start, trusted=trusted)


def test_contiguous_prefix_can_prove_only_new_short_ids():
    cache = HotPrefixCache(filter_bytes=1024, max_sources=2, min_source_bytes=0)
    first = _begin(cache)
    assert first.observe(0, 10, 'old')
    first.close(verified=True)
    second = _begin(cache, start=10)
    assert not second.observe(10, 20, 'old')
    assert second.observe(20, 30, 'new')
    second.close(verified=True)
    assert cache.usage() == (1, 1024)
    assert not second.observe(30, 40, 'late')
    second.close(verified=True)


def test_real_filter_collision_is_only_a_miss_not_first_occurrence():
    bits = bytearray(1)
    assert not note_seen(bits, 'a')
    collision = next(str(i) for i in range(1000) if note_seen(bytearray(bits), str(i)))
    cache = HotPrefixCache(filter_bytes=1, min_source_bytes=0)
    one = _begin(cache)
    assert one.observe(0, 10, 'a')
    assert not one.observe(10, 20, collision)
    one.close(verified=True)


def test_changed_generation_same_stat_and_foreign_source_cannot_inherit_prefix():
    cache = HotPrefixCache(filter_bytes=16, min_source_bytes=0)
    one = _begin(cache)
    one.observe(0, 10, 'old')
    one.close(verified=True)
    assert _begin(cache, start=10, changed=6) is None
    assert _begin(cache, 'foreign', start=10) is None
    assert _begin(cache, start=0, trusted=False) is None
    fresh = _begin(cache, changed=6)
    assert fresh.observe(0, 10, 'old')
    fresh.close(verified=True)


def test_gaps_blank_or_failed_final_verification_cannot_publish_coverage():
    for invalid in ('gap', 'blank', 'failed'):
        cache = HotPrefixCache(filter_bytes=16, min_source_bytes=0)
        one = _begin(cache)
        assert one.observe(0, 10, 'old')
        if invalid == 'gap':
            assert not one.observe(11, 20, 'later')
        elif invalid == 'blank':
            assert not one.observe(10, 20, None)
        one.close(verified=invalid != 'failed')
        assert _begin(cache, start=10) is None


def test_active_entries_are_not_evicted_and_contention_does_not_wait():
    cache = HotPrefixCache(filter_bytes=16, max_sources=1, min_source_bytes=0)
    one = _begin(cache)
    observed = []
    thread = threading.Thread(target=lambda: observed.extend([
        _begin(cache), _begin(cache, 'foreign')]))
    thread.start()
    thread.join(1)
    assert not thread.is_alive() and observed == [None, None]
    assert cache.usage() == (1, 16)
    one.close(verified=True)
    other = _begin(cache, 'foreign')
    assert one._entry is None
    assert cache.usage() == (1, 16)
    other.close()


def test_many_sources_keep_fixed_total_bitset_bytes():
    cache = HotPrefixCache(filter_bytes=64, max_sources=2, min_source_bytes=0)
    for number in range(100):
        one = _begin(cache, str(number))
        one.observe(0, 10, str(number))
        one.close(verified=True)
        assert cache.usage() == (min(number + 1, 2), 64 * min(number + 1, 2))


def test_foreign_thread_cannot_advance_or_close_borrowed_proof():
    cache = HotPrefixCache(filter_bytes=64, min_source_bytes=0)
    one = _begin(cache)
    result = []

    def foreign():
        result.append(one.observe(0, 10, 'foreign'))
        one.close(verified=True)

    thread = threading.Thread(target=foreign)
    thread.start()
    thread.join(1)
    assert not thread.is_alive() and result == [False]
    assert one.observe(0, 10, 'owner')
    one.close(verified=True)
    following = _begin(cache, start=10)
    assert following is not None
    following.close(verified=True)


def _stable_jsonl(tmp_path, rows):
    import json
    import os
    import time

    path = tmp_path / 'thread.jsonl'
    with path.open('wb') as stream:
        for row in rows:
            stream.write(b'\n' if row is None else (json.dumps(row) + '\n').encode())
    old = time.time() - 10
    os.utime(path, (old, old))
    time.sleep(2.05)  # 只在准备阶段等真实ctime离开原2秒信任窗，不伪造源代次。
    return path


def test_real_100k_source_three_logical_pages_parse_prefix_once(tmp_path, monkeypatch):
    import json
    import sys

    from agent_py_agent.agent.conversation import message_cursor as cursors
    from agent_py_agent.agent.conversation import message_cursor_slice as reader
    from agent_py_agent.agent.io.cursor_cache import CursorCache
    from agent_py_agent.agent.memory_store.retention_scan_budget import RetentionScanBudget

    monkeypatch.setattr(reader, 'HOT_PREFIXES', HotPrefixCache())
    monkeypatch.setattr(cursors, '_MESSAGE_CURSORS', CursorCache(32768))
    count = 100_000
    path = _stable_jsonl(tmp_path, ({'message_id': f'msg-{i:06}', 'thread_id': 'thread', 'role': 'user',
        'content': 'x' * 100, 'metadata': {}, 'created_at': float(i)} for i in range(count)))
    loads = json.loads
    parsed_prefix = [0]

    def observed(*args, **kwargs):
        row = loads(*args, **kwargs)
        frame = sys._getframe(1)
        if frame.f_code is reader.read_after_slice.__code__ and frame.f_locals.get('phase') == 'locate':
            parsed_prefix[0] += 1
        return row

    monkeypatch.setattr(json, 'loads', observed)
    target = 'msg-099799'
    pages, prefix_work = [], []
    for _logical in range(3):
        before = parsed_prefix[0]
        selected = []
        for _slice in range(2000):
            budget = RetentionScanBudget.default()
            result = reader.read_after_slice(path, target, 100 - len(selected), reader.MessageReadAllowance(budget))
            assert budget.entries <= 128 and not result.load_errors
            if result.rows:
                selected.extend(row['message_id'] for row in result.rows)
                target = selected[-1]
            if not result.partial and not result.has_more:
                break
            if len(selected) == 100:
                break
        else:
            raise AssertionError('bounded continuation did not finish')
        pages.append(selected)
        prefix_work.append(parsed_prefix[0] - before)
    assert pages == [[f'msg-{i:06}' for i in range(99800, 99900)],
                     [f'msg-{i:06}' for i in range(99900, 100000)], []]
    assert prefix_work == [99800, 0, 0]
    assert reader.HOT_PREFIXES.usage() == (1, 256 * 1024)


def test_real_duplicate_id_does_not_replace_original_first_position(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation import message_cursor as cursors
    from agent_py_agent.agent.conversation import message_cursor_slice as reader
    from agent_py_agent.agent.io.cursor_cache import CursorCache
    from agent_py_agent.agent.memory_store.retention_scan_budget import RetentionScanBudget

    monkeypatch.setattr(reader, 'HOT_PREFIXES', HotPrefixCache(min_source_bytes=0))
    monkeypatch.setattr(cursors, '_MESSAGE_CURSORS', CursorCache(32768))
    path = _stable_jsonl(tmp_path, [{'message_id': key, 'role': 'user', 'content': key}
                                  for key in ('duplicate', 'middle', 'duplicate')])
    result = reader.read_after_slice(path, '', 3, RetentionScanBudget.default())
    assert [row['message_id'] for row in result.rows] == ['duplicate', 'middle', 'duplicate']
    following = reader.read_after_slice(path, 'duplicate', 1, RetentionScanBudget.default())
    assert following.rows[0]['message_id'] == 'middle'


def test_real_source_rewrite_during_slice_cannot_publish_prefix(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation import message_cursor_slice as reader
    from agent_py_agent.agent.memory_store.retention_scan_budget import RetentionScanBudget

    cache = HotPrefixCache(min_source_bytes=0)
    monkeypatch.setattr(reader, 'HOT_PREFIXES', cache)
    path = _stable_jsonl(tmp_path, [{'message_id': 'one', 'role': 'user', 'content': 'one'}])
    original = reader._line
    changed = []

    def rewrite(handle, budget):
        raw = original(handle, budget)
        if raw and not changed:
            path.write_bytes(path.read_bytes().replace(b'one', b'two'))
            changed.append(True)
        return raw

    monkeypatch.setattr(reader, '_line', rewrite)
    result = reader.read_after_slice(path, '', 1, RetentionScanBudget.default())
    assert result.partial and result.reason == 'source_changed' and not result.rows
    assert cache.usage() == (1, 0)


def test_real_blank_prefix_never_creates_a_later_first_id_hint(tmp_path, monkeypatch):
    import pytest

    from agent_py_agent.agent.conversation import message_cursor as cursors
    from agent_py_agent.agent.conversation import message_cursor_slice as reader
    from agent_py_agent.agent.io.cursor_cache import CursorCache, cursor_key
    from agent_py_agent.agent.memory_store.retention_scan_budget import RetentionScanBudget
    from agent_py_agent.agent.runtime_errors import DataCorruptionError

    monkeypatch.setattr(reader, 'HOT_PREFIXES', HotPrefixCache(min_source_bytes=0))
    monkeypatch.setattr(cursors, '_MESSAGE_CURSORS', CursorCache(32768))
    path = _stable_jsonl(tmp_path, [{'message_id': 'before', 'role': 'user'}, None,
                                   {'message_id': 'after', 'role': 'user'}])
    tail = reader.read_after_slice(path, '', 2, RetentionScanBudget.default())
    assert [row['message_id'] for row in tail.rows] == ['before', 'after']
    assert cursors._MESSAGE_CURSORS.get(cursor_key(path, 'after')) is None
    with pytest.raises(DataCorruptionError):
        reader.read_after_slice(path, 'after', 1, RetentionScanBudget.default())


def test_eviction_clears_old_bitset_even_if_closed_metadata_is_retained():
    cache = HotPrefixCache(filter_bytes=64, max_sources=1, min_source_bytes=0)
    one = _begin(cache)
    retained = one._entry.bits
    one.close(verified=True)
    other = _begin(cache, 'foreign')
    assert len(retained) == 0 and retained.__alloc__() <= 1
    assert cache.usage() == (1, 64)
    other.close(verified=True)


def test_inherited_pid_abstains_without_touching_parent_lock(monkeypatch):
    import os

    from agent_py_agent.agent.conversation import message_prefix_proof as proof

    cache = HotPrefixCache(filter_bytes=64, min_source_bytes=0)
    one = _begin(cache)
    pid = os.getpid()
    with cache._lock:
        with monkeypatch.context() as child:
            child.setattr(proof.os, 'getpid', lambda: pid + 1)
            assert _begin(cache) is None
            assert not one.observe(0, 10, 'child')
            assert cache.usage() == (1, 64)
    assert one.observe(0, 10, 'parent')
    one.close(verified=True)
