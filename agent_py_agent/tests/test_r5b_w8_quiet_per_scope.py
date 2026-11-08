"""R5B-W8：轮流询问多个目标时，每个目标的空结果各自留在可信窗口内，不再互相顶掉。"""
from __future__ import annotations

from agent_py_agent.agent.common.cache_freshness import CACHE_TRUST_AGE_SECONDS
from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.tests.test_observation_index_read_path import _clock, _count_locks

_TARGETS = (("thread", "t1"), ("request", "r1"), ("agent_run", "a1"), ("task", "k1"))


def test_alternating_guidance_targets_stay_quiet_inside_trust_window(tmp_path, monkeypatch):
    store = ConversationStore(tmp_path / "conversations")
    index = store.guidance.ledger.pending_index()
    clock = _clock(monkeypatch)
    calls = _count_locks(monkeypatch)
    for target_type, target_id in _TARGETS:
        assert index.rows(target_type, target_id, 20, False) == ([], [])
    calls.clear()
    for _ in range(3):
        for target_type, target_id in _TARGETS:
            assert index.rows(target_type, target_id, 20, False) == ([], [])
    assert calls == []
    clock["monotonic"] += CACHE_TRUST_AGE_SECONDS
    assert index.rows("thread", "t1", 20, False) == ([], [])
    assert calls


def test_new_guidance_for_one_target_is_visible_while_others_stay_quiet(tmp_path, monkeypatch):
    store = ConversationStore(tmp_path / "conversations")
    index = store.guidance.ledger.pending_index()
    _clock(monkeypatch)
    for target_type, target_id in _TARGETS:
        assert index.rows(target_type, target_id, 20, False) == ([], [])
    entry = store.guidance.append({"target_type": "request", "target_id": "r1", "message": "new"})
    rows, errors = index.rows("request", "r1", 20, False)
    assert [row["id"] for row in rows] == [entry.guidance_id] and errors == []
    for target_type, target_id in _TARGETS:
        if (target_type, target_id) != ("request", "r1"):
            assert index.rows(target_type, target_id, 20, False) == ([], [])
    index.mark_delivered([entry.guidance_id], 10)
    assert index.rows("request", "r1", 20, False) == ([], [])


def test_alternating_observation_wakes_stay_quiet_inside_trust_window(tmp_path, monkeypatch):
    store = ConversationStore(tmp_path / "conversations")
    index = store.observations._index
    _clock(monkeypatch)
    calls = _count_locks(monkeypatch)
    for wake in (None, "wake-a", "wake-b"):
        assert index.pending(20, wake=wake) == []
    calls.clear()
    for _ in range(3):
        for wake in (None, "wake-a", "wake-b"):
            assert index.pending(20, wake=wake) == []
    assert calls == []
