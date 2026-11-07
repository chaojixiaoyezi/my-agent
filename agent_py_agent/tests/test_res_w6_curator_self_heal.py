"""RES-W6：隔离历史副本、恢复已删除目录，同时保留规范记录的严格校验。"""
from __future__ import annotations

import json
import logging
import shutil
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.memory_store.candidate_models import CandidateObservation
from agent_py_agent.agent.memory_store.candidates import CandidateService
from agent_py_agent.agent.memory_store.curator_commit import (
    CuratorBatchCommitter,
    CuratorCommitRecoveryError,
)
from agent_py_agent.agent.memory_store.curator_inputs import (
    CuratorMessageSource,
    _excluded_audit_thread_ids,
)
from agent_py_agent.agent.memory_store.curator_run_log import CuratorRunLog, CuratorRunRecord
from agent_py_agent.agent.memory_store.curator_state import (
    CuratorLeaseRequest,
    MemoryCuratorStateStore,
)
from agent_py_agent.agent.memory_store.daily import DailyMemoryEvent, DailyMemoryStore
from agent_py_agent.agent.memory_store.lessons import LessonRepository
from agent_py_agent.agent.memory_store.operations import append_memory_operation_events
from agent_py_agent.tests.test_curator_state_corruption import _OkBackend
from agent_py_agent.tests.test_memory_curator_v2 import (
    _conversation,
    _service,
    _StaticStructuredBackend,
    _valid_output,
)


def _committer(root):
    """缓存真实四库；测试期间不重建对象，所有删除只发生在 pytest 临时目录。"""
    return CuratorBatchCommitter(
        candidate_service=CandidateService(root / "candidates.jsonl"),
        daily_store=DailyMemoryStore(root / "daily"),
        state_store=MemoryCuratorStateStore(root / "curator" / "state.json"),
        run_log=CuratorRunLog(root / "curator" / "runs"),
    )


def _approved(candidates):
    candidate = candidates.observe(CandidateObservation(
        "lesson", "保留精确证据再推进游标。", "res.lesson",
        {"scope_type": "personal", "scope_key": "personal"}, "user_explicit",
        source_message_refs=({"message_id": "message-1", "thread_id": "thread-1"},),
    ))
    return replace(candidate, status="approved", promotion_target="lesson", occurrence_count=3,
                   source_task_ids=("task-a", "task-b"))


def test_stray_transaction_file_and_copy_do_not_block_or_replay(tmp_path, monkeypatch, caplog):
    from agent_py_agent.agent.memory_store import curator_commit as commit

    store, thread, message = _conversation(tmp_path)
    backend = _StaticStructuredBackend(_valid_output(thread.thread_id, message.message_id, message.content))
    service = _service(tmp_path, backend, store)
    # 保留一次真实已提交事务，复制其完整清单与前镜像，而非只放空目录。
    monkeypatch.setattr(commit, "_cleanup_committed_best_effort", lambda _directory: None)
    assert service.run(reason="admin").status == "succeeded"
    committer = service.committer
    original = next(committer.transactions_dir.iterdir())
    copied = original.with_name(original.name + " copy")
    shutil.copytree(original, copied)
    manifest_path = copied / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["manifest_path"] = str(manifest_path.resolve())
    manifest_path.write_text(json.dumps(manifest))
    stray = committer.transactions_dir / "operator.bak"
    stray.write_bytes(b"untouched operator backup")
    copy_before = {p.relative_to(copied): p.read_bytes() for p in copied.rglob("*") if p.is_file()}
    candidates_before = service.candidate_service.path.read_bytes()
    with caplog.at_level(logging.WARNING):
        assert committer.recover_incomplete() == 0
        acquired = committer.recover_and_acquire(CuratorLeaseRequest("admin", "res-w6", 60))
    assert acquired is not None
    assert not original.exists()
    assert stray.read_bytes() == b"untouched operator backup"
    assert {p.relative_to(copied): p.read_bytes() for p in copied.rglob("*") if p.is_file()} == copy_before
    assert service.candidate_service.path.read_bytes() == candidates_before
    warnings = [r.getMessage() for r in caplog.records if "CURATOR_TRANSACTION_STRAY_SKIPPED" in r.getMessage()]
    assert len(warnings) == 4  # 两个杂项，每个公共恢复入口各警告一次。
    assert any("name=operator.bak" in text for text in warnings)
    assert any("name=" + copied.name in text for text in warnings)


@pytest.mark.parametrize("kind", ["canonical_file", "canonical_symlink", "copy_symlink"])
def test_canonical_non_directory_and_symlinks_still_refuse(tmp_path, kind):
    committer = _committer(tmp_path / "memory")
    outside = tmp_path / "untouched"
    outside.mkdir()
    (outside / "keep.txt").write_bytes(b"surviving bytes")
    path = committer.transactions_dir / ("x copy" if kind == "copy_symlink" else "memory-curator-run-valid")
    if kind == "canonical_file":
        path.write_bytes(b"not a directory")
    else:
        path.symlink_to(outside, target_is_directory=True)
    with pytest.raises(CuratorCommitRecoveryError, match="non-directory"):
        committer.recover_incomplete()
    assert (outside / "keep.txt").read_bytes() == b"surviving bytes"
    assert path.is_symlink() if kind != "canonical_file" else path.is_file()


@pytest.mark.parametrize("suffix", ["abc123", "with--dash", "甲"])
def test_transaction_scan_reuses_actual_writer_names(tmp_path, suffix):
    committer = _committer(tmp_path / "memory")
    directory = committer.transactions_dir / ("memory-curator-run-" + suffix)
    directory.mkdir()
    assert committer.recover_incomplete() == 0
    assert not directory.exists()  # 合法但未完成 manifest 的旧目录仍按原语义清理。


@pytest.mark.parametrize("deleted", ["curator/transactions", "curator", "."])
@pytest.mark.parametrize("entrypoint", ["recover_incomplete", "recover_and_acquire"])
def test_cached_committer_recreates_deleted_parent_directories(tmp_path, deleted, entrypoint):
    root = tmp_path / "memory"
    committer = _committer(root)
    survivor = tmp_path / "memory.md"
    survivor.write_bytes(b"surviving owner file")
    shutil.rmtree(root / deleted)
    if entrypoint == "recover_incomplete":
        assert committer.recover_incomplete() == 0
    else:
        assert committer.recover_and_acquire(CuratorLeaseRequest("admin", "res-w6", 60)) is not None
    assert committer.transactions_dir.is_dir()
    assert list(committer.transactions_dir.iterdir()) == []
    assert survivor.read_bytes() == b"surviving owner file"
    assert not (root / "lessons").exists() and not (root / "routing").exists()
    if deleted == ".":
        assert not committer.daily.daily_dir.exists()  # 不做全 memory 重新初始化。


def test_bad_thread_is_excluded_and_healthy_audit_cursor_advances(tmp_path):
    store, thread, _message = _conversation(tmp_path)
    bad = store.threads.get_or_create(dict(canonical_user_id="user-1", channel="internal",
        channel_conversation_id="bad-chat", channel_user_id="user-1", now=20.0))
    store.storage.thread_path(bad.thread_id).write_bytes(b"{")
    assert _excluded_audit_thread_ids(CuratorMessageSource(store)) == frozenset({bad.thread_id})
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir()
    rows = [dict(event_id="healthy-audit", thread_id=thread.thread_id),
            dict(event_id="excluded-audit", thread_id=bad.thread_id)]
    (audit_dir / "2026-11-06.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
    backend = _OkBackend()
    service = _service(tmp_path, backend, store)
    result = service.run(reason="admin")
    assert result.status == "succeeded", result
    assert service.state_store.load().last_processed_audit_event_id == "healthy-audit"
    assert service.run(reason="admin").status == "succeeded"
    assert service.state_store.load().last_processed_audit_event_id == "excluded-audit"
    assert bad.thread_id not in service.state_store.load().per_thread_cursors


@pytest.mark.parametrize("error", [{}, {"thread_id": ""}, {"thread_id": None}])
def test_unidentified_thread_errors_still_block_audit(error):
    source = CuratorMessageSource(SimpleNamespace(threads=SimpleNamespace(
        list_report=lambda **_kw: ([], [{"thread_id": "bad-thread"}, error]))))
    assert _excluded_audit_thread_ids(source) is None


@pytest.mark.parametrize("error", [{}, {"thread_id": ""}, {"thread_id": None}])
def test_unidentified_thread_list_errors_still_block_message_collection(error):
    from agent_py_agent.agent.memory_store.curator_inputs import _collect_messages

    store = SimpleNamespace(threads=SimpleNamespace(list_report=lambda **_kw: ([], [error])),
                            messages=SimpleNamespace(after_report=lambda *_a, **_kw: ([], [])))
    messages, errors = _collect_messages(source=CuratorMessageSource(store), cursors={}, limit=10, max_chars=10000)
    assert messages == [] and errors == [error]


def test_unreadable_thread_transcript_still_blocks_curator(tmp_path):
    store, thread, _message = _conversation(tmp_path)
    store.storage.message_path(thread.thread_id).write_bytes(b"invalid transcript\n")
    service = _service(tmp_path, _OkBackend(), store)
    result = service.run(reason="admin")
    assert result.status == "failed" and result.failure_code.startswith("CURATOR_INPUT_")
    assert service.state_store.load().per_thread_cursors == {}
    assert service.backend.calls == 0


@pytest.mark.parametrize("migration_complete", [False, True])
@pytest.mark.parametrize("copy_text", ["original", "unmarked"])
def test_lesson_operator_copies_are_ignored_before_reading(tmp_path, caplog, migration_complete, copy_text):
    root = tmp_path / "memory"
    repository = LessonRepository(root / "lessons", root / "routing" / "INDEX.md")
    record = repository.promote(_approved(CandidateService(root / "candidates.jsonl")))
    original = root.parent / record.path
    if migration_complete:
        (root / "migration.json").write_text("{}")
    text = original.read_text() if copy_text == "original" else "not a migrated lesson"
    names = ["x.bak.md", "x copy.md", "x.old.md", original.stem + ".bak.md", original.stem + " copy.md"]
    for name in names:
        (repository.lessons_dir / name).write_text(text)
    with caplog.at_level(logging.DEBUG):
        assert repository.list() == [record]
    assert all(any(name in r.getMessage() and r.levelno == logging.DEBUG for r in caplog.records) for name in names)


@pytest.mark.parametrize("subject", ["topic.bak", "_topic", "a" * 100, "中文主题", "",
                                     "a" * 79 + ".tail", "a" * 79 + "-tail"])
def test_lesson_scan_accepts_every_writer_slug_shape(tmp_path, subject):
    root = tmp_path / "memory"
    repository = LessonRepository(root / "lessons", root / "routing" / "INDEX.md")
    candidate = replace(_approved(CandidateService(root / "candidates.jsonl")), subject_key=subject)
    record = repository.promote(candidate)
    assert repository.list() == [record]


@pytest.mark.parametrize("stem", ["a.-01234567", "a--01234567"])
def test_short_slug_trailing_punctuation_is_not_a_writer_filename(tmp_path, stem):
    root = tmp_path / "memory"
    repository = LessonRepository(root / "lessons", root / "routing" / "INDEX.md")
    record = repository.promote(_approved(CandidateService(root / "candidates.jsonl")))
    (root / "migration.json").write_text("{}")
    (repository.lessons_dir / (stem + ".md")).write_text("not a migrated lesson")
    assert repository.list() == [record]


def test_canonical_lesson_corruption_after_migration_still_raises(tmp_path):
    root = tmp_path / "memory"
    repository = LessonRepository(root / "lessons", root / "routing" / "INDEX.md")
    (root / "migration.json").write_text("{}")
    (repository.lessons_dir / "topic-deadbeef.md").write_text("not a migrated lesson")
    with pytest.raises(ValueError, match="requires migration"):
        repository.list()


@pytest.mark.parametrize("writer", ["daily", "lesson", "routing", "run_log", "candidate", "ops"])
def test_cached_memory_writers_recreate_only_their_deleted_directories(tmp_path, writer):
    root = tmp_path / "memory"
    committer = _committer(root)
    lessons = LessonRepository(root / "lessons", root / "routing" / "INDEX.md")
    approved = _approved(committer.candidates)
    lessons.promote(approved)
    daily_event = DailyMemoryEvent("conversation", "新的有界经历摘要", "user", created_at="2026-11-06T12:00:00+00:00")
    run_record = CuratorRunRecord("memory-curator-run-test", "lease-test", "succeeded", "admin", "echo", "echo",
                                 "2026-11-06T12:00:00+00:00", "2026-11-06T12:00:01+00:00")
    deleted = {"daily": committer.daily.daily_dir, "lesson": lessons.lessons_dir,
               "routing": lessons.routing_index_path.parent, "run_log": committer.run_log.runs_dir,
               "candidate": root, "ops": root}[writer]
    owned = [deleted, lessons.routing_index_path.parent] if writer == "lesson" else [deleted]
    survivors = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()
                 and not any(p.is_relative_to(directory) for directory in owned)}
    survivor = tmp_path / "SOUL.md"
    survivor.write_bytes(b"surviving owner root file")
    shutil.rmtree(deleted)
    directories_before = {p for p in tmp_path.rglob("*") if p.is_dir()}
    if writer == "daily":
        event = committer.daily.append(daily_event)
        assert committer.daily.list(day=event.created_at[:10]) == [event]
    elif writer == "lesson":
        record = lessons.promote(replace(approved, candidate_id=approved.candidate_id + "-next"))
        assert lessons.list() == [record]
        assert record.lesson_id in lessons.routing_index_path.read_text()
    elif writer == "routing":
        assert lessons.rebuild_routing_index() == lessons.routing_index_path
        assert lessons.list()[0].lesson_id in lessons.routing_index_path.read_text()
    elif writer == "run_log":
        record = committer.run_log.append(run_record)
        assert committer.run_log.list() == [record]
    elif writer == "candidate":
        observation = CandidateObservation("long_term_fact", "重新追加的候选", "res.next",
            {"scope_type": "personal", "scope_key": "personal"}, "user_explicit")
        candidate = committer.candidates.observe(observation)
        assert committer.candidates.get(candidate.candidate_id).content == observation.content
    else:
        record = SimpleNamespace(entry_id="memory-test", version=1, action="add", content="not stored in ops")
        append_memory_operation_events(root / "ops.jsonl", [record])
        rows = [json.loads(line) for line in (root / "ops.jsonl").read_text().splitlines()]
        assert len(rows) == 1 and rows[0]["entry_id"] == record.entry_id
        assert "not stored in ops" not in (root / "ops.jsonl").read_text()
    created = {p for p in tmp_path.rglob("*") if p.is_dir()} - directories_before
    assert all(any(p.is_relative_to(directory) for directory in owned) for p in created)
    assert {p: p.read_bytes() for p in survivors} == survivors
    assert survivor.read_bytes() == b"surviving owner root file"
    if writer in {"candidate", "ops"}:
        assert not any((root / name).exists() for name in ("daily", "lessons", "routing", "curator"))


def test_missing_lesson_directory_lists_empty_without_recreating_it(tmp_path):
    repository = LessonRepository(tmp_path / "memory" / "lessons", tmp_path / "memory" / "routing" / "INDEX.md")
    shutil.rmtree(repository.lessons_dir)
    assert repository.list() == []
    assert not repository.lessons_dir.exists()
