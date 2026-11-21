"""Regression tests for durable correction review publication."""

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.capability.memory_tool import RememberTool
from agent_py_agent.agent.memory_store.recall import MemoryRecallScope
from agent_py_agent.agent.owner_wake_discovery import _has_pending_feedback_review
from agent_py_agent.cli import gateway_loops
from agent_py_agent.tests.test_conveyor_m63_correction_feedback import (
    _correction,
    _receipt_rows,
    _seed,
)
from agent_py_agent.tests.test_memory_tool import _agent_with_current_user


def test_pending_review_replays_after_formal_version_advances(tmp_path, monkeypatch):
    changed = "The maintenance window opens on Tuesday."
    agent = _agent_with_current_user(tmp_path, content=changed)
    entry = _seed(agent)
    tool = RememberTool(agent)

    def unavailable(_observation):
        raise OSError("candidate publication unavailable")

    with monkeypatch.context() as fault:
        fault.setattr(agent.memory_candidates, "observe", unavailable)
        accepted = tool.execute(_correction(entry, note="The original correction."))
    assert accepted.ok, accepted.output
    saved = json.loads(accepted.output)
    assert saved["review_status"] == "pending"
    updated = tool.execute({"action": "replace", "entry_id": entry["entry_id"],
                            "content": changed, "origin": "user_explicit"})
    assert updated.ok and json.loads(updated.output)["active_memory_changed"], updated.output
    formal_before = agent.memory.path.read_bytes()

    replay = tool.execute(_correction(entry, "wrong", note="A conflicting repeat."))
    assert replay.ok, replay.output
    payload = json.loads(replay.output)
    assert payload["receipt"] == saved["receipt"] and payload["conflicting_repeat"] is True
    candidate = agent.memory_candidates.get(payload["review_candidate_id"])
    assert candidate.target_entry_version == entry["version"]
    assert "The original correction." in candidate.content
    assert "A conflicting repeat." not in candidate.content
    assert _receipt_rows(agent.subagents.runtime_db) == [saved["receipt"]]
    assert agent.memory.path.read_bytes() == formal_before


@pytest.mark.parametrize("interruption", ["before_write", "before_ack"])
def test_owner_worker_recovers_original_review_after_restart(tmp_path, monkeypatch, interruption):
    agent = _agent_with_current_user(tmp_path / "source")
    other = _agent_with_current_user(tmp_path / "other")
    home = agent.home_paths.owner_home_dir
    scope_key = "project:maintenance"
    entry = agent.memory.add("user", "Approved maintenance window", kind="fact", attributes={
        "scope_type": "project", "scope_key": scope_key, "subject_key": "maintenance.window",
    })
    agent._current_run_params.memory_recall_scope = MemoryRecallScope.from_runtime(
        task_attributes={"project_id": scope_key},
    )
    tool = RememberTool(agent)
    original = agent.memory_candidates.observe
    request = _correction({"entry_id": entry.entry_id, "version": entry.version}, note="Original schedule note.")

    def simulated_io(observation):
        if interruption == "before_ack":
            original(observation)
        raise OSError("candidate publication interrupted")

    with monkeypatch.context() as fault:
        fault.setattr(agent.memory_candidates, "observe", simulated_io)
        accepted = tool.execute(request)
    assert accepted.ok, accepted.output
    saved = json.loads(accepted.output)
    assert saved["review_status"] == "pending"
    assert _has_pending_feedback_review(home)
    assert not _has_pending_feedback_review(other.home_paths.owner_home_dir)

    # Retain the original accepted scope even if an ordinary formal mutation changes it.
    [advanced] = agent.memory.apply_batch([{
        "action": "replace", "entry_id": entry.entry_id, "expected_version": entry.version,
        "content": "Updated maintenance window", "attributes": {"scope_type": "personal", "scope_key": "personal"},
    }])
    assert advanced.version == entry.version + 1
    formal_before = agent.memory.path.read_bytes()
    agent._current_run_params.memory_recall_scope = MemoryRecallScope.from_runtime()
    invisible = tool.execute(request)
    assert invisible.error_code == "MEMORY_ENTRY_NOT_FOUND"
    agent._current_run_params.task_id = "new-host-task"
    stale = tool.execute(request)
    assert (stale.error_code, stale.effect_outcome) == ("MEMORY_VERSION_CONFLICT", "not_started")

    restarted = _agent_with_current_user(tmp_path / "source")
    supervisor = SimpleNamespace()
    monkeypatch.setattr(gateway_loops, "_curator_run_allowed", lambda *_: False)
    monkeypatch.setattr(gateway_loops, "_run_memory_curator_maintenance", lambda *_: None)
    monkeypatch.setattr(gateway_loops, "_run_skill_learning", lambda *_: None)

    def tick(owner):
        return gateway_loops._BackgroundMainSupervisor._safe_run_curator(supervisor, owner, "test")

    tick(other)
    assert not other.memory_candidates.list()
    assert _has_pending_feedback_review(home)
    restarted.owner_policy = SimpleNamespace(memory_enabled=False)
    tick(restarted)
    assert _has_pending_feedback_review(home)
    restarted.owner_policy = None

    ready = Barrier(2)

    def repair():
        ready.wait(timeout=10)
        tick(restarted)

    with ThreadPoolExecutor(max_workers=2) as threads:
        repairs = [threads.submit(repair) for _ in range(2)]
        for future in repairs:
            future.result(timeout=20)
    tick(restarted)

    [review] = restarted.memory_candidates.list()
    assert review.occurrence_count == 1
    assert review.scope["scope_key"] == scope_key and review.target_entry_version == entry.version
    assert review.evidence_refs[0]["ref_id"] == saved["receipt"]["receipt_id"]
    assert review.evidence_refs[0]["verdict"] == saved["receipt"]["verdict"]
    assert "Original schedule note." in review.content
    assert (review.status, review.promotion_target) == ("pending_review", "none")
    assert _receipt_rows(restarted.subagents.runtime_db) == [saved["receipt"]]
    assert not _has_pending_feedback_review(home)
    assert restarted.memory.path.read_bytes() == formal_before


@pytest.mark.parametrize("advanced", [False, True])
def test_legacy_receipt_backfill_requires_exact_original_version(tmp_path, monkeypatch, advanced):
    agent = _agent_with_current_user(tmp_path)
    entry = _seed(agent)
    tool = RememberTool(agent)
    request = _correction(entry, note="Original receipt note.")

    def unavailable(_observation):
        raise OSError("candidate publication unavailable")

    with monkeypatch.context() as fault:
        fault.setattr(agent.memory_candidates, "observe", unavailable)
        accepted = tool.execute(request)
    assert accepted.ok, accepted.output
    receipt = json.loads(accepted.output)["receipt"]
    repo = agent.subagents.runtime_db
    with repo.transaction() as conn:
        # Recreate the pre-upgrade representation with only the canonical receipt and note.
        conn.execute("DELETE FROM metadata WHERE key=?", ("memory_feedback_review_pending:" + receipt["receipt_id"],))
    assert not _has_pending_feedback_review(agent.home_paths.owner_home_dir)
    if advanced:
        agent.memory.apply_batch([{
            "action": "replace", "entry_id": entry["entry_id"], "expected_version": entry["version"],
            "content": "Updated window", "attributes": {"scope_type": "personal", "scope_key": "personal"},
        }])

    repeat = tool.execute(_correction(entry, "wrong", note="Replacement note."))
    if advanced:
        assert repeat.error_code == "MEMORY_VERSION_CONFLICT"
        assert not agent.memory_candidates.list()
    else:
        assert repeat.ok, repeat.output
        assert json.loads(repeat.output)["receipt"] == receipt
        [review] = agent.memory_candidates.list()
        assert review.target_entry_version == entry["version"]
        assert "Original receipt note." in review.content and "Replacement note." not in review.content
    assert _receipt_rows(repo) == [receipt]
