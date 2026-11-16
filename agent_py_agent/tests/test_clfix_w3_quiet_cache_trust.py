"""W3 + D-10: settled guidance sources keep an empty scope quiet past the 2 s window; other scopes survive one
scope's change; a transient double link on the projection database only skips the lock-free fast path."""
from __future__ import annotations

import os

from agent_py_agent.agent.common.cache_freshness import CACHE_TRUST_AGE_SECONDS
from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.agent.conversation import observation_index as observation_module
from agent_py_agent.tests.test_observation_index_read_path import _clock, _count_locks

_TARGETS = (("thread", "t1"), ("request", "r1"), ("agent_run", "a1"), ("task", "k1"))


def _guidance_index(tmp_path):
    return ConversationStore(tmp_path / "conversations").guidance.ledger.pending_index()


def test_settled_guidance_scope_stays_quiet_for_a_minute_without_locks(tmp_path, monkeypatch):
    index = _guidance_index(tmp_path)
    monkeypatch.setattr(observation_module, "cache_entry_trustworthy", lambda _mtime_ns: True)
    clock = _clock(monkeypatch)
    assert index.rows("thread", "t1", 20, False) == ([], [])
    calls = _count_locks(monkeypatch)
    for _ in range(10):
        clock["monotonic"] += 6.0
        assert index.rows("thread", "t1", 20, False) == ([], [])
    assert calls == []


def test_fresh_guidance_sources_still_expire_with_the_window(tmp_path, monkeypatch):
    store = ConversationStore(tmp_path / "conversations")
    index = store.guidance.ledger.pending_index()
    entry = store.guidance.append({"target_type": "thread", "target_id": "t1", "message": "one"})
    index.mark_delivered([entry.guidance_id], 10)
    monkeypatch.setattr(observation_module, "cache_entry_trustworthy", lambda _mtime_ns: False)
    clock = _clock(monkeypatch)
    assert index.rows("thread", "t1", 20, False) == ([], [])
    calls = _count_locks(monkeypatch)
    assert index.rows("thread", "t1", 20, False) == ([], [])
    assert calls == []
    clock["monotonic"] += CACHE_TRUST_AGE_SECONDS
    assert index.rows("thread", "t1", 20, False) == ([], [])
    assert calls


def test_one_target_append_leaves_other_settled_scopes_quiet(tmp_path, monkeypatch):
    store = ConversationStore(tmp_path / "conversations")
    index = store.guidance.ledger.pending_index()
    monkeypatch.setattr(observation_module, "cache_entry_trustworthy", lambda _mtime_ns: True)
    clock = _clock(monkeypatch)
    for target_type, target_id in _TARGETS:
        assert index.rows(target_type, target_id, 20, False) == ([], [])
    entry = store.guidance.append({"target_type": "request", "target_id": "r1", "message": "new"})
    rows, errors = index.rows("request", "r1", 20, False)
    assert [row["id"] for row in rows] == [entry.guidance_id] and errors == []
    calls = _count_locks(monkeypatch)
    clock["monotonic"] += 30.0
    for target_type, target_id in _TARGETS:
        if target_type != "request":
            assert index.rows(target_type, target_id, 20, False) == ([], [])
    assert calls == []


def test_observation_quiet_still_expires_even_when_sources_are_settled(tmp_path, monkeypatch):
    index = ConversationStore(tmp_path / "conversations").observations._index
    monkeypatch.setattr(observation_module, "cache_entry_trustworthy", lambda _mtime_ns: True)
    clock = _clock(monkeypatch)
    assert index.pending(20) == []
    calls = _count_locks(monkeypatch)
    clock["monotonic"] += CACHE_TRUST_AGE_SECONDS
    assert index.pending(20) == []
    assert calls


def test_remembered_scopes_are_bounded(tmp_path, monkeypatch):
    index = _guidance_index(tmp_path)
    monkeypatch.setattr(observation_module, "_QUIET_SCOPE_CACHE_MAX_ENTRIES_COUNT", 3)
    monkeypatch.setattr(observation_module, "cache_entry_trustworthy", lambda _mtime_ns: True)
    _clock(monkeypatch)
    for number in range(6):
        assert index.rows("thread", f"t{number}", 20, False) == ([], [])
    assert len(index._quiet) == 3
    assert ("thread", "t5", 20, False) in index._quiet


def test_transient_double_link_only_skips_the_fast_path(tmp_path, monkeypatch):
    index = _guidance_index(tmp_path)
    monkeypatch.setattr(observation_module, "cache_entry_trustworthy", lambda _mtime_ns: True)
    _clock(monkeypatch)
    assert index.rows("thread", "t1", 20, False) == ([], [])
    alias = index.path.with_name(index.path.name + ".birth")
    os.link(index.path, alias)
    try:
        assert index._quiet_lookup(("thread", "t1", 20, False))[1] is False
        with index._read_connection() as conn:
            assert conn is None
    finally:
        alias.unlink()
    assert index._quiet_lookup(("thread", "t1", 20, False))[1] is True
