"""核对记忆纠正回执的版本、幂等与正式记忆边界。"""

import hashlib
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from agent_py_agent.agent.capability.memory_search_tool import MemorySearchTool
from agent_py_agent.agent.capability.memory_tool import RememberTool
from agent_py_agent.agent.io.sqlite_schema import schema_fingerprint, stamp_schema
from agent_py_agent.agent.memory_store.read_index import MemoryReadIndex
from agent_py_agent.agent.memory_store.recall import MemoryRecallScope
from agent_py_agent.agent.runtime_db import schema
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.tests._tool_runtime_harness import execute_registry_test_call
from agent_py_agent.tests.test_memory_tool import _agent_with_current_user


def _seed(agent):
    agent.memory.add(
        "user", "The maintenance window opens on Sunday.", kind="fact",
        attributes={"scope_type": "personal", "scope_key": "personal", "subject_key": "maintenance.window"},
    )
    listed = RememberTool(agent).execute({"action": "list"})
    assert listed.ok, listed.output
    [entry] = json.loads(listed.output)["entries"]
    return entry


def _correction(entry, verdict="outdated", **extra):
    return {"action": "correct", "entry_id": entry["entry_id"],
            "version": entry["version"], "verdict": verdict, **extra}


def _expected_id(agent, entry):
    task = agent._current_run_params.task_id
    return hashlib.sha256(f"{entry['entry_id']}\0{entry['version']}\0{task}".encode()).hexdigest()


def test_presented_version_creates_one_receipt_without_editing_memory(tmp_path):
    agent = _agent_with_current_user(tmp_path)
    entry = _seed(agent)
    tool = RememberTool(agent)
    request = _correction(entry, note="The maintenance window moved.")
    formal_before = agent.memory.path.read_bytes()
    first = tool.execute(request)
    assert first.ok, first.output
    payload = json.loads(first.output)
    assert payload["receipt"]["receipt_id"] == _expected_id(agent, entry)
    assert payload["receipt"]["task_identity"] == agent._current_run_params.task_id
    assert payload["active_memory_changed"] is False
    review = agent.memory_curator.candidate_service.get(payload["review_candidate_id"])
    assert (review.status, review.promotion_target) == ("pending_review", "none")
    assert (review.target_entry_id, review.target_entry_version) == (entry["entry_id"], entry["version"])
    assert review.evidence_refs[0]["ref_id"] == payload["receipt"]["receipt_id"]
    assert "The maintenance window moved." in review.content
    assert not agent.memory_promotion.promote(review.candidate_id, automatic=True).promoted
    repeat = json.loads(tool.execute(request).output)
    conflict = json.loads(tool.execute(_correction(entry, "wrong")).output)
    assert repeat["replayed"] is True
    assert conflict["conflicting_repeat"] is True
    assert payload["receipt"] == repeat["receipt"] == conflict["receipt"]
    assert payload["review_candidate_id"] == repeat["review_candidate_id"] == conflict["review_candidate_id"]
    [record] = agent.memory.read_ids([entry["entry_id"]])
    assert (record.content, record.version) == (entry["content"], entry["version"])
    assert agent.memory.path.read_bytes() == formal_before
    assert _receipt_rows(agent.subagents.runtime_db) == [payload["receipt"]]


def _receipt_rows(repo):
    with repo._runtime_connection() as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM memory_feedback_receipts ORDER BY receipt_id")]


def test_changed_formal_version_rejects_stale_feedback_and_hints(tmp_path):
    corrected = "The maintenance window opens on Tuesday."
    agent = _agent_with_current_user(tmp_path, content=corrected)
    entry = _seed(agent)
    tool = RememberTool(agent)
    original = tool.execute(_correction(entry))
    assert original.ok
    result = tool.execute({"action": "replace", "entry_id": entry["entry_id"], "content": corrected,
                           "origin": "user_explicit"})
    assert result.ok and json.loads(result.output)["active_memory_changed"], result.output

    stale = tool.execute(_correction(entry))
    assert (stale.error_code, stale.effect_outcome) == ("MEMORY_VERSION_CONFLICT", "not_started")
    [current] = json.loads(tool.execute({"action": "list"}).output)["entries"]
    assert current["entry_id"] == entry["entry_id"] and current["version"] == entry["version"] + 1
    assert current["content"] == corrected and "feedback" not in current
    assert _receipt_rows(agent.subagents.runtime_db) == [json.loads(original.output)["receipt"]]


@pytest.mark.parametrize("changes", [
    {"version": True}, {"version": 0}, {"version": 1 << 63}, {"verdict": "maybe"},
    {"note": ["invalid"]}, {"task_identity": "another-task"},
])
def test_correction_rejects_invalid_parameters(tmp_path, changes):
    agent = _agent_with_current_user(tmp_path)
    params = _correction(_seed(agent))
    params.update(changes)
    result = RememberTool(agent).execute(params)
    assert (result.error_code, result.effect_outcome) == ("TOOL_INVALID_ARGUMENTS", "not_started")
    assert _receipt_rows(agent.subagents.runtime_db) == []


def test_correction_requires_host_identity_and_visible_target(tmp_path):
    agent = _agent_with_current_user(tmp_path)
    current = agent._current_run_params
    hidden = agent.memory.add("user", "Project maintenance window", kind="fact",
                              attributes={"scope_type": "project", "scope_key": "project:hidden"})
    tool = RememberTool(agent)
    current.memory_recall_scope = MemoryRecallScope.from_runtime()
    denied = tool.execute(_correction({"entry_id": hidden.entry_id, "version": hidden.version}))
    missing = tool.execute(_correction({"entry_id": "missing", "version": hidden.version}))
    assert denied.output == missing.output and denied.error_code == "MEMORY_ENTRY_NOT_FOUND"

    current.context_scope = "task_local"
    assert tool.execute(_correction({"entry_id": hidden.entry_id, "version": hidden.version})).error_code == "TOOL_UNAVAILABLE"
    current.context_scope = "default"
    current.task_id = ""
    assert tool.execute(_correction({"entry_id": hidden.entry_id, "version": hidden.version})).error_code == "TOOL_UNAVAILABLE"
    assert _receipt_rows(agent.subagents.runtime_db) == []


def test_receipt_and_first_note_survive_candidate_projection_failure(tmp_path, monkeypatch):
    agent = _agent_with_current_user(tmp_path)
    entry = _seed(agent)
    tool = RememberTool(agent)

    def fail_before_write(_observation):
        raise OSError("candidate store temporarily unavailable")

    with monkeypatch.context() as failure:
        failure.setattr(agent.memory_candidates, "observe", fail_before_write)
        initial = tool.execute(_correction(entry, note="Use the approved schedule."))
        assert initial.ok, initial.output
        saved = json.loads(initial.output)
        assert saved["review_status"] == "pending"
    retry = json.loads(tool.execute(_correction(entry, "wrong", note="Ignore this conflicting note.")).output)
    assert retry["conflicting_repeat"] is True and retry["receipt"] == saved["receipt"]
    candidate = agent.memory_candidates.get(retry["review_candidate_id"])
    assert "Use the approved schedule." in candidate.content
    assert "Ignore this conflicting note." not in candidate.content


def test_concurrent_corrections_keep_one_original_receipt(tmp_path):
    agent = _agent_with_current_user(tmp_path)
    entry = _seed(agent)
    active = agent._current_run_params
    barrier = Barrier(8)

    def submit(index):
        agent._current_run_params = active
        barrier.wait(timeout=10)
        verdict = "outdated" if index % 2 == 0 else "wrong"
        result = RememberTool(agent).execute(_correction(entry, verdict, note=f"Observation {index}"))
        assert result.ok, result.output
        return json.loads(result.output)

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(submit, range(8)))
    [first] = [row for row in results if not row["replayed"] and not row["conflicting_repeat"]]
    assert sum(row["conflicting_repeat"] for row in results) == 4
    assert all(row["receipt"] == first["receipt"] for row in results)
    assert {row["review_candidate_id"] for row in results} == {first["review_candidate_id"]}
    assert _receipt_rows(agent.subagents.runtime_db) == [first["receipt"]]
    [review] = agent.memory_candidates.list()
    assert review.occurrence_count == 1


def test_receipt_survives_baseline_upgrade_restart_and_cold_rebuild(tmp_path, monkeypatch):
    parent_ddl = tuple(sql for sql in schema._BASE_RUNTIME_SQL if "memory_feedback" not in sql)
    old_fingerprint = schema_fingerprint((parent_ddl, schema._RUNTIME_MIGRATIONS, schema._RUNTIME_MIGRATION_SQL))
    with monkeypatch.context() as baseline:
        baseline.setattr(schema, "_BASE_RUNTIME_SQL", parent_ddl)
        agent = _agent_with_current_user(tmp_path)
        repo = agent.subagents.runtime_db
        with repo._runtime_connection() as conn:
            assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='memory_feedback_receipts'").fetchone()

    RuntimeRepository(repo.db_path)
    entry = _seed(agent)
    result = RememberTool(agent).execute(_correction(entry, note="Verify the approved schedule."))
    assert result.ok, result.output
    original = json.loads(result.output)["receipt"]
    # 模拟旧解释器给仍含新表的库盖旧章，再让新进程执行同一项恢复安装。
    with repo.transaction() as conn:
        stamp_schema(conn, old_fingerprint)
    child = """
import json
import sys
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
repo = RuntimeRepository(sys.argv[1])
with repo._runtime_connection() as conn:
    print(json.dumps(dict(conn.execute(
        "SELECT * FROM memory_feedback_receipts WHERE receipt_id=?", (sys.argv[2],)
    ).fetchone())))
"""
    interpreter = [sys.executable]
    if not getattr(sys, "_is_gil_enabled", lambda: True)():
        interpreter += ["-X", "gil=0"]
    restarted = subprocess.run(
        [*interpreter, "-c", child, str(repo.db_path), original["receipt_id"]],
        check=True, capture_output=True, text=True, timeout=30,
    )
    assert json.loads(restarted.stdout) == original

    policy = agent.home_paths.owner_home_dir / "memory_policy.json"
    policy.write_text(json.dumps({"budget": {"active_library_max_chars": 10}}))
    agent.memory.migrate_tiers()
    assert not agent.memory.active_records()
    MemoryReadIndex(agent.memory.path).invalidate()
    agent.memory.rebuild_read_index()
    search = MemorySearchTool(agent).execute({"query": "maintenance window"})
    assert search.ok, search.output
    [visible] = json.loads(search.output)["entries"]
    assert visible["entry_id"] == entry["entry_id"] and visible["version"] == entry["version"]
    assert visible["feedback"]["receipt_id"] == original["receipt_id"]
    assert visible["feedback"]["verdict"] == original["verdict"]
    assert _receipt_rows(repo) == [original]


def test_correction_runs_through_registry_schema_and_effect_policy(tmp_path):
    agent = _agent_with_current_user(tmp_path)
    request = _correction(_seed(agent), "confirmed")
    result = execute_registry_test_call(agent.tools, "remember", request, register_with=agent)
    assert result.ok and result.effect_outcome == "confirmed", result.output
    assert json.loads(result.output)["receipt"]["verdict"] == "confirmed"
