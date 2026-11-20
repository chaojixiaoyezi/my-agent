import errno
import json
import os
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.collaboration import external_mailbox_adapter as mailbox
from agent_py_agent.agent.collaboration.external_mailbox_adapter import (
    ExternalMailboxAdapter,
    create_external_mailbox_layout,
)
from agent_py_agent.agent.memory_store.retention_scan_budget import RETENTION_SCAN_SLICE_ENTRIES


def _mailbox(tmp_path, *, window=262144):
    layout = create_external_mailbox_layout(tmp_path, "worker")
    adapter = ExternalMailboxAdapter([tmp_path], max_file_bytes=window)
    calls = []
    service = SimpleNamespace(
        store=SimpleNamespace(
            list_all_handoff_items=lambda **_: [],
            load_handoff_item=lambda *_: SimpleNamespace(responsible_actor=layout.actor_id),
        ),
        record_ack=lambda **values: calls.append(values["ack_id"]),
    )
    return adapter, layout, service, calls


def _message(message_id, **fields):
    record = {
        "action": "ack",
        "case_id": "case",
        "item_id": "item",
        "message_id": message_id,
        **fields,
    }
    return (json.dumps(record, ensure_ascii=False) + "\n").encode()


def _cursor(layout):
    return json.loads((layout.actor_inbox_path.parent / ".coordinator_cursor.json").read_bytes())


def test_large_inbox_is_delivered_once_across_batches(tmp_path):
    adapter, layout, service, calls = _mailbox(tmp_path)
    expected = [f"message-{i}" for i in range(2048)]
    raw = b"".join(_message(ident, payload={"body": "x" * 1024}) for ident in expected)
    assert len(raw) >= 2 * 1024 * 1024
    layout.actor_inbox_path.write_bytes(raw)
    for _ in range(16):
        report = adapter.sync_external_mailbox(service, layout)
        assert report["quarantined"] == report["errors"] == []
        assert len(report["acked_item_ids"]) < len(expected)
        if len(calls) == len(expected):
            break
    assert calls == expected
    assert _cursor(layout)["offset"] == len(raw)
    assert adapter.sync_external_mailbox(service, layout)["acked_item_ids"] == []
    assert calls == expected


def test_outbox_appends_preserve_inode_and_prefix(tmp_path):
    adapter, layout, _, _ = _mailbox(tmp_path)
    outbox = layout.coordinator_outbox_path
    adapter.write_coordinator_message(layout, {"action": "ping", "message_id": "first"})
    inode, prefix = outbox.stat().st_ino, outbox.read_bytes()
    adapter.write_coordinator_message(layout, {"action": "ping"})
    assert outbox.stat().st_ino == inode
    assert outbox.read_bytes().startswith(prefix)
    first, second = map(json.loads, outbox.read_bytes().splitlines())
    assert first["message_id"] == "first"
    assert second["message_id"] and second["message_id"] != "first"


def test_duplicate_ids_survive_batches_and_reopen_until_generation_changes(tmp_path):
    adapter, layout, service, calls = _mailbox(tmp_path, window=256)
    inbox = layout.actor_inbox_path
    raw = _message("same", payload={"padding": "x" * 120})
    inbox.write_bytes(raw)
    adapter.sync_external_mailbox(service, layout)
    first = _cursor(layout)
    assert first == {"v": 2, "offset": len(raw), "st_dev": inbox.stat().st_dev,
                     "st_ino": inbox.stat().st_ino, "generation": 0, "receipts": 2}
    with inbox.open("ab") as stream:
        stream.write(_message("same", payload={"different": True}))
    reopened = ExternalMailboxAdapter([tmp_path], max_file_bytes=256)
    assert reopened.sync_external_mailbox(service, layout)["acked_item_ids"] == []
    assert calls == ["same"]

    # 新文件足够长，单靠长度无法判断轮换。
    replacement = inbox.with_suffix(".new")
    replacement.write_bytes(raw + raw)
    replacement.replace(inbox)
    reopened.sync_external_mailbox(service, layout)
    assert (_cursor(layout)["st_ino"], _cursor(layout)["generation"]) == (inbox.stat().st_ino, 1)
    reopened.sync_external_mailbox(service, layout)
    assert calls == ["same", "same"]

    inode = inbox.stat().st_ino
    inbox.write_bytes(_message("same"))
    adapter.sync_external_mailbox(service, layout)
    assert inbox.stat().st_ino == inode
    assert _cursor(layout)["generation"] == 2
    assert calls == ["same", "same", "same"]


def test_malformed_lines_have_byte_ranges_and_partial_record_waits(tmp_path):
    adapter, layout, service, calls = _mailbox(tmp_path)
    bad = b'{invalid}\n'
    invalid_utf8 = b'{"message_id":"\xff"}\n'
    complete = _message("hello", payload={"body": "你好"})
    tail = _message("tail", payload={"body": "后续"})
    inbox = layout.actor_inbox_path
    split_at = tail.index("后".encode()) + 1
    prefix = complete + bad + invalid_utf8
    inbox.write_bytes(prefix + tail[:split_at])
    report = adapter.sync_external_mailbox(service, layout)
    assert [(q["byte_start"], q["byte_end"]) for q in report["quarantined"]] == [
        (len(complete), len(complete + bad)), (len(complete + bad), len(prefix)),
    ]
    assert _cursor(layout)["offset"] == len(prefix)
    assert calls == ["hello"]
    with inbox.open("ab") as stream:
        stream.write(tail[split_at:])
    assert adapter.sync_external_mailbox(service, layout)["quarantined"] == []
    assert calls == ["hello", "tail"]


def test_oversized_line_is_reported_once_after_its_terminator(tmp_path):
    adapter, layout, service, calls = _mailbox(tmp_path, window=128)
    bad = b"x" * 300 + b"\n"
    raw = bad + _message("tail")
    layout.actor_inbox_path.write_bytes(raw)
    quarantined = []
    for _ in range(5):
        quarantined.extend(adapter.sync_external_mailbox(service, layout)["quarantined"])
    entry, = quarantined
    assert (entry["byte_start"], entry["byte_end"]) == (0, len(bad))
    assert calls == ["tail"]
    assert _cursor(layout)["offset"] == len(raw)
    assert "discard_start" not in _cursor(layout)
    assert layout.actor_inbox_path.read_bytes() == raw


def test_legacy_cursor_counts_lines_before_reading_new_records(tmp_path):
    adapter, layout, service, calls = _mailbox(tmp_path, window=128)
    inbox = layout.actor_inbox_path
    old = b"{unreadable}\n" + _message("previous")
    new = _message("next")
    inbox.write_bytes(old + new)
    (inbox.parent / ".coordinator_cursor.json").write_text(
        json.dumps({"processed_lines": 2, "file_size": len(old)}), encoding="utf-8",
    )
    assert adapter.sync_external_mailbox(service, layout)["quarantined"] == []
    assert _cursor(layout)["offset"] == len(old + new)
    with inbox.open("ab") as stream:
        stream.write(_message("last"))
    adapter.sync_external_mailbox(service, layout)
    assert calls == ["next", "last"]


def test_short_append_restores_prefix_and_reports_io_error(tmp_path, monkeypatch):
    adapter, layout, _, _ = _mailbox(tmp_path)
    outbox = layout.coordinator_outbox_path
    adapter.write_coordinator_message(layout, {"message_id": "previous"})
    identity, prefix = outbox.stat(), outbox.read_bytes()
    write = os.write
    writes = []

    def short_write(fd, data):
        if os.path.samestat(os.fstat(fd), identity):
            writes.append(data)
            return write(fd, data[:len(data) // 2])
        return write(fd, data)

    with monkeypatch.context() as patch:
        patch.setattr(os, "write", short_write)
        with pytest.raises(OSError, match="Mailbox append wrote") as error:
            adapter.write_coordinator_message(layout, {"message_id": "retry"})
    assert error.value.errno == errno.EIO
    assert len(writes) == 1
    assert outbox.read_bytes() == prefix
    adapter.write_coordinator_message(layout, {"message_id": "retry"})
    assert outbox.read_bytes() == prefix + b'{"message_id": "retry"}\n'


def test_concurrent_syncs_share_one_mailbox_cursor(tmp_path):
    adapter, layout, service, calls = _mailbox(tmp_path)
    layout.actor_inbox_path.write_bytes(_message("once") * 3)
    with ThreadPoolExecutor(max_workers=4) as executor:
        reports = list(executor.map(lambda _: adapter.sync_external_mailbox(service, layout), range(8)))
    assert calls == ["once"]
    assert sum(len(report["acked_item_ids"]) for report in reports) == 1


def _seen(layout):
    return layout.actor_inbox_path.parent / ".coordinator_seen"


def _receipt_files(root):
    return sorted(path.relative_to(root).parts for path in root.rglob("*") if path.is_file())


def _rotate(inbox, raw):
    replacement = inbox.with_suffix(".new")
    replacement.write_bytes(raw)
    replacement.replace(inbox)


def test_rotation_retires_previous_generation_receipts_in_bounded_slices(tmp_path, monkeypatch):
    adapter, layout, service, calls = _mailbox(tmp_path)
    inbox = layout.actor_inbox_path
    retired_count = RETENTION_SCAN_SLICE_ENTRIES * 2 + 7
    inbox.write_bytes(b"".join(_message(f"old-{i}") for i in range(retired_count)))
    adapter.sync_external_mailbox(service, layout)
    old_namespace = mailbox._receipt_namespace(_cursor(layout))
    assert {parts[0] for parts in _receipt_files(_seen(layout))} == {old_namespace}
    assert len(_receipt_files(_seen(layout))) == retired_count

    writes = []
    original = mailbox.write_text_atomic_beneath

    def record(root, parts, text, *, durable):
        writes.append((json.loads(text).get("generation"), durable))
        return original(root, parts, text, durable=durable)

    monkeypatch.setattr(mailbox, "write_text_atomic_beneath", record)
    _rotate(inbox, _message("old-0") + _message("fresh"))
    adapter.sync_external_mailbox(service, layout)
    assert writes[0] == (1, True)  # 代次切换先持久化，再退役旧命名空间
    active = mailbox._receipt_namespace(_cursor(layout))
    assert calls[-2:] == ["old-0", "fresh"]
    remaining = [parts for parts in _receipt_files(_seen(layout)) if parts[0] == old_namespace]
    # 单片只做既有留存预算内的工作（文件与空桶都计数），剩余部分留给后续同步。
    assert 0 < retired_count - len(remaining) <= RETENTION_SCAN_SLICE_ENTRIES
    assert _cursor(layout)["retire"] is True

    for _ in range(8):
        # 每次同步用新实例，模拟进程重启后仅凭游标和剩余文件续做。
        ExternalMailboxAdapter([tmp_path]).sync_external_mailbox(service, layout)
        if "retire" not in _cursor(layout):
            break
    assert "retire" not in _cursor(layout)
    assert sorted(item.name for item in _seen(layout).iterdir()) == [active]
    assert {parts[0] for parts in _receipt_files(_seen(layout))} == {active}
    assert all(durable is False for _, durable in writes[1:])
    # 活跃代次的精确去重不受退役影响。
    with inbox.open("ab") as stream:
        stream.write(_message("fresh"))
    assert adapter.sync_external_mailbox(service, layout)["acked_item_ids"] == []
    assert calls.count("fresh") == 1


def test_pre_upgrade_receipts_deduplicate_until_their_generation_is_inactive(tmp_path):
    adapter, layout, service, calls = _mailbox(tmp_path)
    inbox = layout.actor_inbox_path
    raw = _message("before-upgrade")
    inbox.write_bytes(raw)
    info = inbox.stat()
    pre_upgrade = {"v": 2, "offset": len(raw), "st_dev": info.st_dev, "st_ino": info.st_ino, "generation": 0}
    (inbox.parent / ".coordinator_cursor.json").write_text(json.dumps(pre_upgrade), encoding="utf-8")
    receipt = mailbox._message_receipt(pre_upgrade, "before-upgrade")
    legacy = _seen(layout) / receipt[2] / receipt[3]
    legacy.parent.mkdir(parents=True)
    legacy.touch()

    with inbox.open("ab") as stream:
        stream.write(_message("before-upgrade") + _message("after-upgrade"))
    adapter.sync_external_mailbox(service, layout)
    assert calls == ["after-upgrade"]
    assert _cursor(layout)["legacy_ns"] == mailbox._receipt_namespace(pre_upgrade)
    assert "retire" not in _cursor(layout) and legacy.exists()

    _rotate(inbox, _message("before-upgrade"))
    adapter.sync_external_mailbox(service, layout)
    assert calls == ["after-upgrade", "before-upgrade"]
    assert "legacy_ns" not in _cursor(layout) and "retire" not in _cursor(layout)
    assert not legacy.parent.exists()
    assert {parts[0] for parts in _receipt_files(_seen(layout))} == {mailbox._receipt_namespace(_cursor(layout))}


def test_failed_transition_write_keeps_previous_generation_receipts(tmp_path, monkeypatch):
    adapter, layout, service, calls = _mailbox(tmp_path)
    inbox = layout.actor_inbox_path
    inbox.write_bytes(_message("first"))
    adapter.sync_external_mailbox(service, layout)
    before = _receipt_files(_seen(layout))
    _rotate(inbox, _message("second"))

    def fail(*_args, **_kwargs):
        raise OSError(errno.EIO, "simulated I/O error")

    with monkeypatch.context() as patch:
        patch.setattr(mailbox, "write_text_atomic_beneath", fail)
        report = adapter.sync_external_mailbox(service, layout)
    assert [entry["error_code"] for entry in report["quarantined"]] == ["MAILBOX_INBOX_IO_ERROR"]
    assert set(before) <= set(_receipt_files(_seen(layout)))
    assert _cursor(layout)["generation"] == 0
    adapter.sync_external_mailbox(service, layout)
    assert calls == ["first", "second"] and _cursor(layout)["generation"] == 1
    assert not set(before) & set(_receipt_files(_seen(layout)))
