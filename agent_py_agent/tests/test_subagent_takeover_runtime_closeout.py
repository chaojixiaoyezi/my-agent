"""被接替的子代理在运行账里也要有终态（2026-09-30，G03 第二条观察）。

脚本模型端到端（证据批次 bg-subagent-paths-9f88）：子代理被宿主收口成 BLOCKED（settle_agent_attempt 只结束执行片，
agent_run 有意保留 created 以便续跑），随后被 replacement_for_run_ids 接替、canonical 转 TAKEN_OVER，但 runtime.db 的
agent_run 永远停在 created。锁定：接替落账后，执行轮已静止的来源收成 cancelled（与 runner 准入的 TAKEN_OVER→cancelled
同一口径）；仍在运行的来源不动；已关闭来源（只记 superseded）不动；没有权威库或写失败都不影响接替本身。
"""
from __future__ import annotations

import json
from types import SimpleNamespace

from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool
from agent_py_agent.agent.runtime_db import run_takeover
from agent_py_agent.agent.subagents.manager import SubAgentManager


def _managed(tmp_path):
    manager = SubAgentManager(tmp_path, workspace_root=tmp_path, owner_home_dir=str(tmp_path / "home"))
    source = manager.create_run(goal="旧子代理读取资料", thought="执行", plan=["读取"], role="worker")
    repo = manager.runtime_db
    agent_run_id = repo.agent_run_for_run_id(source.id)["agent_run_id"]
    attempt = repo.create_attempt(agent_run_id, reuse_pending=True)
    return manager, source.id, agent_run_id, attempt["attempt_id"]


def _blocked(tmp_path):
    manager, run_id, agent_run_id, attempt_id = _managed(tmp_path)
    # 与宿主收口 BLOCKED 相同：只结束执行片，agent_run 保持 created。
    assert manager.runtime_db.settle_agent_attempt(agent_run_id=agent_run_id, attempt_id=attempt_id)["settled"]
    manager.lifecycle.set_status(run_id, "BLOCKED")
    return manager, run_id, agent_run_id, attempt_id


def _replace(manager, source_id):
    agent = SimpleNamespace(
        config=SimpleNamespace(enable_subagents=True, max_subagents=10, access_mode="workspace-write"),
        subagents=manager, tools=SimpleNamespace(specs=lambda: []),
    )
    result = CreateSubagentsTool(agent).execute(
        {"goal": "接替旧子代理读取资料", "role": "worker", "replacement_for_run_ids": [source_id], "defer_start": True})
    return result, json.loads(result.output)


def _run(manager, agent_run_id):
    row = manager.runtime_db.get_agent_run(agent_run_id)
    return str(row["status"]), str(manager.runtime_db.get_attempt(row["current_attempt_id"])["status"])


def _completed_events(manager, agent_run_id):
    events = manager.runtime_db.events_for_agent_run(agent_run_id, event_type="agent_run.completed")
    return [event["payload"] if isinstance(event.get("payload"), dict) else json.loads(event.get("payload_json") or "{}")
            for event in events]


def test_blocked_source_replaced_is_settled_cancelled_in_the_runtime_ledger(tmp_path):
    manager, source_id, agent_run_id, _attempt_id = _blocked(tmp_path)
    assert _run(manager, agent_run_id) == ("created", "done")
    result, payload = _replace(manager, source_id)
    replacement_id = payload["created_run_ids"][0]
    assert result.ok and manager.load(source_id).status == "TAKEN_OVER"
    # 改前这里仍是 ("created", "done")：执行片结束了，运行本身永远没有终态。
    assert _run(manager, agent_run_id) == ("cancelled", "done")
    [event] = _completed_events(manager, agent_run_id)
    assert (event["runtime_source"], event["runtime_reason"], event["takeover_by"]) == (
        "subagent_takeover", "taken_over", replacement_id)


def test_running_source_taken_over_keeps_its_live_attempt(tmp_path):
    manager, source_id, agent_run_id, attempt_id = _managed(tmp_path)
    manager.record_takeover(source_id, take_over_by="run-successor", reason="接管")
    assert manager.load(source_id).status == "TAKEN_OVER"
    # 活的执行轮不在这里停：运行账不动，执行锁仍在，等取消入口或 runner 自己收口。
    assert _run(manager, agent_run_id) == ("created", "running")
    outcome = run_takeover.settle_taken_over_run(manager.runtime_db, source_id, takeover_by="run-successor")
    assert outcome == {"settled": False, "reason": "source_attempt_active", "attempt_status": "running"}
    assert manager.runtime_db.get_attempt(attempt_id)["ended_at"] == 0


def test_closed_source_superseded_keeps_its_runtime_status(tmp_path):
    manager, source_id, agent_run_id, attempt_id = _managed(tmp_path)
    assert manager.runtime_db.settle_agent_run(agent_run_id=agent_run_id, status="done", attempt_id=attempt_id)["settled"]
    manager.lifecycle.set_status(source_id, "DONE")
    _result, payload = _replace(manager, source_id)
    assert payload["replacement_records"][0]["disposition"] == "superseded"
    assert _run(manager, agent_run_id) == ("done", "done")
    assert [event.get("runtime_source") for event in _completed_events(manager, agent_run_id)] == [None]


def test_settle_is_idempotent_and_skips_pending_or_unknown_attempts(tmp_path):
    manager, source_id, agent_run_id, attempt_id = _blocked(tmp_path)
    first = run_takeover.settle_taken_over_run(manager.runtime_db, source_id, takeover_by="run-x")
    second = run_takeover.settle_taken_over_run(manager.runtime_db, source_id, takeover_by="run-x")
    assert first["settled"] is True and second == {"settled": False, "reason": "already_terminal", "run_status": "cancelled"}
    pending_manager, pending_source, pending_run, _pending_attempt = _managed(tmp_path / "pending")
    fresh = pending_manager.create_run(goal="排队中的子代理", thought="t", plan=["p"], role="worker")
    assert run_takeover.settle_taken_over_run(pending_manager.runtime_db, fresh.id, takeover_by="run-y")["reason"] == \
        "source_attempt_active"
    assert run_takeover.settle_taken_over_run(pending_manager.runtime_db, "no-such-run", takeover_by="run-y") == {
        "settled": False, "reason": "no_runtime_authority"}


def test_best_effort_never_raises_and_skips_unmanaged(tmp_path):
    class Broken:
        def agent_run_for_run_id(self, run_id):
            raise OSError("disk gone")

    assert run_takeover.settle_taken_over_run_best_effort(None, "run-a", takeover_by="run-b") == {
        "settled": False, "reason": "no_runtime_db"}
    assert run_takeover.settle_taken_over_run_best_effort(Broken(), "run-a", takeover_by="run-b") == {
        "settled": False, "reason": "write_error", "error_type": "OSError"}
    unmanaged = SubAgentManager(tmp_path, workspace_root=tmp_path)
    source = unmanaged.create_run(goal="本地投影", thought="t", plan=["p"], role="worker")
    unmanaged.lifecycle.set_status(source.id, "BLOCKED")
    result, _payload = _replace(unmanaged, source.id)
    assert result.ok and unmanaged.load(source.id).status == "TAKEN_OVER"


def test_settle_guards_the_observed_attempt_status():
    # 读到的静止状态必须作为 CAS 条件交给同一写事务：读写之间 attempt 被重新挂载就不能覆盖。
    calls = []

    class Repo:
        def agent_run_for_run_id(self, run_id):
            return {"agent_run_id": "ar-1", "status": "created", "current_attempt_id": "at-1"}

        def get_attempt(self, attempt_id):
            return {"status": "failed"}

        def settle_agent_run(self, **kwargs):
            calls.append(kwargs)
            return {"settled": False, "reason": "attempt_status_conflict"}

    outcome = run_takeover.settle_taken_over_run(Repo(), "run-1", takeover_by="run-2")
    assert outcome == {"settled": False, "reason": "attempt_status_conflict", "attempt_id": "at-1"}
    assert [(call["status"], call["attempt_id"], call["expected_attempt_status"]) for call in calls] == [
        ("cancelled", "at-1", "failed")]
