"""外发索引：分片、期限、精确身份和重放的回归。"""
from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest

from agent_py_agent.agent.conversation.outbound_message_index import OutboundMessageIndex


def _entry(message_id="om_outbound", *, sent=1_800_000_000, **changes):
    return {"schema_version": "outbound_message.v1", "channel": "feishu",
            "channel_message_id": message_id, "chat_id": "chat-a", "thread_id": "thread-a",
            "conversation_message_id": "msg-a", "task_id": "task-a", "workflow_run_id": "workflow-run-a",
            "evidence_refs": ["artifact://report-a"], "receipt_id": message_id,
            "sent_at": datetime.fromtimestamp(sent, timezone.utc).isoformat(), **changes}


def test_receipt_survives_reopen_and_is_scoped_by_channel_and_chat(tmp_path):
    index = OutboundMessageIndex(tmp_path)
    row = _entry()
    assert index.record(row, now=1_800_000_001) == row
    reopened = OutboundMessageIndex(tmp_path)
    assert reopened.lookup("om_outbound", channel="feishu", chat_id="chat-a", now=1_800_000_002) == row
    assert reopened.lookup("om_outbound", channel="other", chat_id="chat-a", now=1_800_000_002) is None
    assert reopened.lookup("om_outbound", channel="feishu", chat_id="chat-b", now=1_800_000_002) is None
    assert OutboundMessageIndex(tmp_path / "other-owner").lookup("om_outbound", channel="feishu", chat_id="chat-a") is None
    path = tmp_path / "data/outbound_messages" / (hashlib.sha256(b"om_outbound").hexdigest()[:2] + ".json")
    assert path.is_file()


def test_lookup_is_readonly_and_expired_entries_are_not_reintroduced(tmp_path):
    index = OutboundMessageIndex(tmp_path)
    assert index.lookup("absent", channel="feishu", chat_id="chat-a", now=1_800_000_000) is None
    assert list(tmp_path.iterdir()) == []
    index.record(_entry(), now=1_800_000_000)
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert index.lookup("om_outbound", channel="feishu", chat_id="chat-a", now=1_800_000_000 + 30 * 86400) is None
    assert {p: p.read_bytes() for p in before} == before
    assert index.record(_entry(), now=1_800_000_000 + 31 * 86400) == _entry()
    assert index.lookup("om_outbound", channel="feishu", chat_id="chat-a", now=1_800_000_000 + 31 * 86400) is None


def test_duplicate_is_idempotent_but_identity_rebinding_is_rejected(tmp_path):
    index = OutboundMessageIndex(tmp_path)
    row = _entry()
    index.record(row, now=1_800_000_000)
    assert index.record(dict(row), now=1_800_000_000) == row
    with pytest.raises(ValueError):
        index.record(_entry(conversation_message_id="different-message"), now=1_800_000_000)
    assert index.lookup("om_outbound", channel="feishu", chat_id="chat-a", now=1_800_000_000) == row


def test_one_shard_is_bounded_and_drops_oldest_without_global_scan(tmp_path, monkeypatch):
    index = OutboundMessageIndex(tmp_path)
    row = _entry()
    prefix = hashlib.sha256(b"om_outbound").hexdigest()[:2]
    ids = []
    i = 0
    while len(ids) < 2000:
        value = f"om_shard_{i}"
        if hashlib.sha256(value.encode()).hexdigest().startswith(prefix):
            ids.append(value)
        i += 1
    path = tmp_path / "data/outbound_messages" / f"{prefix}.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"schema_version": "outbound_message_index.v1", "entries": [
        _entry(value, sent=1_800_000_000 + n) for n, value in enumerate(ids)]}))
    monkeypatch.setattr(type(tmp_path), "rglob", lambda *a, **k: pytest.fail("全历史枚举"))
    index.record(_entry(sent=1_800_002_001), now=1_800_002_001)
    persisted = json.loads(path.read_text())["entries"]
    assert len(persisted) == 2000
    assert index.lookup(ids[0], channel="feishu", chat_id="chat-a", now=1_800_002_001) is None
    assert index.lookup("om_outbound", channel="feishu", chat_id="chat-a", now=1_800_002_001) is not None
    assert row["conversation_message_id"] == "msg-a"


def test_concurrent_same_shard_updates_do_not_erase_neighbors(tmp_path):
    ids = [f"om_parallel_{i}" for i in range(40)]
    def record(value):
        return OutboundMessageIndex(tmp_path).record(_entry(value), now=1_800_000_001)
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert len(list(pool.map(record, ids))) == len(ids)
    index = OutboundMessageIndex(tmp_path)
    for value in ids:
        assert index.lookup(value, channel="feishu", chat_id="chat-a", now=1_800_000_002)["channel_message_id"] == value


@pytest.mark.parametrize("change", [{"channel_message_id": ""}, {"chat_id": ""}, {"thread_id": []},
    {"sent_at": "bad-time"}, {"schema_version": "unknown"}, {"evidence_refs": "not-a-list"}])
def test_invalid_records_never_publish(tmp_path, change):
    with pytest.raises(ValueError):
        OutboundMessageIndex(tmp_path).record(_entry(**change), now=1_800_000_001)
    assert list(tmp_path.iterdir()) == []
