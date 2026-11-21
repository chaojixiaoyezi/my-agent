from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.memory_store.curator_formal import FormalMemorySource
from agent_py_agent.agent.memory_store.curator_inputs import curator_batch_scopes
from agent_py_agent.agent.memory_store.jsonl import JsonlMemory, MemoryRecord
from agent_py_agent.agent.memory_store.lessons import HotRuleRepository, LessonRepository
from agent_py_agent.agent.memory_store.owner_services import (
    ExplicitRememberServices,
    remember_explicit,
)
from agent_py_agent.agent.memory_store.read_index import MemoryReadIndex
from agent_py_agent.agent.memory_store.recall import MemoryRecallScope, runtime_recall_predicate
from agent_py_agent.agent.memory_store.rule_notices import RuleNoticeStore
from agent_py_agent.tests.test_memory_promotion_v2 import _explicit, _runtime


def _fact(entry_id, content, *, scope_type="project", scope_key="project:current", subject=None):
    return MemoryRecord(
        role="user", kind="fact", entry_id=entry_id, content=content,
        attributes={"scope_type": scope_type, "scope_key": scope_key, "subject_key": subject or entry_id},
    )


def _seed_index(memory, records):
    memory.path.parent.mkdir(parents=True, exist_ok=True)
    memory.path.write_text("".join(record.to_json() + "\n" for record in records), encoding="utf-8")
    memory.migrate_tiers()
    for _ in range(3):
        coverage = memory.rebuild_read_index()
        if coverage["status"] == "complete":
            return
    raise AssertionError(coverage)


@pytest.mark.parametrize("query", ["deploy", ""])
def test_formal_window_keeps_scoped_rows_before_index_limit(tmp_path, query):
    memory = JsonlMemory(tmp_path / "memory" / "long_term" / "memory.jsonl")
    outside = [_fact(f"a-other-{i:04}", f"deploy {i}", scope_key="project:outside") for i in range(1000)]
    inside = [_fact(f"z-current-{i}", f"deploy {i} " + "some unrelated detail " * 12) for i in range(5)]
    _seed_index(memory, [*outside, *inside])
    source = FormalMemorySource(memory, LessonRepository(tmp_path / "lessons", tmp_path / "INDEX.md"),
                                HotRuleRepository(tmp_path / "memory-hot.md"))
    batch = SimpleNamespace(messages=(), audit_events=(SimpleNamespace(task_id="task:current"),))
    scopes = curator_batch_scopes(batch)
    assert {row.authority_id for row in source.read(query=query, scopes=scopes)} == {
        record.entry_id for record in inside
    }


@pytest.mark.parametrize("candidate_type", ["long_term_fact", "working_agreement"])
def test_promotion_locates_with_complete_index_without_all(tmp_path, monkeypatch, candidate_type):
    service, store, thread = _runtime(tmp_path)
    _seed_index(service.long_term, [_fact("existing", "Existing fact")])
    candidate = _explicit(service, store, thread, content="A new personal fact", subject_key="new.fact",
                          candidate_type=candidate_type)
    original = service.long_term.all
    scans = []

    def count_all():
        scans.append(True)
        return original()

    monkeypatch.setattr(service.long_term, "all", count_all)
    result = service.promote(candidate.candidate_id, automatic=True)
    assert result.promoted, result
    assert scans == []
    if candidate_type == "working_agreement":
        notice = RuleNoticeStore(service.persona.owner_home).take("another-thread")
        assert notice == candidate.content


def test_explicit_remember_returns_the_committed_record_without_all(tmp_path, monkeypatch):
    service, _store, _thread = _runtime(tmp_path)
    _seed_index(service.long_term, [_fact("existing", "Existing fact")])
    monkeypatch.setattr(service.long_term, "all", lambda: pytest.fail("unexpected full memory scan"))
    record = remember_explicit(ExplicitRememberServices(service.candidates, service, service.long_term),
                               "A user-saved fact")
    assert record.content == "A user-saved fact"
    assert service.long_term.read_ids([record.entry_id]) == [record]


def test_complete_v3_watermark_rebuilds_scoped_recall(tmp_path):
    memory = JsonlMemory(tmp_path / "memory" / "long_term" / "memory.jsonl")
    old_dream = replace(
        _fact("dream", "deploy documented procedure"),
        source="user-profile-dream", attributes={"relocated_from": "USER.md#old"},
    )
    _seed_index(memory, [_fact("inside", "deploy current procedure", scope_key="task:current"), old_dream,
                        _fact("outside", "deploy outside procedure", scope_key="project:outside")])
    index = MemoryReadIndex(memory.path)
    root = index.store.manifest()
    source_bytes = {part["path"]: (memory.path.parent / part["path"]).read_bytes() for part in root["segments"]}
    # 保留真正的旧表列、全文投影和同代完成水位。
    with closing(sqlite3.connect(index.path)) as db, db:
        db.executescript("""
            ALTER TABLE entries RENAME TO entries_current;
            CREATE TABLE entries (id TEXT PRIMARY KEY, version INTEGER, content_hash TEXT,
                                  segment INTEGER, row_no INTEGER, expires_at REAL, search_text TEXT);
            INSERT INTO entries SELECT id,version,content_hash,segment,row_no,expires_at,search_text
                FROM entries_current;
            DROP TABLE entries_current;
        """)
        watermark = json.loads(db.execute("SELECT value FROM metadata WHERE key='watermark'").fetchone()[0])
        assert watermark["complete"]
        db.execute("UPDATE metadata SET value=? WHERE key='watermark'", (json.dumps({**watermark, "format_version": 3}),))

    scope = MemoryRecallScope((("project", "project:current"), ("project", "task:current"), ("personal", "personal")))
    predicate = runtime_recall_predicate(scope, "deploy")

    def assert_recall():
        rows = memory.search_scoped("deploy", 5, predicate)
        assert {row.entry_id for row in rows} == {"inside", "dream"}

    assert index.coverage()["status"] == "missing"
    assert index.query("deploy")[0] == []
    assert_recall()
    assert memory.rebuild_read_index(batch_size=1)["status"] == "partial"
    assert index.query("deploy")[0] == []
    assert_recall()
    assert memory.rebuild_read_index(batch_size=3)["status"] == "complete"
    assert_recall()
    with closing(sqlite3.connect(index.path)) as db:
        assert dict(db.execute("SELECT key,value FROM metadata")).keys() == {"watermark"}
        assert db.execute("SELECT id,scope_type,scope_key,subject_key FROM entries ORDER BY id").fetchall() == [
            ("dream", "personal", "personal", ""),
            ("inside", "project", "project:current", "inside"),
            ("outside", "project", "project:outside", "outside"),
        ]
    assert index.store.manifest() == root
    assert {name: (memory.path.parent / name).read_bytes() for name in source_bytes} == source_bytes


@pytest.mark.parametrize("stale_index", [False, True])
def test_promotion_conflict_uses_canonical_scope_and_rechecks_version(tmp_path, monkeypatch, stale_index):
    service, store, thread = _runtime(tmp_path)
    _seed_index(service.long_term, [
        _fact("current", "Original fact", scope_key="task:current", subject="shared.subject"),
        _fact("other", "Other project fact", scope_key="project:other", subject="shared.subject"),
    ])
    if stale_index:
        index = MemoryReadIndex(service.long_term.path)
        with closing(sqlite3.connect(index.path)) as db, db:
            db.execute("UPDATE entries SET version=99 WHERE id='current'")
    candidate = _explicit(service, store, thread, content="Conflicting fact", subject_key="shared.subject",
                          scope_type="project", scope_key="project:current")
    scans = []
    original = service.long_term.all

    def count_all():
        scans.append(True)
        return original()

    monkeypatch.setattr(service.long_term, "all", count_all)
    result = service.promote(candidate.candidate_id, automatic=True)
    assert not result.promoted
    assert result.reason_code == "SUBJECT_SCOPE_CONFLICT_REQUIRES_REPLACE"
    assert service.candidates.get(candidate.candidate_id).conflicts_with == ["current"]
    assert scans == ([True] if stale_index else [])


def test_empty_add_reports_exact_content_conflicts_without_all(tmp_path, monkeypatch):
    service, store, thread = _runtime(tmp_path)
    _seed_index(service.long_term, [
        _fact("existing", "A  matching FACT", subject="different.subject"),
        _fact("unrelated", "unrelated fact"),
    ])
    candidate = _explicit(service, store, thread, content="A matching fact", subject_key="new.subject")
    monkeypatch.setattr(service.long_term, "apply_batch", lambda _operations, **_kwargs: [])
    monkeypatch.setattr(service.long_term, "all", lambda: pytest.fail("unexpected full memory scan"))
    result = service.promote(candidate.candidate_id, automatic=True)
    assert result.reason_code == "LONG_TERM_ADD_IDEMPOTENCY_IDENTITY_MISMATCH"
    assert service.candidates.get(candidate.candidate_id).conflicts_with == ["existing"]


def test_semantic_curator_keeps_exact_lexical_hits_after_candidate_limit(tmp_path, monkeypatch):
    from agent_py_agent.tests._hashing_embedder import LocalHashingEmbedder

    memory = JsonlMemory(tmp_path / "memory" / "long_term" / "memory.jsonl")
    unrelated = [_fact(f"a-current-{i:04}", f"ordinary procedure {i}") for i in range(1000)]
    exact = [_fact(f"z-current-{i}", f"quasarneedle procedure {i}") for i in range(5)]
    _seed_index(memory, [*unrelated, *exact])
    source = FormalMemorySource(memory, LessonRepository(tmp_path / "lessons", tmp_path / "INDEX.md"),
                                HotRuleRepository(tmp_path / "memory-hot.md"))
    scopes = frozenset({("project", "project:current")})
    assert {row.authority_id for row in source.read(query="quasarneedle", scopes=scopes)} == {
        record.entry_id for record in exact
    }
    empty_keyword = source.read(query="", scopes=scopes)
    monkeypatch.setattr(memory, "_embedder", LocalHashingEmbedder())
    monkeypatch.setattr("agent_py_agent.agent.memory_store.jsonl._queue_vector_backfill", lambda *_args: None)
    window = source.read(query="quasarneedle", scopes=scopes)
    assert {row.authority_id for row in window} == {record.entry_id for record in exact}
    assert any(row["code"] == "FORMAL_MEMORY_COVERAGE_PARTIAL"
               and "candidate_window_limited" in row["detail"] and "vectors_missing" in row["detail"]
               for row in window.errors)
    empty = source.read(query="", scopes=scopes)
    assert 0 < len(empty) <= 32 and empty == empty_keyword and empty.errors == ()
    assert memory._pending_access == {}
