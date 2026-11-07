"""用真实 JSONL、冷段与 SQLite 验证代理回收后的增量索引和失败重试。"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.common.global_write_commit import global_write_commit_scope
from agent_py_agent.agent.conversation import ConversationStore, history_index, message_scan
from agent_py_agent.agent.conversation.models import MessageLogEntry
from agent_py_agent.agent.conversation.store_io import ImmutableJsonIndex
from agent_py_agent.agent.local_storage import LocalStore
from agent_py_agent.tests.test_local_store_coordinator_io import make_store


def _case(tmp_path, count=8, *, local=None):
    store = ConversationStore(tmp_path / "conversation")
    thread = store.threads.get_or_create({"canonical_user_id": "watermark-owner"})
    case = SimpleNamespace(store=store, thread_id=thread.thread_id,
                           local=local or LocalStore(tmp_path / "local.db", enable_fts=False))
    case.path = store.storage.message_path(case.thread_id)
    case.rows = _append(case, count)
    return case


def _append(case, count):
    return [case.store.messages.append({
        "thread_id": case.thread_id, "role": "user", "content": f"searchable-{index}",
    }) for index in range(count)]


def _agent(case, *, reopen=True):
    local = LocalStore(case.local.db_path, enable_fts=False) if reopen else case.local
    return SimpleNamespace(local_store=local)


def _ensure(case, agent=None):
    agent = agent or _agent(case)
    history_index.ensure_thread_history_indexed(agent, case.store, case.thread_id)
    return agent


def _watermark(case):
    with closing(sqlite3.connect(case.local.db_path)) as conn:
        row = conn.execute("SELECT value FROM metadata WHERE key=?",
                           ("conversation_history_indexed:" + case.thread_id,)).fetchone()
    return json.loads(row[0]) if row else None


def _store_watermark(case, value):
    with closing(sqlite3.connect(case.local.db_path)) as conn:
        conn.execute("UPDATE metadata SET value=? WHERE key=?",
                     (json.dumps(value), "conversation_history_indexed:" + case.thread_id))
        conn.commit()


def _observe(monkeypatch):
    visited = []
    actual = history_index.index_conversation_message

    def visit(agent, store, row):
        visited.append(row.message_id)
        return actual(agent, store, row)

    monkeypatch.setattr(history_index, "index_conversation_message", visit)
    return visited


def _rotate(case, rows):
    """直接构造协议允许的冷段，使用产品的段 writer 与不可变索引，不引入 Compact 假数据。"""
    manifest = message_scan.transcript_manifest(case.path) or {
        "schema": "conversation_transcript_segments.v1", "thread_id": case.thread_id,
        "hot_base": 0, "segment_count": 0, "cold_message_count": 0,
        "index": None, "chain_sha256": hashlib.sha256(b"").hexdigest(),
    }
    lines = case.path.read_bytes().splitlines(keepends=True)
    cold = b"".join(lines[:rows])
    index = ImmutableJsonIndex(case.path.parent, ("archive", case.thread_id, "index"))
    message_scan._save_transcript_segment(case.path, manifest, index, manifest["hot_base"], cold)
    stage = case.path.with_suffix(".replacement")
    stage.write_bytes(b"".join(lines[rows:]))
    stage.replace(case.path)
    manifest["hot_base"] += len(cold)
    stamp = case.path.stat()
    manifest["hot_identity"] = [stamp.st_dev, stamp.st_ino]
    hot = case.path.read_bytes()
    manifest["hot_frontier"] = {"bytes": len(hot), "lines": hot.count(b"\n"),
                                "sha256": hashlib.sha256(hot).hexdigest()}
    manifest["rotation_id"] = message_scan._transcript_rotation_id(manifest)
    case.path.with_suffix(".manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return manifest


def test_first_ensure_scans_all_and_persists_complete_watermark(tmp_path, monkeypatch):
    case = _case(tmp_path)
    actual = case.store.messages.visit_all_report
    full = []

    def scan(thread_id, visitor):
        full.append(thread_id)
        return actual(thread_id, visitor)

    monkeypatch.setattr(case.store.messages, "visit_all_report", scan)
    seen = _observe(monkeypatch)
    agent = _ensure(case)
    assert full == [case.thread_id]
    assert seen == [row.message_id for row in case.rows]
    stamp = case.path.stat()
    assert _watermark(case) == {
        "schema": "conversation_history_watermark.v1", "position": stamp.st_size,
        "cold_count": 0, "cold_segments": 0, "last_segment": "",
        "hot_dev": stamp.st_dev, "hot_ino": stamp.st_ino, "hot_offset": stamp.st_size,
    }
    _ensure(case, agent)
    assert full == [case.thread_id]
    assert len(seen) == len(case.rows)


def test_recycled_agent_visits_zero_rows_then_only_three_appended_rows(tmp_path, monkeypatch):
    case = _case(tmp_path)
    first = _ensure(case)
    seen = _observe(monkeypatch)
    second = _ensure(case)
    assert second is not first and second.local_store is not first.local_store
    assert seen == []
    extra = _append(case, 3)
    _ensure(case)
    assert seen == [row.message_id for row in extra]
    assert _watermark(case)["hot_offset"] == case.path.stat().st_size


def test_recycled_agent_does_not_parse_any_existing_lines(tmp_path, monkeypatch):
    case = _case(tmp_path)
    _ensure(case)

    def unexpected_read(*_args):
        pytest.fail("水位之后没有新行，不应重读消息")

    monkeypatch.setattr(message_scan.TranscriptReader, "readline", unexpected_read)
    monkeypatch.setattr(case.store.messages, "visit_all_report", unexpected_read)
    _ensure(case)


@pytest.mark.parametrize("already_cold", [False, True])
def test_rotation_reads_only_new_segments_and_hot_and_keeps_search(tmp_path, monkeypatch, already_cold):
    case = _case(tmp_path, 20)
    old_cold = _rotate(case, 5)["hot_base"] if already_cold else 0
    _ensure(case)
    extra = _append(case, 3)
    manifest = _rotate(case, 10)
    seen = _observe(monkeypatch)
    actual_readline = message_scan.TranscriptReader.readline
    positions, writes = [], []
    agent = _agent(case)
    actual_upsert = agent.local_store.upsert_record

    def read_line(reader, *args, **kwargs):
        positions.append(reader.tell())
        return actual_readline(reader, *args, **kwargs)

    def upsert(record):
        writes.append(record.source_id)
        return actual_upsert(record)

    monkeypatch.setattr(message_scan.TranscriptReader, "readline", read_line)
    monkeypatch.setattr(agent.local_store, "upsert_record", upsert)
    _ensure(case, agent)
    expected = case.rows[5:] + extra if already_cold else case.rows + extra
    assert seen == [row.message_id for row in expected]
    assert positions and min(positions) == old_cold
    assert writes == [f"{case.thread_id}:{row.message_id}" for row in extra]
    assert _watermark(case)["cold_count"] == manifest["cold_message_count"]
    assert _watermark(case)["cold_segments"] == manifest["segment_count"]
    hits = agent.local_store.search("searchable", source_type="conversation_message", limit=100)
    assert {hit.metadata["message_id"] for hit in hits} == {row.message_id for row in case.rows + extra}
    seen.clear()
    _ensure(case)
    assert seen == []


@pytest.mark.parametrize("damage", ["schema", "last_segment", "segments", "offset", "json_type"])
def test_invalid_watermark_falls_back_to_full_scan(tmp_path, monkeypatch, damage):
    case = _case(tmp_path)
    _rotate(case, 3)
    _ensure(case)
    watermark = _watermark(case)
    if damage == "schema":
        watermark["schema"] = "unknown"
    elif damage == "last_segment":
        watermark["last_segment"] = "wrong.jsonl"
    elif damage == "segments":
        watermark["cold_segments"] = 2
    elif damage == "offset":
        watermark["hot_offset"] = True
    else:
        watermark = []
    _store_watermark(case, watermark)
    seen = _observe(monkeypatch)
    _ensure(case)
    assert seen == [row.message_id for row in case.rows]
    assert _watermark(case)["last_segment"] == "seg-00000000000000000001.jsonl"


def test_shortened_segment_list_falls_back_to_full_scan(tmp_path, monkeypatch):
    case = _case(tmp_path)
    _rotate(case, 2)
    old_hot, old_manifest = case.path.read_bytes(), case.path.with_suffix(".manifest.json").read_bytes()
    _rotate(case, 2)
    _ensure(case)
    assert _watermark(case)["cold_segments"] == 2
    case.path.write_bytes(old_hot)
    case.path.with_suffix(".manifest.json").write_bytes(old_manifest)
    seen = _observe(monkeypatch)
    _ensure(case)
    assert seen == [row.message_id for row in case.rows]
    assert _watermark(case)["cold_segments"] == 1


@pytest.mark.parametrize("rotated", [False, True])
def test_deleted_transcript_resets_watermark_without_fatal_error(tmp_path, monkeypatch, rotated):
    case = _case(tmp_path)
    if rotated:
        _rotate(case, 3)
    _ensure(case)
    case.path.unlink()
    seen = _observe(monkeypatch)
    _ensure(case)
    watermark = _watermark(case)
    assert seen == []
    assert watermark["position"] == watermark["hot_offset"] == watermark["cold_segments"] == 0
    assert watermark["hot_dev"] == watermark["hot_ino"] == 0
    if not rotated:
        extra = _append(case, 3)
        _ensure(case)
        assert seen == [row.message_id for row in extra]


@pytest.mark.parametrize("change", ["replace", "truncate"])
def test_changed_hot_file_is_revisited(tmp_path, monkeypatch, change):
    case = _case(tmp_path)
    _ensure(case)
    raw = case.path.read_bytes()
    if change == "replace":
        stage = case.path.with_suffix(".new")
        stage.write_bytes(raw)
        stage.replace(case.path)
        expected = case.rows
    else:
        case.path.write_bytes(raw.split(b"\n", 1)[0] + b"\n")
        expected = case.rows[:1]
    seen = _observe(monkeypatch)
    _ensure(case)
    assert seen == [row.message_id for row in expected]
    assert _watermark(case)["hot_offset"] == case.path.stat().st_size


@pytest.mark.parametrize("rotated", [False, True])
@pytest.mark.parametrize("fault", ["visitor", "json", "model", "utf8"])
def test_failure_preserves_watermark_and_retries_pending_rows(tmp_path, monkeypatch, rotated, fault):
    case = _case(tmp_path)
    if rotated:
        _rotate(case, 3)
    _ensure(case)
    before = _watermark(case)
    extra = _append(case, 3)
    good = case.path.read_bytes()
    agent = _agent(case)
    actual = history_index.index_conversation_message
    calls = []

    def visit(agent, store, row):
        calls.append(row.message_id)
        if fault == "visitor":
            raise RuntimeError("visitor failed")
        return actual(agent, store, row)

    monkeypatch.setattr(history_index, "index_conversation_message", visit)
    if fault != "visitor":
        broken = {"json": b"not-json\n", "model": b'{"created_at":"invalid"}\n', "utf8": b"\xff\n"}
        with case.path.open("ab") as handle:
            handle.write(broken[fault])
    with pytest.raises(RuntimeError if fault == "visitor" else OSError):
        _ensure(case, agent)
    assert _watermark(case) == before
    assert case.thread_id not in agent._conversation_indexed_threads
    if fault != "visitor":
        assert calls == []
    case.path.write_bytes(good)
    monkeypatch.setattr(history_index, "index_conversation_message", actual)
    seen = _observe(monkeypatch)
    _ensure(case, agent)
    assert seen == [row.message_id for row in extra]


@pytest.mark.parametrize("rotated", [False, True])
def test_unfinished_valid_row_does_not_advance_past_last_complete_line(tmp_path, monkeypatch, rotated):
    case = _case(tmp_path)
    if rotated:
        _rotate(case, 3)
    _ensure(case)
    offset = case.path.stat().st_size
    row = _append(case, 1)[0]
    case.path.write_bytes(case.path.read_bytes()[:-1])
    seen = _observe(monkeypatch)
    _ensure(case)
    assert seen == [row.message_id]
    assert _watermark(case)["hot_offset"] == offset
    with case.path.open("ab") as handle:
        handle.write(b"\n")
    seen.clear()
    _ensure(case)
    assert seen == [row.message_id]
    assert _watermark(case)["hot_offset"] == case.path.stat().st_size


@pytest.mark.parametrize("rotated", [False, True])
def test_incremental_parser_keeps_display_and_unicode_contract(tmp_path, monkeypatch, rotated):
    case = _case(tmp_path)
    if rotated:
        _rotate(case, 3)
    _ensure(case)
    extra = _append(case, 1)[0]
    case.store.messages.append({"thread_id": case.thread_id, "role": "display", "content": "进度"})
    text = "多字节😀\u0085\u2028\u2029"
    row = MessageLogEntry("unicode-row", case.thread_id, "user", text)
    with case.path.open("ab") as handle:
        handle.write(b" \t\r\n" + (json.dumps(row.to_dict(), ensure_ascii=False) + "\r\n").encode())
    seen = _observe(monkeypatch)
    _ensure(case)
    assert seen == [extra.message_id, row.message_id]
    assert _watermark(case)["hot_offset"] == case.path.stat().st_size


def test_cold_incremental_parser_rejects_unicode_blank_like_full_reader(tmp_path):
    case = _case(tmp_path)
    _rotate(case, 3)
    _ensure(case)
    before = _watermark(case)
    with case.path.open("ab") as handle:
        handle.write("\u2028\n".encode())
    full_seen, seen = [], []
    full_errors = case.store.messages.visit_all_report(case.thread_id, full_seen.append)
    errors, watermark = case.store.messages.visit_from_report(case.thread_id, before, seen.append)
    assert errors == full_errors and errors
    assert watermark is before
    assert seen == full_seen == []


def test_rotation_during_visit_keeps_watermark_and_retries_new_layout(tmp_path, monkeypatch):
    case = _case(tmp_path)
    _ensure(case)
    before = _watermark(case)
    _append(case, 3)
    actual = history_index.index_conversation_message
    rotated = False

    def visit(agent, store, row):
        nonlocal rotated
        actual(agent, store, row)
        if not rotated:
            rotated = True
            _rotate(case, 2)

    monkeypatch.setattr(history_index, "index_conversation_message", visit)
    agent = _agent(case)
    with pytest.raises(OSError, match="could not be indexed reliably"):
        _ensure(case, agent)
    assert _watermark(case) == before
    assert case.thread_id not in agent._conversation_indexed_threads
    monkeypatch.setattr(history_index, "index_conversation_message", actual)
    _ensure(case, agent)
    assert _watermark(case)["cold_segments"] == 1
    hits = agent.local_store.search("searchable", source_type="conversation_message", limit=100)
    assert len(hits) == len(case.rows) + 3


def test_late_append_during_full_scan_is_left_for_next_agent(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation import store_messages

    case = _case(tmp_path)
    actual = store_messages._validate_all
    late = MessageLogEntry("late-row", case.thread_id, "user", "late searchable")

    def validate(path):
        result = actual(path)
        with path.open("ab") as handle:
            handle.write((json.dumps(late.to_dict()) + "\n").encode())
        return result

    monkeypatch.setattr(store_messages, "_validate_all", validate)
    seen = _observe(monkeypatch)
    _ensure(case)
    assert seen == [row.message_id for row in case.rows]
    assert _watermark(case)["hot_offset"] < case.path.stat().st_size
    seen.clear()
    _ensure(case)
    assert seen == [late.message_id]


def test_metadata_write_failure_is_retryable_and_not_marked_complete(tmp_path, monkeypatch):
    case = _case(tmp_path)
    _ensure(case)
    before = _watermark(case)
    _append(case, 3)
    actual = history_index._write_local_state
    agent = _agent(case)

    def fail(*_args):
        raise sqlite3.OperationalError("injected metadata failure")

    monkeypatch.setattr(history_index, "_write_local_state", fail)
    with pytest.raises(sqlite3.OperationalError):
        _ensure(case, agent)
    assert _watermark(case) == before
    assert case.thread_id not in agent._conversation_indexed_threads
    monkeypatch.setattr(history_index, "_write_local_state", actual)
    _ensure(case, agent)
    assert _watermark(case)["hot_offset"] == case.path.stat().st_size


def test_coordinator_watermark_write_uses_current_local_write_gate(tmp_path, monkeypatch):
    local, door = make_store(tmp_path, enable_fts=False)
    case = _case(tmp_path, local=local)
    actual = history_index._write_local_state
    writes = []

    def write(conn, key, value):
        assert door.inside
        writes.append(key)
        return actual(conn, key, value)

    monkeypatch.setattr(history_index, "_write_local_state", write)
    with global_write_commit_scope(door):
        _ensure(case, _agent(case, reopen=False))
    assert writes == ["conversation_history_indexed:" + case.thread_id]
    before = _watermark(case)
    # 新代理没有新消息，第二次门调用就是水位事务；撤权不能借旧水位跳过写门。
    door.reject_at = door.calls + 2
    agent = _agent(case, reopen=False)
    with global_write_commit_scope(door), pytest.raises(type(door.error)) as failure:
        _ensure(case, agent)
    assert failure.value is door.error
    assert case.thread_id not in agent._conversation_indexed_threads
    assert _watermark(case) == before
    door.reject_at = None
    with global_write_commit_scope(door):
        _ensure(case, agent)
