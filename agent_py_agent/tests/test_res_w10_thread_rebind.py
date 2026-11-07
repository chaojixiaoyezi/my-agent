"""RES-W10：真实文件故障下按通道重绑，不恢复备份或改动其它历史。"""
from __future__ import annotations

import json
import logging
import shutil
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Barrier, Event, current_thread

import pytest

from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.agent.conversation import compact_checkpoint as checkpoints
from agent_py_agent.agent.conversation import store_threads as records
from agent_py_agent.agent.conversation.compact import load_conversation_compact_source
from agent_py_agent.agent.conversation.compact_scope import THREAD_COMPACT_SCOPE
from agent_py_agent.agent.conversation.context_usage import save_context_usage_snapshot
from agent_py_agent.agent.conversation.models import ConversationCompactCommit
from agent_py_agent.agent.runtime_errors import DataCorruptionError
from agent_py_agent.tests.test_compact_scoped_checkpoint import compact_case as _compact_case
from agent_py_agent.tests.test_transcript_cold_rotation import _commit_rotation

compact_case = _compact_case


def _request(**changes):
    return {"canonical_user_id": "owner", "channel": "feishu",
            "channel_conversation_id": "chat-a", "channel_user_id": "actor", **changes}


def _used_thread(store, request):
    thread = store.threads.get_or_create(request)
    for number in range(2):
        for role in ("user", "assistant"):
            store.messages.append({"thread_id": thread.thread_id, "role": role,
                                   "content": f"{role}-{number}"})
        assert save_context_usage_snapshot(store, store.threads.require(thread.thread_id),
            {"schema": "model_visible_context_usage.v1", "current_tokens": 120 + number})
    return store.threads.require(thread.thread_id)


def _next_thread(store, request):
    try:
        return store.threads.get_or_create(request)
    except DataCorruptionError as error:
        pytest.fail(f"the first message must rebind instead of raising {type(error).__name__}")


def _assert_bound(store, request, thread_id):
    assert store.threads.resolve(channel=request["channel"],
        channel_conversation_id=request["channel_conversation_id"],
        channel_user_id=request["channel_user_id"]).thread_id == thread_id
    latest, error = store.threads.latest_for_user_report(request["canonical_user_id"])
    assert error is None and latest.thread_id == thread_id
    assert store.threads.get_or_create(request).thread_id == thread_id


@pytest.mark.parametrize("reuse", [False, True])
@pytest.mark.parametrize("deleted", ["hot", "messages"])
def test_deleted_used_transcript_rebinds_without_touching_owner_state(tmp_path, caplog, reuse, deleted):
    owner = tmp_path / "owner"
    store = ConversationStore(owner / "conversations")
    request = _request(reuse_latest_for_user=reuse)
    thread = _used_thread(store, request)
    record = store.storage.thread_path(thread.thread_id)
    original = record.read_bytes()
    other_request = _request(canonical_user_id="other", channel_conversation_id="chat-b")
    other = _used_thread(store, other_request)
    protected = [store.storage.thread_path(other.thread_id), store.storage.message_path(other.thread_id)]
    for name in ("memory/long_term/notes.jsonl", "SOUL.md", "skills/example/SKILL.md", "scheduler/jobs.json"):
        target = owner / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"owner state must not change\n")
        protected.append(target)
    # 整个 messages/ 删除时，另一线程正文也是操作员主动删除，不计入修复副作用。
    if deleted == "messages":
        protected.remove(store.storage.message_path(other.thread_id))
        shutil.rmtree(store.storage.messages_dir)
    else:
        store.storage.message_path(thread.thread_id).unlink()
    before = {path: path.read_bytes() for path in protected}
    with caplog.at_level(logging.WARNING, logger=records.__name__):
        current = _next_thread(store, request)
    assert current.thread_id != thread.thread_id
    assert current.summary == "" and current.compact_generation == 0 and not current.model_context_usage
    _assert_bound(store, request, current.thread_id)
    assert record.read_bytes() == original
    assert {path: path.read_bytes() for path in protected} == before
    assert f"THREAD_REBOUND_AFTER_DELETION old={thread.thread_id} reason=transcript_missing" in caplog.text
    appended = store.messages.append({"thread_id": current.thread_id, "role": "user", "content": "first retry-free turn"})
    assert store.messages.recent_report(current.thread_id)[0] == [appended]


@pytest.mark.parametrize("field,value", [
    ("compact_generation", 1), ("compacted_through_byte_offset", 1),
    ("compacted_through_message_id", "msg-prior"), ("model_context_usage", {"current_tokens": 1}),
])
def test_each_prior_turn_marker_detects_missing_transcript(tmp_path, field, value):
    store = ConversationStore(tmp_path / "conversations")
    request = _request()
    old = store.threads.get_or_create(request)
    store.threads.update_atomic(old.thread_id, lambda row: replace(row, **{field: value}))
    assert _next_thread(store, request).thread_id != old.thread_id


@pytest.mark.parametrize("bad", [b"{", b"[]", b"\xff"])
def test_bad_bound_record_is_archived_with_lock_and_no_list_error(tmp_path, caplog, bad):
    store = ConversationStore(tmp_path / "conversations")
    request = _request()
    old = _used_thread(store, request)
    record = store.storage.thread_path(old.thread_id)
    lock = Path(f"{record}.lock")
    assert lock.exists()
    record.write_bytes(bad)
    with caplog.at_level(logging.WARNING, logger=records.__name__):
        current = _next_thread(store, request)
    assert current.thread_id != old.thread_id
    archive = store.storage.threads_dir / ".ledger_archive"
    assert (archive / record.name).read_bytes() == bad
    assert (archive / lock.name).exists() and not lock.exists() and not record.exists()
    _assert_bound(store, request, current.thread_id)
    threads, errors = store.threads.list_report(limit=0)
    assert errors == [] and [row.thread_id for row in threads] == [current.thread_id]
    assert f"THREAD_RECORD_QUARANTINED thread={old.thread_id}" in caplog.text
    store.messages.append({"thread_id": current.thread_id, "role": "user", "content": "continue"})


@pytest.mark.parametrize("reuse", [False, True])
def test_bad_bindings_are_archived_and_each_channel_gets_a_fresh_thread(tmp_path, caplog, reuse):
    store = ConversationStore(tmp_path / "conversations")
    first, second = _request(), _request(canonical_user_id="other", channel_conversation_id="chat-b")
    old = [_used_thread(store, request) for request in (first, second)]
    originals = {store.storage.thread_path(row.thread_id): store.storage.thread_path(row.thread_id).read_bytes()
                 for row in old}
    bindings = store.storage.bindings_path
    bindings.write_bytes(b"broken binding record")
    with caplog.at_level(logging.WARNING, logger=records.__name__):
        current = [_next_thread(store, {**request, "reuse_latest_for_user": reuse and request == first})
                   for request in (first, second)]
    assert not ({row.thread_id for row in current} & {row.thread_id for row in old})
    archive = store.storage.root / ".ledger_archive"
    assert (archive / bindings.name).read_bytes() == b"broken binding record"
    assert (archive / (bindings.name + ".lock")).exists()
    assert {path: path.read_bytes() for path in originals} == originals
    for request, row in zip((first, second), current):
        _assert_bound(store, request, row.thread_id)
        store.messages.append({"thread_id": row.thread_id, "role": "user", "content": "continue"})
    assert "THREAD_RECORD_QUARANTINED" in caplog.text


@pytest.mark.parametrize("fault", ["record", "transcript"])
def test_latest_candidate_uses_the_same_recovery_checks(tmp_path, fault):
    store = ConversationStore(tmp_path / "conversations")
    old = _used_thread(store, _request())
    if fault == "record":
        store.storage.thread_path(old.thread_id).write_bytes(b"{")
    else:
        store.storage.message_path(old.thread_id).unlink()
    request = _request(channel_conversation_id="new-chat", reuse_latest_for_user=True)
    current = _next_thread(store, request)
    assert current.thread_id != old.thread_id
    _assert_bound(store, request, current.thread_id)


@pytest.mark.parametrize("suffix", [" copy", ".bak", ".old"])
def test_list_ignores_noncanonical_copies_before_reading_them(tmp_path, caplog, suffix):
    store = ConversationStore(tmp_path / "conversations")
    old = store.threads.get_or_create(_request())
    stray = store.storage.threads_dir / f"thread-x{suffix}.json"
    stray.write_bytes(store.storage.thread_path(old.thread_id).read_bytes())
    with caplog.at_level(logging.DEBUG, logger=records.__name__):
        threads, errors = store.threads.list_report(limit=0)
    assert errors == [] and [row.thread_id for row in threads] == [old.thread_id]
    assert stray.name in caplog.text


@pytest.mark.parametrize("kind", ["bad-json", "directory"])
def test_canonical_bad_record_still_reports_a_list_error(tmp_path, kind):
    store = ConversationStore(tmp_path / "conversations")
    path = store.storage.threads_dir / "thread-broken.json"
    if kind == "directory":
        path.mkdir()
    else:
        path.write_bytes(b"{")
    threads, errors = store.threads.list_report(limit=0)
    assert threads == [] and len(errors) == 1
    assert errors[0]["context"] == "conversation.thread.read"


@pytest.mark.parametrize("reuse", [False, True])
def test_fresh_thread_without_transcript_keeps_its_id(tmp_path, reuse):
    store = ConversationStore(tmp_path / "conversations")
    request = _request(reuse_latest_for_user=reuse)
    old = store.threads.get_or_create(request)
    assert not store.storage.message_path(old.thread_id).exists()
    assert store.threads.get_or_create(request).thread_id == old.thread_id


def test_deleted_thread_record_rebinds_but_keeps_the_transcript(tmp_path):
    store = ConversationStore(tmp_path / "conversations")
    request = _request()
    old = _used_thread(store, request)
    transcript = store.storage.message_path(old.thread_id)
    original = transcript.read_bytes()
    store.storage.thread_path(old.thread_id).unlink()
    current = _next_thread(store, request)
    assert current.thread_id != old.thread_id
    _assert_bound(store, request, current.thread_id)
    assert transcript.read_bytes() == original


def test_rotated_thread_missing_hot_file_rebinds_and_appends(compact_case, monkeypatch):
    case = compact_case
    old, _, _, hot = _commit_rotation(case, monkeypatch)
    manifest = hot.with_suffix(".manifest.json")
    assert json.loads(manifest.read_text())["segment_count"] > 0
    segments = list((hot.parent / "archive" / old.thread_id).glob("seg-*.jsonl"))
    assert segments
    protected = [manifest, *segments, case.store.storage.thread_path(old.thread_id)]
    originals = {path: path.read_bytes() for path in protected}
    hot.unlink()
    request = {"canonical_user_id": "owner", "now": 30.0}
    current = _next_thread(case.store, request)
    assert current.thread_id != old.thread_id
    appended = case.store.messages.append({"thread_id": current.thread_id, "role": "user", "content": "continue"})
    rows, errors = case.store.messages.recent_report(current.thread_id)
    assert errors == [] and rows == [appended]
    assert {path: path.read_bytes() for path in protected} == originals


def _compact_once(case, label):
    rows = [case.store.messages.append({"thread_id": case.thread_id, "role": "user", "content": f"{label}-{n}"})
            for n in range(6)]
    boundary = case.store.messages.byte_offset_after(case.thread_id, rows[-2].message_id)
    thread = case.store.threads.require(case.thread_id)
    checkpoint = checkpoints.write_compact_checkpoint(case.agent, checkpoints.CompactCheckpointRequest(
        thread=thread, summary=label, operation_evidence={}, compact_rows=tuple(rows[:-1]),
        retained_tail=(rows[-1],), source_end_byte_offset=boundary, projected_tokens_before=900,
        projected_tokens_after=300, policy=case.policy, forced=False, scope=THREAD_COMPACT_SCOPE))
    return case.store.threads.update_compact_state(case.thread_id, commit=ConversationCompactCommit(
        summary=label, operation_evidence={}, checkpoint_id=checkpoint,
        compacted_through_message_id=rows[-2].message_id, compacted_through_byte_offset=boundary,
        source_messages=5, source_tool_pairs=0, publish_thread_view=True), expected_generation=thread.compact_generation)


def test_older_thread_record_restore_keeps_id_and_next_compaction_commits(compact_case):
    case = compact_case
    _compact_once(case, "first")
    record = case.store.storage.thread_path(case.thread_id)
    older = record.read_bytes()
    second = _compact_once(case, "second")
    assert second.compact_generation == 2
    record.write_bytes(older)
    current = case.store.threads.get_or_create({"canonical_user_id": "owner", "now": 40.0})
    assert current.thread_id == case.thread_id and current.compact_generation == 1
    source = load_conversation_compact_source(case.agent, case.store, current, scope=THREAD_COMPACT_SCOPE)
    assert source.thread.thread_id == case.thread_id
    third = _compact_once(case, "after-restore")
    assert third.thread_id == case.thread_id and third.compact_generation == 2
    assert third.compact_checkpoint_id != second.compact_checkpoint_id and third.summary == "after-restore"


@pytest.mark.parametrize("fault", ["record", "transcript", "bindings"])
def test_concurrent_same_channel_recovery_creates_exactly_one_thread(tmp_path, fault):
    store = ConversationStore(tmp_path / "conversations")
    request = _request()
    old = _used_thread(store, request)
    if fault == "record":
        store.storage.thread_path(old.thread_id).write_bytes(b"{")
    elif fault == "transcript":
        store.storage.message_path(old.thread_id).unlink()
    else:
        store.storage.bindings_path.write_bytes(b"{")
    barrier = Barrier(4)

    def resolve():
        other_store = ConversationStore(store.storage.root, initialize=False)
        barrier.wait(timeout=10)
        return other_store.threads.get_or_create(request).thread_id

    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(lambda _: resolve(), range(4)))
    assert len(set(ids)) == 1 and ids[0] != old.thread_id
    threads, errors = store.threads.list_report(limit=0)
    assert errors == []
    assert {row.thread_id for row in threads} == ({ids[0]} if fault == "record" else {old.thread_id, ids[0]})
    _assert_bound(store, request, ids[0])


def test_quarantine_failure_preserves_original_and_does_not_claim_recovery(tmp_path, monkeypatch):
    store = ConversationStore(tmp_path / "conversations")
    request = _request()
    old = _used_thread(store, request)
    path = store.storage.thread_path(old.thread_id)
    path.write_bytes(b"{")
    before = store.storage.bindings_path.read_bytes()
    monkeypatch.setattr(records, "archive_ledger_file", lambda *_: False, raising=False)
    with pytest.raises(DataCorruptionError):
        store.threads.get_or_create(request)
    assert path.read_bytes() == b"{" and store.storage.bindings_path.read_bytes() == before


def test_unrelated_latest_index_error_still_fails_closed(tmp_path):
    store = ConversationStore(tmp_path / "conversations")
    old = _used_thread(store, _request())
    latest = store.storage.user_latest_path
    latest.write_bytes(b"{")
    before = store.storage.thread_path(old.thread_id).read_bytes()
    with pytest.raises(DataCorruptionError):
        store.threads.get_or_create(_request(channel_conversation_id="new-chat", reuse_latest_for_user=True))
    assert latest.read_bytes() == b"{" and store.storage.thread_path(old.thread_id).read_bytes() == before


# LLM: 用事件固定坏索引旧错误晚于兄弟恢复的顺序，不替换真实读取、归档或绑定写入。
# 函数用途: 验证迟到通道不再隔离刚恢复的健康绑定。
def test_stale_cross_channel_quarantine_preserves_repaired_sibling_binding(tmp_path, monkeypatch):
    store = ConversationStore(tmp_path / "conversations")
    first, second = _request(), _request(canonical_user_id="other", channel_conversation_id="chat-b")
    old = [store.threads.get_or_create(request) for request in (first, second)]
    store.storage.bindings_path.write_bytes(b"{")
    observed, resume = Event(), Event()
    quarantine = records._quarantine_thread_resolution_error

    # LLM: 只在隔离入口协调第二通道，随后调用真实实现并保留blocking参数。
    # 函数用途: 稳定重现陈旧错误与兄弟恢复交错。
    def delay_second(thread_store, error, **options):
        if current_thread().name.startswith("stale-B") and error.get("context") == "conversation.bindings.read":
            observed.set()
            assert resume.wait(10), "first channel did not finish recovery"
        return quarantine(thread_store, error, **options)

    monkeypatch.setattr(records, "_quarantine_thread_resolution_error", delay_second)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="stale-B") as pool:
        pending = pool.submit(store.threads.get_or_create, second)
        try:
            assert observed.wait(10), "second channel did not observe corrupt bindings"
            recovered_first = store.threads.get_or_create(first)
        finally:
            resume.set()
        recovered_second = pending.result(timeout=10)
    for request, recovered in zip((first, second), (recovered_first, recovered_second)):
        _assert_bound(store, request, recovered.thread_id)
    threads, errors = store.threads.list_report(limit=0)
    assert errors == [] and len(threads) == len(old) + 2
    assert (store.storage.root / ".ledger_archive" / "channel_bindings.json").read_bytes() == b"{"


# LLM: bound/latest的可读外来身份均不能被JSON隔离分支洗成新会话；拒绝时核对原字节不变。
# 函数用途: 验证外来线程身份保持拒绝且不归档。
@pytest.mark.parametrize("reuse", [False, True])
def test_foreign_embedded_thread_identity_stays_fail_closed(tmp_path, caplog, reuse):
    store = ConversationStore(tmp_path / "conversations")
    old = _used_thread(store, _request())
    record = store.storage.thread_path(old.thread_id)
    payload = json.loads(record.read_bytes())
    payload["thread_id"] = "thread-foreign"
    record.write_text(json.dumps(payload), encoding="utf-8")
    protected = [record, store.storage.bindings_path, store.storage.user_latest_path]
    before = {path: path.read_bytes() for path in protected}
    request = _request(channel_conversation_id="new-chat" if reuse else "chat-a", reuse_latest_for_user=reuse)
    with pytest.raises(DataCorruptionError):
        store.threads.get_or_create(request)
    assert {path: path.read_bytes() for path in protected} == before
    assert not (store.storage.threads_dir / ".ledger_archive").exists()
    assert "THREAD_RECORD_QUARANTINED" not in caplog.text


# LLM: 无历史、已有历史以及latest均须拒绝热正文/manifest悬链，不创建新线程。
# 函数用途: 区分悬空链接与真实文件删除。
@pytest.mark.parametrize("reuse", [False, True])
@pytest.mark.parametrize("used", [False, True])
@pytest.mark.parametrize("backing", ["hot", "manifest"])
def test_dangling_backing_symlink_stays_fail_closed(tmp_path, caplog, reuse, used, backing):
    store = ConversationStore(tmp_path / "conversations")
    old = _used_thread(store, _request()) if used else store.threads.get_or_create(_request())
    hot = store.storage.message_path(old.thread_id)
    path = hot if backing == "hot" else hot.with_suffix(".manifest.json")
    if path.exists():
        path.unlink()
    path.symlink_to(store.storage.root / "missing-target")
    record = store.storage.thread_path(old.thread_id)
    before = record.read_bytes(), store.storage.bindings_path.read_bytes()
    request = _request(channel_conversation_id="new-chat" if reuse else "chat-a", reuse_latest_for_user=reuse)
    with pytest.raises(DataCorruptionError):
        store.threads.get_or_create(request)
    assert path.is_symlink() and not path.exists()
    assert (record.read_bytes(), store.storage.bindings_path.read_bytes()) == before
    assert len(list(store.storage.threads_dir.glob("*.json"))) == 1
    assert "THREAD_REBOUND_AFTER_DELETION" not in caplog.text


# LLM: 新续作允许健康显式复用，但已丢正文、坏JSON和已归档线程不能从latest复活。
# 函数用途: 验证恢复索引后的后到通道仍过滤不可用最近线程。
@pytest.mark.parametrize("backing", ["healthy", "deleted", "corrupt", "quarantined"])
def test_later_channel_explicit_reuse_filters_unusable_candidates(tmp_path, backing):
    store = ConversationStore(tmp_path / "conversations")
    first, second = _request(), _request(canonical_user_id="other", channel_conversation_id="chat-b")
    store.threads.get_or_create(first)
    old = _used_thread(store, second)
    store.storage.bindings_path.write_bytes(b"{")
    store.threads.get_or_create(first)
    record = store.storage.thread_path(old.thread_id)
    if backing == "deleted":
        store.storage.message_path(old.thread_id).unlink()
    elif backing == "corrupt":
        record.write_bytes(b"{")
    elif backing == "quarantined":
        assert records.archive_ledger_file(record, store.storage.threads_dir / ".ledger_archive")
    recovered = store.threads.get_or_create({**second, "reuse_latest_for_user": True})
    assert (recovered.thread_id == old.thread_id) is (backing == "healthy")
    _assert_bound(store, second, recovered.thread_id)
    appended = store.messages.append({"thread_id": recovered.thread_id, "role": "user", "content": "continue"})
    assert store.messages.recent_report(recovered.thread_id)[0][-1] == appended


# LLM: 两通道合法共享同一线程；坏记录已被先行请求隔离后，迟到请求须把缺失当缺失而非旧损坏。
# 函数用途: 固定独审发现的共享线程记录竞争，同时覆盖bound与latest入口。
@pytest.mark.parametrize("candidate", ["bound", "latest"])
def test_stale_shared_thread_quarantine_recovers_on_first_request(tmp_path, monkeypatch, candidate):
    store = ConversationStore(tmp_path / "conversations")
    first, second = _request(), _request(channel_conversation_id="chat-b", reuse_latest_for_user=True)
    old = _used_thread(store, first)
    assert store.threads.get_or_create(second).thread_id == old.thread_id
    record = store.storage.thread_path(old.thread_id)
    record.write_bytes(b"{")
    observed, resume = Event(), Event()
    quarantine = records._quarantine_thread_resolution_error

    # LLM: 只延迟共享坏记录的旧错误；真实归档完成后才允许迟到请求继续。
    # 函数用途: 固定先归档后复核缺失的交错。
    def delay_second(thread_store, error, **options):
        if current_thread().name.startswith("shared-B") and error.get("context") == "conversation.thread.read":
            observed.set()
            assert resume.wait(10), "first channel did not quarantine the shared thread"
        return quarantine(thread_store, error, **options)

    monkeypatch.setattr(records, "_quarantine_thread_resolution_error", delay_second)
    request = second if candidate == "bound" else {**second, "channel_conversation_id": "new-chat"}
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="shared-B") as pool:
        pending = pool.submit(store.threads.get_or_create, request)
        try:
            assert observed.wait(10), "second channel did not observe the corrupt shared thread"
            recovered_first = store.threads.get_or_create(first)
        finally:
            resume.set()
        recovered_second = pending.result(timeout=10)
    assert old.thread_id not in {recovered_first.thread_id, recovered_second.thread_id}
    assert (store.storage.threads_dir / ".ledger_archive" / record.name).read_bytes() == b"{"
    for query, recovered in ((first, recovered_first), (request, recovered_second)):
        assert store.threads.get_or_create(query).thread_id == recovered.thread_id
        store.messages.append({"thread_id": recovered.thread_id, "role": "user", "content": "continue"})
    assert store.threads.list_report(limit=0)[1] == []
