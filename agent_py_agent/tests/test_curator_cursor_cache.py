# LLM: 合成owner与原API验证hint隔离/失效；fresh负例仅控制缓存输入与时间年龄，真实stat和原损坏诊断不被替换。
# 模块用途: 覆盖E11c预算、首ID、LRU及粗时间窗口内不入缓存/不命中，稳态断言只对真实稳定文件成立。
from __future__ import annotations

import os
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.common import cache_freshness
from agent_py_agent.agent.conversation import message_cursor
from agent_py_agent.agent.conversation.display_checkpoint import DISPLAY_CHECKPOINT_ROLE
from agent_py_agent.agent.io.cursor_cache import CursorCache, cursor_key, file_stamp
from agent_py_agent.agent.memory_store import curator_audit_cursor
from agent_py_agent.agent.memory_store.curator_inputs import (
    CuratorMessageSource,
    _collect_audit,
    _collect_messages,
)
from agent_py_agent.agent.runtime_errors import DataCorruptionError
from agent_py_agent.tests.support.curator_scan_cases import (
    encode_row,
    make_audit,
    make_messages,
    observe_reads,
    settle_files,
)
from agent_py_agent.tests.support.curator_scan_legacy import (
    LegacyMessageReader,
    legacy_collect_audit,
)


def test_changed_anchor_id_falls_back_and_preserves_missing_error(tmp_path):
    store, tid, rows = make_messages(tmp_path, count=3)
    path = store.storage.message_path(tid)
    cursor = rows[-1].message_id
    store.messages.after_report(tid, after_message_id=cursor)
    raw = path.read_bytes().replace(cursor.encode(), b"msg-xxxxx")
    path.write_bytes(raw + encode_row(replace(rows[-1], message_id="new").to_dict()))
    expected = LegacyMessageReader(store).after_report(tid, after_message_id=cursor)
    assert expected[1] and store.messages.after_report(tid, after_message_id=cursor) == expected
    with pytest.raises(DataCorruptionError):
        store.messages.byte_offset_after(tid, cursor)


def test_replaced_growing_inode_keeps_old_prefix_corruption_visible(tmp_path):
    store, tid, rows = make_messages(tmp_path, count=3)
    path = store.storage.message_path(tid)
    cursor = rows[-1].message_id
    store.messages.after_report(tid, after_message_id=cursor)
    first = encode_row(rows[0].to_dict())
    raw = b"x" * (len(first) - 1) + b"\n" + path.read_bytes()[len(first):]
    replacement = path.with_suffix(".replacement")
    replacement.write_bytes(raw + encode_row(replace(rows[-1], message_id="new").to_dict()))
    os.replace(replacement, path)
    expected = LegacyMessageReader(store).after_report(tid, after_message_id=cursor)
    assert expected[1] and store.messages.after_report(tid, after_message_id=cursor) == expected


def test_same_size_replaced_end_cannot_skip_identity_check(tmp_path):
    store, tid, rows = make_messages(tmp_path, count=3)
    path = store.storage.message_path(tid)
    cursor = rows[-1].message_id
    store.messages.after_report(tid, after_message_id=cursor)
    raw = path.read_bytes().replace(cursor.encode(), b"msg-xxxxx")
    replacement = path.with_suffix(".replacement")
    replacement.write_bytes(raw)
    os.replace(replacement, path)
    expected = LegacyMessageReader(store).after_report(tid, after_message_id=cursor)
    assert expected[1] and store.messages.after_report(tid, after_message_id=cursor) == expected


def test_missing_audit_cursor_returns_original_corruption_not_replay(tmp_path):
    root = tmp_path / "audit"
    make_audit(root)
    actual = _collect_audit(root, after_event_id="missing-cursor", limit=10, max_chars=20_000)
    assert actual == ([], [{"context": "memory_curator.audit_cursor", "error_code": "AUDIT_CURSOR_MISSING", "event_id": "missing-cursor"}])
    assert actual == legacy_collect_audit(root, after_event_id="missing-cursor", limit=10, max_chars=20_000)


def test_message_cache_keys_and_eviction_are_owner_local(tmp_path, monkeypatch):
    cache = CursorCache(1)
    monkeypatch.setattr(message_cursor, "_MESSAGE_CURSORS", cache)
    a, atid, arows = make_messages(tmp_path / "owner-a")
    b, btid, brows = make_messages(tmp_path / "owner-b")
    apath, bpath = a.storage.message_path(atid), b.storage.message_path(btid)
    settle_files([apath, bpath])
    cursor = arows[-1].message_id
    assert cursor == brows[-1].message_id and cursor_key(apath, cursor) != cursor_key(bpath, cursor)
    assert a.messages.after_report(atid, after_message_id=cursor) == ([], [])
    assert cache.get(cursor_key(bpath, cursor)) is None
    assert b.messages.after_report(btid, after_message_id=cursor) == ([], [])
    assert len(cache._items) == 1 and cache.get(cursor_key(apath, cursor)) is None
    counts = observe_reads(monkeypatch, [apath])
    assert a.messages.after_report(atid, after_message_id=cursor) == ([], [])
    assert counts.opens


def test_audit_cache_keys_and_eviction_are_owner_local(tmp_path, monkeypatch):
    cache = CursorCache(1)
    monkeypatch.setattr(curator_audit_cursor, "_AUDIT_CURSORS", cache)
    ar, br = tmp_path / "owner-a/audit", tmp_path / "owner-b/audit"
    apaths, arows = make_audit(ar, days=1)
    bpaths, brows = make_audit(br, days=1)
    settle_files([*apaths, *bpaths])
    cursor = arows[-1]["event_id"]
    assert cursor == brows[-1]["event_id"] and cursor_key(ar, cursor) != cursor_key(br, cursor)
    assert _collect_audit(ar, after_event_id=cursor, limit=10, max_chars=20_000) == ([], [])
    assert cache.get(cursor_key(br, cursor)) is None
    assert _collect_audit(br, after_event_id=cursor, limit=10, max_chars=20_000) == ([], [])
    assert len(cache._items) == 1 and cache.get(cursor_key(ar, cursor)) is None
    counts = observe_reads(monkeypatch, apaths)
    assert _collect_audit(ar, after_event_id=cursor, limit=10, max_chars=20_000) == ([], [])
    assert counts.opens


def test_saturated_first_id_filter_only_slows_cursor_advancement(tmp_path, monkeypatch):
    monkeypatch.setattr(message_cursor, "_MESSAGE_CURSOR_FILTER_BYTES", 1)
    store, tid, rows = make_messages(tmp_path, count=200)
    settle_files([store.storage.message_path(tid)])
    assert store.messages.after_report(tid, after_message_id=rows[-2].message_id) == (rows[-1:], [])
    counts = observe_reads(monkeypatch, [store.storage.message_path(tid)])
    assert store.messages.after_report(tid, after_message_id=rows[-1].message_id) == ([], [])
    assert counts.opens


def test_audit_advancement_keeps_first_duplicate_across_shards(tmp_path):
    root = tmp_path / "audit"
    paths, rows = make_audit(root)
    with paths[-1].open("ab") as handle:
        handle.write(encode_row({"event_id": rows[0]["event_id"], "preview": "重复"}))
    _collect_audit(root, after_event_id=rows[5]["event_id"], limit=20, max_chars=20_000)
    expected = legacy_collect_audit(root, after_event_id=rows[0]["event_id"], limit=20, max_chars=20_000)
    assert _collect_audit(root, after_event_id=rows[0]["event_id"], limit=20, max_chars=20_000) == expected


def test_current_audit_shard_checks_corruption_even_beyond_budget(tmp_path):
    root = tmp_path / "audit"
    paths, rows = make_audit(root)
    cursor = rows[5]["event_id"]
    _collect_audit(root, after_event_id=cursor, limit=1, max_chars=1)
    with paths[1].open("ab") as handle:
        handle.write(b"{}\nbroken\n[]\n")
    expected = legacy_collect_audit(root, after_event_id=cursor, limit=1, max_chars=1)
    assert len(expected[1]) == 3
    assert _collect_audit(root, after_event_id=cursor, limit=1, max_chars=1) == expected


def test_display_only_tail_becomes_empty_without_new_opens(tmp_path, monkeypatch):
    store, tid, rows = make_messages(tmp_path, count=3)
    path = store.storage.message_path(tid)
    cursor = rows[-1].message_id
    with path.open("ab") as handle:
        handle.write(encode_row(replace(rows[-1], role=DISPLAY_CHECKPOINT_ROLE, message_id="display").to_dict()))
    settle_files([path])
    assert store.messages.after_report(tid, after_message_id=cursor) == ([], [])
    counts = observe_reads(monkeypatch, [path])
    assert store.messages.after_report(tid, after_message_id=cursor) == ([], [])
    assert counts.opens == []


@pytest.mark.parametrize("budget", [(1, 1000), (20, 900), (20, 20_000)])
def test_multi_thread_mixed_new_and_old_messages_equal_without_writes(tmp_path, budget):
    store, tid, rows = make_messages(tmp_path, count=4)
    other = store.threads.get_or_create({"canonical_user_id": "synthetic-owner", "channel": "chat",
                                        "channel_conversation_id": "second", "channel_user_id": "synthetic-owner", "now": 20.0})
    first = store.messages.append({"thread_id": other.thread_id, "role": "user", "content": "中文\u0085\u2028", "now": 21.0})
    store.messages.append({"thread_id": other.thread_id, "role": "assistant", "content": "新一轮\u2029", "now": 22.0})
    cursors = {tid: rows[-1].message_id, other.thread_id: first.message_id}
    legacy = SimpleNamespace(threads=store.threads, messages=LegacyMessageReader(store))
    limit, max_chars = budget
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    expected = _collect_messages(
        source=CuratorMessageSource(conversation_store=legacy),
        cursors=cursors, limit=limit, max_chars=max_chars,
    )
    current = _collect_messages(
        source=CuratorMessageSource(conversation_store=store),
        cursors=cursors, limit=limit, max_chars=max_chars,
    )
    assert current == expected
    assert _collect_messages(
        source=CuratorMessageSource(conversation_store=store),
        cursors=cursors, limit=limit, max_chars=max_chars,
    ) == expected
    assert {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before
    assert cursors == {tid: rows[-1].message_id, other.thread_id: first.message_id}


@pytest.mark.parametrize("separator", [b"\r", b"\r\n", b"\n"])
def test_audit_preserves_original_universal_newline_boundaries(tmp_path, separator):
    root = tmp_path / "audit"
    paths, rows = make_audit(root, days=1)
    paths[0].write_bytes(separator.join(encode_row(row).rstrip(b"\n") for row in rows) + separator)
    cursor = rows[1]["event_id"]
    expected = legacy_collect_audit(root, after_event_id=cursor, limit=10, max_chars=20_000)
    assert _collect_audit(root, after_event_id=cursor, limit=10, max_chars=20_000) == expected
    assert _collect_audit(root, after_event_id=cursor, limit=10, max_chars=20_000) == expected


def test_blank_after_cursor_still_reads_from_start_after_explicit_offset_lookup(tmp_path):
    store, tid, rows = make_messages(tmp_path, count=3)
    path = store.storage.message_path(tid)
    path.write_bytes(b"".join(encode_row(replace(row, message_id=" " if i == 2 else row.message_id).to_dict()) for i, row in enumerate(rows)))
    assert store.messages.byte_offset_after(tid, " ") == path.stat().st_size
    expected = LegacyMessageReader(store).after_report(tid, after_message_id=" ")
    assert expected[0] and store.messages.after_report(tid, after_message_id=" ") == expected


# LLM: 只控制共享freshness的挂钟年龄；stat、fstat、inode、内容与真实读取完全不替换。
# 函数用途: 让刚写文件在整个负例内保持明确的窗口内年龄，避免慢CI自然跨过两秒造成假绿。
def hold_fresh_clock(monkeypatch, path):
    instant = path.stat().st_mtime_ns / 1_000_000_000 + 0.25
    monkeypatch.setattr(cache_freshness.time, "time", lambda: instant)
    assert not cache_freshness.cache_entry_trustworthy(path.stat().st_mtime_ns)


@pytest.mark.parametrize("kind", ["message", "audit"])
def test_recent_files_never_admit_cursor_hints(tmp_path, monkeypatch, kind):
    cache = CursorCache(4)
    if kind == "message":
        monkeypatch.setattr(message_cursor, "_MESSAGE_CURSORS", cache)
        store, tid, rows = make_messages(tmp_path, count=6)
        path = store.storage.message_path(tid)
        hold_fresh_clock(monkeypatch, path)
        assert store.messages.after_report(tid, after_message_id=rows[-1].message_id) == ([], [])
    else:
        monkeypatch.setattr(curator_audit_cursor, "_AUDIT_CURSORS", cache)
        root = tmp_path / "audit"
        paths, rows = make_audit(root)
        hold_fresh_clock(monkeypatch, paths[-1])
        assert _collect_audit(root, after_event_id=rows[-1]["event_id"], limit=3, max_chars=20_000) == ([], [])
    assert not cache._items


@pytest.mark.parametrize("change", ["missing_anchor", "bad_prefix"])
def test_recent_message_hint_cannot_hide_original_corruption(tmp_path, monkeypatch, change):
    store, tid, rows = make_messages(tmp_path, count=6)
    path = store.storage.message_path(tid)
    settle_files([path])
    target = rows[-1 if change == "missing_anchor" else 2].message_id
    store.messages.after_report(tid, after_message_id=target, limit=1)
    key = cursor_key(path, target)
    cached = message_cursor._MESSAGE_CURSORS.get(key)
    assert cached is not None
    raw = path.read_bytes()
    changed = raw.replace(target.encode(), b"msg-xxxxx") if change == "missing_anchor" else b"!" + raw[1:]
    path.write_bytes(changed)
    # 显式注入同stamp的陈旧hint，模拟已由独立Linux真实重放证明的碰撞；不mock真实stat或正文。
    message_cursor._MESSAGE_CURSORS.put(key, replace(cached, stamp=file_stamp(path.stat())))
    hold_fresh_clock(monkeypatch, path)
    expected = LegacyMessageReader(store).after_report(tid, after_message_id=target, limit=1)
    assert expected[1]
    assert store.messages.after_report(tid, after_message_id=target, limit=1) == expected


@pytest.mark.parametrize("change", ["missing_anchor", "bad_prefix"])
def test_recent_audit_hint_cannot_hide_original_corruption(tmp_path, monkeypatch, change):
    root = tmp_path / "audit"
    paths, rows = make_audit(root)
    settle_files(paths)
    target = rows[7]["event_id"]
    _collect_audit(root, after_event_id=target, limit=3, max_chars=20_000)
    key = cursor_key(root, target)
    cached = curator_audit_cursor._AUDIT_CURSORS.get(key)
    assert cached is not None
    path = paths[1 if change == "missing_anchor" else 0]
    raw = path.read_bytes()
    path.write_bytes(raw.replace(target.encode(), b"other-1-3") if change == "missing_anchor" else b"!" + raw[1:])
    # 陈旧hint携带文件系统的真实当前指纹，独立控制的是缓存输入和时间年龄，不替换stat或损坏诊断。
    if change == "missing_anchor":
        cached = replace(cached, stamp=file_stamp(path.stat()))
    else:
        cached = replace(cached, prefix=tuple((name, file_stamp(path.stat()) if name == path.name else stamp)
                                             for name, stamp in cached.prefix))
    curator_audit_cursor._AUDIT_CURSORS.put(key, cached)
    hold_fresh_clock(monkeypatch, path)
    expected = legacy_collect_audit(root, after_event_id=target, limit=3, max_chars=20_000)
    assert expected[1]
    assert _collect_audit(root, after_event_id=target, limit=3, max_chars=20_000) == expected


# LLM: 每例隔离进程内hint，真实Store和文件保持原实现；不触及正式owner或修改stat/fstat。
# 函数用途: 为初始空游标的正反例提供空线程和一条可用原格式样本。
@pytest.fixture
def initial_cursor_case(tmp_path, monkeypatch):
    cache = CursorCache(message_cursor._MESSAGE_CURSOR_CACHE_MAX_COUNT)
    monkeypatch.setattr(message_cursor, '_MESSAGE_CURSORS', cache)
    store, tid, rows = make_messages(tmp_path, count=1)
    path = store.storage.message_path(tid)
    path.write_bytes(b'')
    return SimpleNamespace(cache=cache, store=store, tid=tid, row=rows[0], path=path)


def test_slice3_message_cursor_capacity_retains_6212_thread_keys(tmp_path):
    assert message_cursor._MESSAGE_CURSOR_CACHE_MAX_COUNT == 32768
    assert message_cursor._MESSAGE_CURSORS.limit == 32768
    cache = CursorCache(message_cursor._MESSAGE_CURSOR_CACHE_MAX_COUNT)
    path = tmp_path / 'sample.jsonl'
    path.write_bytes(b'')
    hint = message_cursor.MessageCursor(file_stamp(path.stat()), 0, 0,
        bytes(message_cursor._MESSAGE_CURSOR_FILTER_BYTES), empty_tail=True)
    # 仅验证LRU容量，不把内存键样本说成6212份真实文件扫描。
    keys = [cursor_key(tmp_path / f'thread-{index}.jsonl', '') for index in range(6212)]
    for key in keys:
        cache.put(key, hint)
    for _ in range(2):
        assert all(cache.get(key) is hint for key in keys)
    assert len(cache._items) == 6212


@pytest.mark.parametrize('target', ['', ' ', '\t'])
@pytest.mark.parametrize('kind', ['empty', 'display', 'blank_lines'])
def test_slice3_initial_empty_cursor_has_zero_open_steady_state(initial_cursor_case, monkeypatch, target, kind):
    case = initial_cursor_case
    if kind == 'display':
        case.path.write_bytes(encode_row(replace(case.row, role=DISPLAY_CHECKPOINT_ROLE, message_id='').to_dict()))
    elif kind == 'blank_lines':
        case.path.write_bytes(b'\n \n\t\n')
    settle_files([case.path])
    counts = observe_reads(monkeypatch, [case.path])
    assert case.store.messages.after_report(case.tid, after_message_id=target) == ([], [])
    assert counts.opens == [str(case.path)]
    counts.clear()
    for _ in range(3):
        assert case.store.messages.after_report(case.tid, after_message_id=target) == ([], [])
    assert counts.opens == [] and counts.bytes == 0
    cached = case.cache.get(cursor_key(case.path, ''))
    assert cached.empty_tail and cached.start == 0 and cached.end == case.path.stat().st_size
    assert cached.stamp == file_stamp(case.path.stat())


def test_slice3_blank_message_id_never_saves_a_tail_position(initial_cursor_case):
    case = initial_cursor_case
    row = replace(case.row, message_id='').to_dict()
    case.path.write_bytes(encode_row(row))
    settle_files([case.path])
    assert message_cursor.read_after_rows(case.path, '', 10) == ([row], [])
    assert case.cache.get(cursor_key(case.path, '')) is None


@pytest.mark.parametrize('target', ['', ' ', '\t'])
def test_slice3_explicit_blank_offset_is_not_an_empty_tail(initial_cursor_case, target):
    case = initial_cursor_case
    rows = [case.row.to_dict(), replace(case.row, message_id='').to_dict()]
    case.path.write_bytes(b''.join(encode_row(row) for row in rows))
    settle_files([case.path])
    assert case.store.messages.byte_offset_after(case.tid, '') == case.path.stat().st_size
    cached = case.cache.get(cursor_key(case.path, ''))
    assert cached is not None and not cached.empty_tail
    assert not message_cursor._unchanged_end(case.path, target)
    assert message_cursor.read_after_rows(case.path, target, 10) == (rows, [])


@pytest.mark.parametrize('limit', [0, -1])
@pytest.mark.parametrize('primed', [False, True])
def test_slice3_nonpositive_initial_limit_neither_skips_nor_certifies_scan(initial_cursor_case, monkeypatch, limit, primed):
    case = initial_cursor_case
    settle_files([case.path])
    key = cursor_key(case.path, '')
    if primed:
        case.cache.put(key, message_cursor.MessageCursor(file_stamp(case.path.stat()), 0, 0,
            bytes(message_cursor._MESSAGE_CURSOR_FILTER_BYTES), empty_tail=True))
    previous = case.cache.get(key)
    counts = observe_reads(monkeypatch, [case.path])
    assert message_cursor.read_after_rows(case.path, '', limit) == ([], [])
    assert counts.opens == [str(case.path)] and case.cache.get(key) is previous
    row = case.row.to_dict()
    case.path.write_bytes(encode_row(row))
    settle_files([case.path])
    assert message_cursor.read_after_rows(case.path, '', limit) == ([], [])
    assert case.cache.get(key) is previous
    assert message_cursor.read_after_rows(case.path, '', 10) == ([row], [])


@pytest.mark.parametrize('raw', [b'{broken\n', b'\xff\n'])
def test_slice3_initial_corruption_never_becomes_cached_empty(initial_cursor_case, monkeypatch, raw):
    case = initial_cursor_case
    case.path.write_bytes(raw)
    settle_files([case.path])
    expected = LegacyMessageReader(case.store).after_report(case.tid, after_message_id='')
    assert expected[1]
    counts = observe_reads(monkeypatch, [case.path])
    for _ in range(2):
        assert case.store.messages.after_report(case.tid, after_message_id='') == expected
    assert counts.opens == [str(case.path)] * 2
    assert case.cache.get(cursor_key(case.path, '')) is None


@pytest.mark.parametrize('stale_hint', [False, True])
def test_slice3_fresh_initial_empty_never_trusts_or_saves_hint(initial_cursor_case, monkeypatch, stale_hint):
    case = initial_cursor_case
    hold_fresh_clock(monkeypatch, case.path)
    key = cursor_key(case.path, '')
    if stale_hint:
        case.cache.put(key, message_cursor.MessageCursor(file_stamp(case.path.stat()), 0, 0,
            bytes(message_cursor._MESSAGE_CURSOR_FILTER_BYTES), empty_tail=True))
    previous = case.cache.get(key)
    counts = observe_reads(monkeypatch, [case.path])
    for _ in range(2):
        assert message_cursor.read_after_rows(case.path, '', 10) == ([], [])
    assert counts.opens == [str(case.path)] * 2 and case.cache.get(key) is previous


@pytest.mark.parametrize('change', ['append', 'replace', 'rewrite', 'truncate', 'corrupt'])
def test_slice3_initial_empty_rechecks_changed_stamp(initial_cursor_case, monkeypatch, change):
    case = initial_cursor_case
    display = encode_row(replace(case.row, role=DISPLAY_CHECKPOINT_ROLE).to_dict())
    case.path.write_bytes(display)
    settle_files([case.path])
    assert case.store.messages.after_report(case.tid) == ([], [])
    cached = case.cache.get(cursor_key(case.path, ''))
    assert cached is not None and cached.empty_tail
    message = encode_row(case.row.to_dict())
    if change == 'replace':
        replacement = case.path.with_suffix('.replacement')
        replacement.write_bytes(message)
        os.replace(replacement, case.path)
    else:
        case.path.write_bytes({'append': display + message, 'rewrite': message,
            'truncate': b'', 'corrupt': b'{broken\n'}[change])
    settle_files([case.path])
    assert file_stamp(case.path.stat()) != cached.stamp
    expected = LegacyMessageReader(case.store).after_report(case.tid)
    counts = observe_reads(monkeypatch, [case.path])
    assert case.store.messages.after_report(case.tid) == expected
    assert counts.opens == [str(case.path)]


def test_slice3_nonblank_cursor_keeps_exact_whitespace_in_id(initial_cursor_case):
    case = initial_cursor_case
    rows = [replace(case.row, message_id=target).to_dict() for target in (' spaced ', 'spaced')]
    case.path.write_bytes(b''.join(encode_row(row) for row in rows))
    settle_files([case.path])
    assert case.store.messages.after_report(case.tid, after_message_id='spaced') == ([], [])
    expected = LegacyMessageReader(case.store).after_report(case.tid, after_message_id=' spaced ')
    assert expected[0] and case.store.messages.after_report(case.tid, after_message_id=' spaced ') == expected
