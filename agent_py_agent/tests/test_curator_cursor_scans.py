# LLM: 原API与冻结baseline逐项比较；变异正确性保留fresh真实stat，性能夹具只对明确稳定文件计量，不改持久游标/权限。
# 模块用途: 锁定E11c等价语义与改写失效，保留共享信任窗口外零打开和有界读尾断言。
from __future__ import annotations

import errno
import inspect
import os
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_py_agent.agent.conversation.display_checkpoint import DISPLAY_CHECKPOINT_ROLE
from agent_py_agent.agent.memory_store import curator_audit_cursor
from agent_py_agent.agent.memory_store.curator_inputs import (
    CuratorMessageSource,
    _collect_audit,
    _collect_messages,
)
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


def test_unchanged_thread_at_cursor_end_has_zero_opens(tmp_path, monkeypatch):
    store, tid, rows = make_messages(tmp_path, count=200, content_chars=2048)
    cursor = rows[-1].message_id
    settle_files([store.storage.message_path(tid)])
    assert store.messages.after_report(tid, after_message_id=cursor) == ([], [])
    counts = observe_reads(monkeypatch, [store.storage.message_path(tid)])
    for _ in range(2):
        assert _collect_messages(
            source=CuratorMessageSource(conversation_store=store),
            cursors={tid: cursor}, limit=20, max_chars=10_000,
        ) == ([], [])
    assert counts.opens == []
    assert counts.bytes == 0


def test_appended_thread_reads_only_anchor_and_tail(tmp_path, monkeypatch):
    store, tid, rows = make_messages(tmp_path, count=200, content_chars=2048)
    cursor = rows[-1].message_id
    settle_files([store.storage.message_path(tid)])
    assert store.messages.after_report(tid, after_message_id=cursor) == ([], [])
    path = store.storage.message_path(tid)
    new = replace(rows[-1], message_id="new-message", content="新消息")
    path.write_bytes(path.read_bytes() + encode_row(new.to_dict()))
    settle_files([path])
    counts = observe_reads(monkeypatch, [path])
    assert store.messages.after_report(tid, after_message_id=cursor) == ([new], [])
    # f74d1161e 起冷热读取器打开时按固定 64KB 倒读块确认完整行末尾（store_io.complete_jsonl_end），与历史长度无关。
    eof_probe = min(path.stat().st_size, 65536)
    assert path.stat().st_size > 4 * eof_probe
    assert counts.bytes <= len(encode_row(rows[-1].to_dict())) + len(encode_row(new.to_dict())) + eof_probe


def test_warm_audit_skips_old_shards_and_cursor_prefix(tmp_path, monkeypatch):
    root = tmp_path / "audit"
    paths, records = make_audit(root, days=4, rows_per_day=100)
    cursor = records[299]["event_id"]
    settle_files(paths)
    expected = legacy_collect_audit(root, after_event_id=cursor, limit=2, max_chars=20_000)
    assert _collect_audit(root, after_event_id=cursor, limit=2, max_chars=20_000) == expected
    counts = observe_reads(monkeypatch, paths)
    assert _collect_audit(root, after_event_id=cursor, limit=2, max_chars=20_000) == expected
    assert all(str(path) not in counts.opens for path in paths[:3])
    assert counts.bytes < paths[-1].stat().st_size + len(encode_row(records[299]))


def test_advanced_message_cursor_does_not_rescan_prefix(tmp_path, monkeypatch):
    store, tid, rows = make_messages(tmp_path, count=200, content_chars=2048)
    settle_files([store.storage.message_path(tid)])
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
    if not at_end and limit < 3 and case in {"bad_after", "no_id", "unicode"}:
        # 预算停在 records[6] 时不预扫 records[7] 之后的尾行；翻到尾页 records[7] 后与旧基线对齐报出尾行错。
        page_selected, page_errors = _collect_audit(root, after_event_id=cursor, limit=limit, max_chars=max_chars)
        assert [item.event_id for item in page_selected] == [records[6]["event_id"]]
        assert page_errors == []
        tail_cursor = records[7]["event_id"]
        expected_tail = legacy_collect_audit(root, after_event_id=tail_cursor, limit=limit, max_chars=max_chars)
        assert _collect_audit(root, after_event_id=tail_cursor, limit=limit, max_chars=max_chars) == expected_tail
        return
    expected = legacy_collect_audit(root, after_event_id=cursor, limit=limit, max_chars=max_chars)
    assert _collect_audit(root, after_event_id=cursor, limit=limit, max_chars=max_chars) == expected


def _scan_fd_count() -> int | None:
    for probe in (Path("/dev/fd"), Path("/proc/self/fd")):
        if probe.is_dir():
            return len(os.listdir(probe))
    return None


def test_r4_two_shard_paging_early_stop_and_next_page_malformed_diagnostics(tmp_path, monkeypatch):
    root = tmp_path / "audit"
    root.mkdir()
    first = root / "1970-01-01.jsonl"
    second = root / "1970-01-02.jsonl"
    shard1_rows = [
        {"event_id": "event-a", "event_type": "tool_call", "status": "ok", "preview": "a"},
        {"event_id": "event-b", "event_type": "tool_call", "status": "ok", "preview": "b"},
        {"event_id": "event-c", "event_type": "tool_call", "status": "ok", "preview": "c"},
    ]
    shard2_rows = [
        {"event_id": "event-d", "event_type": "tool_call", "status": "ok", "preview": "d"},
        {"event_id": "event-e", "event_type": "tool_call", "status": "ok", "preview": "e"},
    ]
    first.write_bytes(b"".join(encode_row(row) for row in shard1_rows))
    second.write_bytes(b"".join(encode_row(row) for row in shard2_rows) + b"{}\nbroken\n[]\n")

    original_row = curator_audit_cursor.AuditRow
    constructions: list[int] = []

    def counted_row(*args, **kwargs):
        constructions.append(1)
        return original_row(*args, **kwargs)

    monkeypatch.setattr(curator_audit_cursor, "AuditRow", counted_row)

    original_stat = os.stat
    stat_faults = 0

    def fault_stat(target, *args, **kwargs):
        nonlocal stat_faults
        if str(target) == str(first):
            frame = inspect.currentframe()
            caller = frame.f_back.f_code.co_name if frame and frame.f_back else ""
            if caller in {"isfile", "is_file"}:
                stat_faults += 1
                raise OSError(errno.EIO, "simulated I/O error")
        return original_stat(target, *args, **kwargs)

    fd_before = _scan_fd_count()
    with ExitStack() as stack:
        for name in ("supports_dir_fd", "supports_follow_symlinks", "supports_fd"):
            current = getattr(os, name)
            if original_stat in current:
                stack.enter_context(patch.object(os, name, current | {fault_stat}))
        stack.enter_context(patch.object(os, "stat", fault_stat))

        page1, err1 = _collect_audit(root, after_event_id="", limit=1, max_chars=20_000)
        assert [row.event_id for row in page1] == ["event-a"]
        assert err1 == []
        # 第一页达预算立即停止，不预构造后续事件行或提前报出第二分片尾行错。
        assert len(constructions) <= 1

        collected_ids = ["event-a"]
        cursor = "event-a"
        for _ in range(4):
            page, err = _collect_audit(root, after_event_id=cursor, limit=1, max_chars=20_000)
            assert len(page) == 1 and err == []
            cursor = page[0].event_id
            collected_ids.append(cursor)

        assert collected_ids == ["event-a", "event-b", "event-c", "event-d", "event-e"]
        tail_rows, tail_errors = _collect_audit(root, after_event_id="event-e", limit=1, max_chars=20_000)
        assert tail_rows == []
        assert len(tail_errors) == 3
        assert all(item["error_code"] == "AUDIT_READ_FAILED" for item in tail_errors)

    assert stat_faults == 0
    assert (tail_rows, tail_errors) == legacy_collect_audit(
        root, after_event_id="event-e", limit=1, max_chars=20_000
    )
    if fd_before is not None:
        assert _scan_fd_count() == fd_before
