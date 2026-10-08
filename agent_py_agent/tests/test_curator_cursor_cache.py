# LLM: 合成owner和原API验证定位hint的隔离/失效，任何缓存淘汰只影响扫描量，不影响选择或错误。
# 模块用途: 覆盖E11c多线程预算、首ID保守性、四个变异靶点及只读边界。
from __future__ import annotations

import os
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation import message_cursor
from agent_py_agent.agent.conversation.display_checkpoint import DISPLAY_CHECKPOINT_ROLE
from agent_py_agent.agent.io.cursor_cache import CursorCache, cursor_key
from agent_py_agent.agent.memory_store import curator_audit_cursor
from agent_py_agent.agent.memory_store.curator_inputs import _collect_audit, _collect_messages
from agent_py_agent.agent.runtime_errors import DataCorruptionError
from agent_py_agent.tests.support.curator_scan_cases import (
    encode_row,
    make_audit,
    make_messages,
    observe_reads,
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
    _, brows = make_audit(br, days=1)
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
    expected = _collect_messages(conversation_store=legacy, cursors=cursors, limit=limit, max_chars=max_chars)
    assert _collect_messages(conversation_store=store, cursors=cursors, limit=limit, max_chars=max_chars) == expected
    assert _collect_messages(conversation_store=store, cursors=cursors, limit=limit, max_chars=max_chars) == expected
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
