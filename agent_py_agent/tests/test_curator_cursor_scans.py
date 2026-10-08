# LLM: 原API和冻结baseline逐项比较，只使用合成owner；不改变持久游标/权限，扫描量来自真实文件观察。
# 模块用途: 锁定E11c消息/审计缓存的等价语义、替换失效和稳态零整扫。
from __future__ import annotations

import os
from dataclasses import replace

import pytest

from agent_py_agent.agent.conversation.display_checkpoint import DISPLAY_CHECKPOINT_ROLE
from agent_py_agent.agent.memory_store.curator_inputs import _collect_audit, _collect_messages
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


def test_unchanged_thread_at_cursor_end_has_zero_opens(tmp_path, monkeypatch):
    store, tid, rows = make_messages(tmp_path, count=200, content_chars=2048)
    cursor = rows[-1].message_id
    assert store.messages.after_report(tid, after_message_id=cursor) == ([], [])
    counts = observe_reads(monkeypatch, [store.storage.message_path(tid)])
    for _ in range(2):
        assert _collect_messages(conversation_store=store, cursors={tid: cursor}, limit=20, max_chars=10_000) == ([], [])
    assert counts.opens == []
    assert counts.bytes == 0


def test_appended_thread_reads_only_anchor_and_tail(tmp_path, monkeypatch):
    store, tid, rows = make_messages(tmp_path, count=200, content_chars=2048)
    cursor = rows[-1].message_id
    assert store.messages.after_report(tid, after_message_id=cursor) == ([], [])
    path = store.storage.message_path(tid)
    new = replace(rows[-1], message_id="new-message", content="新消息")
    path.write_bytes(path.read_bytes() + encode_row(new.to_dict()))
    counts = observe_reads(monkeypatch, [path])
    assert store.messages.after_report(tid, after_message_id=cursor) == ([new], [])
    assert counts.bytes <= len(encode_row(rows[-1].to_dict())) + len(encode_row(new.to_dict()))


def test_warm_audit_skips_old_shards_and_cursor_prefix(tmp_path, monkeypatch):
    root = tmp_path / "audit"
    paths, records = make_audit(root, days=4, rows_per_day=100)
    cursor = records[299]["event_id"]
    expected = legacy_collect_audit(root, after_event_id=cursor, limit=2, max_chars=20_000)
    assert _collect_audit(root, after_event_id=cursor, limit=2, max_chars=20_000) == expected
    counts = observe_reads(monkeypatch, paths)
    assert _collect_audit(root, after_event_id=cursor, limit=2, max_chars=20_000) == expected
    assert all(str(path) not in counts.opens for path in paths[:3])
    assert counts.bytes < paths[-1].stat().st_size + len(encode_row(records[299]))


def test_advanced_message_cursor_does_not_rescan_prefix(tmp_path, monkeypatch):
    store, tid, rows = make_messages(tmp_path, count=200, content_chars=2048)
    selected, errors = store.messages.after_report(tid, after_message_id=rows[-2].message_id)
    assert selected == rows[-1:] and not errors
    counts = observe_reads(monkeypatch, [store.storage.message_path(tid)])
    assert store.messages.after_report(tid, after_message_id=selected[-1].message_id) == ([], [])
    assert counts.opens == []


def test_message_duplicate_id_keeps_original_first_match(tmp_path):
    store, tid, rows = make_messages(tmp_path, count=6)
    path = store.storage.message_path(tid)
    duplicate = replace(rows[-1], message_id=rows[0].message_id, content="后来的重复ID")
    with path.open("ab") as handle:
        handle.write(encode_row(duplicate.to_dict()))
    store.messages.after_report(tid, after_message_id=rows[2].message_id)
    assert store.messages.after_report(tid, after_message_id=rows[0].message_id) == LegacyMessageReader(store).after_report(tid, after_message_id=rows[0].message_id)


def test_message_blank_prefix_cannot_be_promoted_to_safe_cursor(tmp_path):
    store, tid, rows = make_messages(tmp_path, count=3)
    path = store.storage.message_path(tid)
    path.write_bytes(b"\n" + path.read_bytes())
    store.messages.after_report(tid)
    assert store.messages.after_report(tid, after_message_id=rows[-1].message_id) == LegacyMessageReader(store).after_report(tid, after_message_id=rows[-1].message_id)


@pytest.mark.parametrize("case", ["unchanged", "append", "replace", "truncate", "same_size", "missing", "bad_before", "bad_after", "display", "blank", "unicode"])
@pytest.mark.parametrize("at_end", [False, True])
@pytest.mark.parametrize("limit", [1, 3, 0])
def test_messages_equal_frozen_baseline_after_file_changes(tmp_path, case, at_end, limit):
    store, tid, rows = make_messages(tmp_path, count=6)
    path = store.storage.message_path(tid)
    cursor = rows[-1 if at_end else 2].message_id
    store.messages.after_report(tid, after_message_id=cursor, limit=limit)
    raw = path.read_bytes()
    display = encode_row(replace(rows[-1], message_id="display", role=DISPLAY_CHECKPOINT_ROLE).to_dict())
    following = encode_row(replace(rows[-1], message_id="after-display").to_dict())
    changes = {"unchanged": raw, "replace": raw, "append": raw + encode_row(replace(rows[-1], message_id="tail").to_dict()),
               "same_size": raw.replace(cursor.encode(), b"msg-xxxxx"), "missing": raw.replace(cursor.encode(), b"msg-xxxxx"),
               "truncate": encode_row(rows[0].to_dict()), "bad_before": b"broken\n" + raw, "bad_after": raw + b"broken\n",
               "display": raw + display + following, "blank": b"\n" + raw, "unicode": raw + b"\xff\n"}
    raw = changes[case]
    if case == "replace":
        replacement = path.with_suffix(".replacement")
        replacement.write_bytes(raw.replace(cursor.encode(), b"msg-xxxxx"))
        os.replace(replacement, path)
    elif case != "unchanged":
        path.write_bytes(raw)
    old = LegacyMessageReader(store).after_report(tid, after_message_id=cursor, limit=limit)
    assert store.messages.after_report(tid, after_message_id=cursor, limit=limit) == old


@pytest.mark.parametrize("case", ["unchanged", "append", "replace", "truncate", "same_size", "missing", "bad_before", "bad_after", "no_id", "unicode", "blank"])
@pytest.mark.parametrize("at_end", [False, True])
@pytest.mark.parametrize("budget", [(1, 1), (3, 400), (0, 10_000)])
def test_audit_equals_frozen_baseline_including_old_errors(tmp_path, case, at_end, budget):
    root = tmp_path / "audit"
    paths, records = make_audit(root)
    cursor = records[7 if at_end else 5]["event_id"]
    limit, max_chars = budget
    _collect_audit(root, after_event_id=cursor, limit=limit, max_chars=max_chars)
    path = paths[1]
    raw = path.read_bytes()
    changed = raw.replace(cursor.encode(), b"other-1-1" if not at_end else b"other-1-3")
    changes = {"unchanged": raw, "append": raw + encode_row({"event_id": "tail"}), "replace": changed,
               "same_size": changed, "missing": changed, "truncate": encode_row(records[4]), "bad_before": raw,
               "bad_after": raw + b"broken\n[]\n", "no_id": raw + b"{}\n", "unicode": raw + b"\xff\n", "blank": b"\n" + raw}
    raw = changes[case]
    if case == "bad_before":
        paths[0].write_bytes(paths[0].read_bytes() + b"broken\n")
    if case == "replace":
        replacement = path.with_suffix(".replacement")
        replacement.write_bytes(raw)
        os.replace(replacement, path)
    elif case != "unchanged":
        path.write_bytes(raw)
    expected = legacy_collect_audit(root, after_event_id=cursor, limit=limit, max_chars=max_chars)
    assert _collect_audit(root, after_event_id=cursor, limit=limit, max_chars=max_chars) == expected
