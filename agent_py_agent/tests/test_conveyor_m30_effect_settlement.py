from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from threading import Event

import pytest

from agent_py_agent.agent.runtime_db import repository
from agent_py_agent.agent.runtime_db.managed_operation_store import ManagedOperationStore
from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.manager_runner_result_payload import RecordRunnerResultParams
from agent_py_agent.agent.subagents.models import SubAgentRunnerResult, SubAgentTask
from agent_py_agent.agent.subagents.result_processors import _write_runner_result_files
from agent_py_agent.agent.subagents.services import runtime_closeout as closeout
from agent_py_agent.agent.subagents.services.runner_result_commit import (
    commit_runner_result,
    prepare_runner_result_commit,
)
from agent_py_agent.agent.tooling.models import ToolHandlerOutcome
from agent_py_agent.agent.tooling.tool_operation_coordinator import _result_payload
from agent_py_agent.tests.test_dispatch_liveness_and_revive import (
    _closeout_fixture,
    _completion_notifier,
    _wakes_for_attempt,
)


@pytest.mark.parametrize(
    "handler,effect,status,reason,expected",
    [
        (True, "unknown", "succeeded", "", "unknown"),
        (True, "partial", "succeeded", "", "partial"),
        (True, "not_started", "failed", "", "not_started"),
        (True, "failed", "failed", "", "failed"),
        (None, "confirmed", "UNKNOWN", "", "applied"),
        (False, "applied", "UNKNOWN", "", "applied"),
        (True, "", "succeeded", "", "applied"),
        (True, "", "failed", "", "unknown"),
        (None, "", "DONE", "", "applied"),
        (False, "", "FAILED", "coordinator_post_execution_error:OSError", "unknown"),
        (None, "", "FAILED", "", "not_started"),
        (False, "", "unknown", "", "unknown"),
    ],
)
def test_classifier_rule_order(handler, effect, status, reason, expected):
    from agent_py_agent.agent.contracts.effect_settlement import classify_effect_settlement

    assert classify_effect_settlement(handler, effect, status, unknown_reason=reason) == expected


@pytest.mark.parametrize(
    "effect,receipt_status,settlement,expected_status",
    [
        ("failed", "FAILED", "failed", "failed"),
        ("not_started", "FAILED", "not_started", "failed"),
        ("unknown", "UNKNOWN", "unknown", "unknown"),
        ("partial", "UNKNOWN", "partial", "unknown"),
        ("confirmed", "SUCCEEDED", "applied", "succeeded"),
    ],
)
def test_serialized_operation_receipts(effect, receipt_status, settlement, expected_status):
    from agent_py_agent.agent.contracts.effect_settlement import (
        classify_effect_settlement,
        operation_status_for_settlement,
    )

    ok = receipt_status == "SUCCEEDED"
    handler_effect = effect if effect in {"failed", "not_started", "unknown"} else ""
    result = ToolHandlerOutcome(
        "run_command", ok, '{"exit_code": 3}' if effect == "failed" else "",
        handler_executed=True, effect_outcome=handler_effect,
    )
    receipt = _result_payload(result, settled_effect_outcome=effect)
    if effect == "partial":
        # 这里模拟既有验证记录，不把 partial 塞给 handler outcome。
        receipt["effect_outcome"] = effect
    stored = json.loads(json.dumps(receipt))
    observed = classify_effect_settlement(
        stored["handler_executed"], stored["effect_outcome"], receipt_status,
    )
    assert observed == settlement
    assert operation_status_for_settlement(observed, ok=stored["ok"]) == expected_status
    assert operation_status_for_settlement(observed, ok=True) == "succeeded"


def test_legacy_failed_status_normalization():
    from agent_py_agent.agent.contracts.effect_settlement import normalize_legacy_operation_status

    assert normalize_legacy_operation_status("FAILED", "coordinator_post_execution_error:OSError") == "unknown"
    assert normalize_legacy_operation_status("FAILED", "other") == "FAILED"
    assert normalize_legacy_operation_status("SUCCEEDED", "coordinator_post_execution_error") == "SUCCEEDED"


@dataclass
class _Run:
    manager: SubAgentManager
    task: SubAgentTask
    params: RecordRunnerResultParams
    result: SubAgentRunnerResult
    agent_run_id: str

    @property
    def repo(self):
        return self.manager.runtime_db

    def sweep(self):
        manager = self.manager
        return closeout.recover_pending_closeouts(
            self.repo, load_task=manager.load, save_task=manager.save,
            list_tasks=lambda: [manager.load(self.task.id)],
            notify_result=_completion_notifier(manager).notify_result,
        )

    def operation(self, effect: str, status: str, *, terminal: bool = True):
        ok = status == "SUCCEEDED"
        receipt = _result_payload(ToolHandlerOutcome(
            "run_command", ok, "", handler_executed=True,
            effect_outcome=effect if effect in {"failed", "not_started", "unknown"} else "",
        ))
        receipt["effect_outcome"] = effect
        with self.repo.transaction() as conn:
            conn.execute(
                "INSERT INTO tool_operations(operation_id, agent_run_id, attempt_id, attempt_generation, "
                "tool_operation_generation, operation_type, status, handler_started_at, settled_at, "
                "outcome_json, created_at, updated_at) VALUES('operation',?,?,1,1,'run_command',?,1,?,?,1,1)",
                (self.agent_run_id, self.params.attempt_id, status, int(terminal),
                 json.dumps({"schema": "managed_operation.v1", "result": receipt})),
            )


@pytest.fixture
def dead_run(tmp_path, monkeypatch):
    def make(status="DONE", *, keep_result=True):
        manager, _, task, attempt_id, agent_run_id, params, _ = _closeout_fixture(
            tmp_path, owner="test/m30",
        )
        repo = manager.runtime_db
        attempt = repo.get_attempt(attempt_id)
        metadata = json.loads(attempt["metadata_json"])
        original_pid = metadata["runner_pid"]
        monkeypatch.setattr(repository, "_proc_state", lambda pid: "dead" if pid == original_pid else "alive")
        with repo.transaction() as conn:
            conn.execute(
                "UPDATE agent_attempts SET status='running', ended_at=0 WHERE attempt_id=?", (attempt_id,),
            )

        task.status = params.status = status
        params.ok = status == "DONE"
        params.turn_end_reason = {"DONE": "completed", "FAILED": "error", "CANCELLED": "aborted"}[status]
        result = SubAgentRunnerResult(
            run_id=task.id, dry_run=False, ok=params.ok, status=status,
            verification_status="UNVERIFIED", message="persisted runner result",
            turn_end_reason=params.turn_end_reason, created_at=attempt["started_at"] + 1,
        )
        _write_runner_result_files(task, result, {}, prompt="", response="")
        assert prepare_runner_result_commit(repo, task, params, result, save_task=manager.save) == "persisted"
        if not keep_result:
            Path(task.runner_result_json).unlink()
        assert repo.recover_stale_attempts() == [agent_run_id]
        assert repo.agent_run_for_run_id(task.id)["status"] == "unknown"
        return _Run(manager, task, params, result, agent_run_id)

    return make


@pytest.mark.parametrize(
    "status,effect,op_status,terminal,settles",
    [
        ("DONE", "", "", True, True),
        ("DONE", "confirmed", "SUCCEEDED", True, True),
        ("DONE", "not_started", "FAILED", True, True),
        ("FAILED", "failed", "FAILED", True, True),
        ("CANCELLED", "not_started", "FAILED", True, True),
        ("DONE", "unknown", "UNKNOWN", False, False),
        ("DONE", "partial", "UNKNOWN", False, False),
        ("DONE", "", "EXECUTING", False, False),
        ("DONE", "partial", "SUCCEEDED", True, False),
        ("FAILED", "unknown", "FAILED", True, False),
    ],
)
def test_replay_dead_runner_attempts(tmp_path, dead_run, status, effect, op_status, terminal, settles):
    run = dead_run(status)
    repo = run.repo
    attempt_id = run.params.attempt_id
    if op_status:
        run.operation(effect, op_status, terminal=terminal)
    before = repo.agent_run_for_run_id(run.task.id)
    attempt_before = repo.get_attempt(attempt_id)

    summary = run.sweep()

    authority = repo.agent_run_for_run_id(run.task.id)
    attempt = repo.get_attempt(attempt_id)
    fact = closeout.pending_closeout(run.manager.load(run.task.id))
    assert authority["agent_run_id"] == before["agent_run_id"]
    assert authority["current_attempt_id"] == attempt_id
    assert authority["updated_at"] == before["updated_at"]
    assert attempt["ended_at"] == attempt_before["ended_at"]
    assert attempt["metadata_json"] == attempt_before["metadata_json"]
    if settles:
        assert summary["runtime_closeouts_recovered"] == 1, fact
        assert authority["status"] == attempt["status"] == status.lower()
        assert fact is None
        run.sweep()
        # 既有取消入口负责受控取消通知，自然收口通知 DONE／FAILED。
        expected_wakes = 0 if status == "CANCELLED" else 1
        assert len(_wakes_for_attempt(tmp_path, run.task.id, attempt_id)) == expected_wakes
        events = repo.events_for_attempt(attempt_id)
        assert sum(event["event_type"] == "agent_run.completed" for event in events) == 1
    else:
        assert summary["runtime_closeouts_pending"] == 1
        assert fact["closeout_state"] == "waiting_for_evidence"
        assert type(fact["evidence_revision"]) is int
        assert authority["status"] == attempt["status"] == "unknown"
        assert _wakes_for_attempt(tmp_path, run.task.id, attempt_id) == []
    if op_status:
        with repo._runtime_connection() as conn:
            assert conn.execute("SELECT status FROM tool_operations").fetchone()[0] == op_status


def test_unchanged_evidence_never_retries_across_ten_windows(dead_run, monkeypatch):
    run = dead_run()
    run.operation("unknown", "UNKNOWN", terminal=False)
    original_settle = run.repo.settle_agent_run
    calls = []

    def settle(**kwargs):
        calls.append(kwargs["attempt_id"])
        return original_settle(**kwargs)

    monkeypatch.setattr(run.repo, "settle_agent_run", settle)
    now = [closeout.time.time()]
    monkeypatch.setattr(closeout.time, "time", lambda: now[0])
    run.sweep()
    fact = closeout.pending_closeout(run.manager.load(run.task.id))
    for _ in range(10):
        now[0] += closeout.CLOSEOUT_RETRY_BACKOFF_MAX_SECONDS + 1
        run.sweep()
    assert calls == [run.params.attempt_id]
    assert closeout.pending_closeout(run.manager.load(run.task.id)) == fact


def test_late_effect_revises_only_the_exact_waiting_attempt(tmp_path, dead_run, monkeypatch):
    run = dead_run()
    run.operation("unknown", "UNKNOWN", terminal=False)
    monkeypatch.setattr(
        ManagedOperationStore, "claim_tool_operation", lambda *a, **kw: pytest.fail("replayed operation"),
    )
    assert run.sweep()["runtime_closeouts_pending"] == 1
    fact = closeout.pending_closeout(run.manager.load(run.task.id))
    assert fact["evidence_kind"] == "tool_operation"

    with monkeypatch.context() as blocked:
        blocked.setattr(closeout, "_load_persisted_runner_result", lambda *_: pytest.fail("unchanged receipt reread"))
        run.repo.append_event(
            event_type="fixture.unrelated", agent_run_id=run.agent_run_id, attempt_id=run.params.attempt_id,
        )
        assert run.sweep()["runtime_closeouts_pending"] == 1
    assert closeout.pending_closeout(run.manager.load(run.task.id)) == fact

    receipt = _result_payload(ToolHandlerOutcome("run_command", True, "", handler_executed=True))
    with run.repo.transaction() as conn:
        # 固定 updated_at 以验证真实状态变化也能触发，而不依赖时钟前进。
        conn.execute(
            "UPDATE tool_operations SET status='SUCCEEDED', settled_at=1, outcome_json=? "
            "WHERE operation_id='operation'",
            (json.dumps({"schema": "managed_operation.v1", "result": receipt}),),
        )
    assert run.sweep()["runtime_closeouts_recovered"] == 1
    assert len(_wakes_for_attempt(tmp_path, run.task.id, run.params.attempt_id)) == 1


def test_result_revision_accepts_repaired_bytes_and_rejects_replaced_receipt(tmp_path, dead_run):
    run = dead_run(keep_result=False)
    assert run.sweep()["runtime_closeouts_pending"] == 1
    missing = closeout.pending_closeout(run.manager.load(run.task.id))
    assert missing["evidence_kind"] == "runner_result"

    other_receipt = replace(run.result, message="different runner result")
    _write_runner_result_files(run.task, other_receipt, {}, prompt="", response="")
    assert run.sweep()["runtime_closeouts_pending"] == 1
    replaced = closeout.pending_closeout(run.manager.load(run.task.id))
    assert replaced["evidence_revision"] != missing["evidence_revision"]
    assert _wakes_for_attempt(tmp_path, run.task.id, run.params.attempt_id) == []

    _write_runner_result_files(run.task, run.result, {}, prompt="", response="")
    assert run.sweep()["runtime_closeouts_recovered"] == 1
    assert len(_wakes_for_attempt(tmp_path, run.task.id, run.params.attempt_id)) == 1


def test_resource_waits_until_original_mutation_is_stable(dead_run):
    run = dead_run()
    scope = "fixture:artifact"
    run.repo.begin_mutation(canonical_scope=scope, attempt_id=run.params.attempt_id)
    assert run.sweep()["runtime_closeouts_pending"] == 1
    fact = closeout.pending_closeout(run.manager.load(run.task.id))
    assert fact["evidence_kind"] == "resource_mutation"
    assert run.repo.mutation_for_scope(scope)["state"] == "MUTATING"
    run.repo.mark_mutation_stable(scope)
    assert run.sweep()["runtime_closeouts_recovered"] == 1


def test_manual_recovery_resumes_evidence_wait_without_elapsed_time(dead_run, monkeypatch):
    run = dead_run()
    run.operation("unknown", "UNKNOWN", terminal=False)
    now = closeout.time.time()
    monkeypatch.setattr(closeout.time, "time", lambda: now)
    assert run.sweep()["runtime_closeouts_pending"] == 1
    assert run.repo.recover_attempt_unknown(
        run.params.attempt_id, operator="fixture", effect_disposition="recorded",
    )["recovered"]
    assert run.sweep()["runtime_closeouts_recovered"] == 1


def test_late_receipt_cas_rejects_newer_unknown_attempt(dead_run, monkeypatch):
    run = dead_run()
    entered = Event()
    resume = Event()
    original_settle = run.repo.settle_agent_run

    def settle(**kwargs):
        entered.set()
        assert resume.wait(5)
        return original_settle(**kwargs)

    monkeypatch.setattr(run.repo, "settle_agent_run", settle)
    with ThreadPoolExecutor(max_workers=1) as worker:
        replay = worker.submit(run.sweep)
        try:
            assert entered.wait(5)
            assert run.repo.recover_attempt_unknown(
                run.params.attempt_id, operator="fixture", effect_disposition="confirmed_noop",
            )["recovered"]
            newer = run.repo.create_attempt(run.agent_run_id)
            assert run.repo.recover_stale_attempts() == [run.agent_run_id]
        finally:
            resume.set()
        assert replay.result(timeout=5)["runtime_closeouts_rejected"] == 1
    authority = run.repo.agent_run_for_run_id(run.task.id)
    assert authority["current_attempt_id"] == newer["attempt_id"]
    assert authority["status"] == "unknown"
    assert run.repo.get_attempt(newer["attempt_id"])["status"] == "unknown"


def test_initial_commit_retains_wal_when_effects_uncertain(dead_run):
    run = dead_run()
    run.operation("unknown", "UNKNOWN", terminal=False)
    commit_runner_result(
        run.repo, run.task, run.params, run.result,
        prepared="persisted", save_task=run.manager.save,
        deliver_result=lambda: pytest.fail("notified before settlement"),
    )
    fact = closeout.pending_closeout(run.manager.load(run.task.id))
    assert fact["closeout_state"] == "waiting_for_evidence"


def test_legacy_closeout_reads_durable_exit_without_rewriting_result(dead_run):
    run = dead_run()
    # 模拟旧版 WAL 和 attempt 元数据，其退出证据来自已有执行器记录。
    with run.repo.transaction() as conn:
        attempt = run.repo.get_attempt(run.params.attempt_id)
        metadata = json.loads(attempt["metadata_json"])
        del metadata["runner_exit_observed_at"]
        metadata["executor"] = {
            "schema_version": "attempt-executor.v1", "status": "exited", "ended_at": attempt["ended_at"],
        }
        conn.execute(
            "UPDATE agent_attempts SET metadata_json=? WHERE attempt_id=?",
            (json.dumps(metadata), run.params.attempt_id),
        )
    task = run.manager.load(run.task.id)
    del task.attributes[closeout.RUNTIME_CLOSEOUT_ATTR]["runner_result_digest"]
    run.manager.save(task)
    assert run.sweep()["runtime_closeouts_recovered"] == 1
    assert json.loads(run.repo.get_attempt(run.params.attempt_id)["metadata_json"]) == metadata


def test_known_failure_does_not_hide_a_later_uncertain_operation(dead_run):
    run = dead_run()
    run.operation("failed", "FAILED")
    with run.repo.transaction() as conn:
        conn.execute(
            "INSERT INTO tool_operations(operation_id, agent_run_id, attempt_id, attempt_generation, "
            "tool_operation_generation, operation_type, status, handler_started_at, created_at, updated_at) "
            "VALUES('later',?,?,1,2,'run_command','EXECUTING',2,2,2)",
            (run.agent_run_id, run.params.attempt_id),
        )
    assert run.repo.classify_attempt_closeout(run.params.attempt_id) is None


def test_exit_revision_moves_when_process_observation_changes(dead_run, monkeypatch):
    run = dead_run()
    attempt = run.repo.get_attempt(run.params.attempt_id)
    metadata = json.loads(attempt["metadata_json"])
    del metadata["runner_exit_observed_at"]
    with run.repo.transaction() as conn:
        conn.execute(
            "UPDATE agent_attempts SET metadata_json=? WHERE attempt_id=?",
            (json.dumps(metadata), run.params.attempt_id),
        )
    exited = False
    monkeypatch.setattr(repository, "executor_exit_reason", lambda _: "executor_process_died" if exited else "")
    assert run.sweep()["runtime_closeouts_pending"] == 1
    assert closeout.pending_closeout(run.manager.load(run.task.id))["evidence_kind"] == "executor_exit"
    exited = True
    assert run.sweep()["runtime_closeouts_recovered"] == 1


def test_wait_keeps_exact_run_unsettled_when_authority_disappears(tmp_path, dead_run):
    run = dead_run()
    run.operation("unknown", "UNKNOWN", terminal=False)
    assert run.sweep()["runtime_closeouts_pending"] == 1
    fact = closeout.pending_closeout(run.manager.load(run.task.id))
    with run.repo.transaction() as conn:
        conn.execute("DELETE FROM agent_runs WHERE agent_run_id=?", (run.agent_run_id,))
    assert run.sweep()["runtime_closeouts_pending"] == 1
    assert closeout.pending_closeout(run.manager.load(run.task.id)) == fact
    assert _wakes_for_attempt(tmp_path, run.task.id, run.params.attempt_id) == []
