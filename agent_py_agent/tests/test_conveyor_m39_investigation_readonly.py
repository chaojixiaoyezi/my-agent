from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.runtime import _run_params, _run_request
from agent_py_agent.agent.conversation.technical_continuation import USER_INPUT_YIELD_WAKE_REASON
from agent_py_agent.agent.scheduler.execution import with_scheduled_work
from agent_py_agent.agent.scheduler.postcheck import snapshot_execution_root
from agent_py_agent.agent.scheduler.repository import SchedulerJobCreateRequest
from agent_py_agent.agent.tooling.executor import ToolExecutor, ToolExecutorRequest
from agent_py_agent.agent.tooling.models import (
    EffectResolverPolicy,
    IdempotencyPolicy,
    ToolRuntimePolicy,
)
from agent_py_agent.tests.test_pp1118_w7b_failure_investigation import (
    _claim,
    _ScheduledDeps,
    _setup_service,
)
from agent_py_agent.tests.test_tool_runtime_unification import _call, _CountingTool, _snapshot


def _reserve(repository, **fields):
    job, _ = repository.create_job(SchedulerJobCreateRequest(
        name="scheduled work", prompt="prepare a report", thread_id="thread-1",
        schedule={"kind": "every", "every_seconds": 300, "anchor_at": 1000.0},
        now=1000.0, **fields,
    ))
    return repository.reserve_manual_run(job["job_id"], now=1010.0)


def _invoke(tmp_path, agent, store, row, reason, tool, value, *, approved=()):
    snapshot = _snapshot(tool, run_id=row["run_id"])
    agent.tools = SimpleNamespace(runtime_snapshot=lambda **kw: snapshot)
    request = _run_request({
        "thread_id": row["thread_id"], "task_id": row["run_id"], "reason": reason,
    })
    params = _run_params(
        request.thread_id, request, agent, thread=store.threads.require(request.thread_id),
    )
    return ToolExecutor().execute(ToolExecutorRequest(
        call=replace(_call(tool, {"value": value}), run_id=row["run_id"], operation_id="", idempotency_key=""),
        runtime_snapshot=snapshot,
        workspace_root=tmp_path,
        write_boundary={**(params.write_boundary or {}), **({"approved_actions": approved} if approved else {})},
        approval_mode="auto",
        operation_store_required=False,
    ))


@pytest.mark.parametrize("reason", ["scheduled_job_due", USER_INPUT_YIELD_WAKE_REASON])
def test_investigation_admits_reads_and_refuses_writes(tmp_path, reason, monkeypatch):
    from agent_py_agent.agent.conversation import host_actor

    monkeypatch.setattr(host_actor, "background_host_actor", lambda *_: None)
    _, store, repository, service, agent = _setup_service(tmp_path)
    agent.home_paths = SimpleNamespace(owner_id=repository.owner["id"])
    normal = _reserve(repository, policy={"on_failure": {"investigate": True, "after_failures": 1}})
    claim = _claim(repository, normal["run_id"], now=1011.0)
    service.finish(claim, status="failed", error_code="SCRIPT_EXIT_NONZERO", now=1012.0)
    investigation = next(row for row in repository.queued_runs() if row["trigger"] == "failure_investigation")
    tool = _CountingTool()
    tool.runtime_policy = ToolRuntimePolicy(
        effect_resolver=EffectResolverPolicy("read_only", by_parameter=(("value", (("save", "mutating"),)),)),
        idempotency_policy=IdempotencyPolicy("operation"),
    )

    assert _invoke(tmp_path, agent, store, investigation, reason, tool, "inspect").result.ok
    proposal = _invoke(tmp_path, agent, store, investigation, reason, tool, "save")
    assert tool.executions == 1
    assert proposal.result.effect_outcome == "not_started"
    assert proposal.result.error_code == "INVESTIGATION_READ_ONLY_REJECTED"
    assert proposal.decision.status == "ask"
    assert proposal.decision.approval_request["kind"] == "tool_action"
    replay = _invoke(
        tmp_path, agent, store, investigation, reason, tool, "save",
        approved=({**proposal.decision.approval_request, "status": "APPROVED"},),
    )
    assert replay.decision.status == "deny"
    assert replay.result.effect_outcome == "not_started"
    assert replay.result.error_code == "INVESTIGATION_READ_ONLY_REJECTED"
    assert tool.executions == 1
    assert repository.get_active_run(investigation["run_id"])["effect_policy"] == "read_only"


def test_legacy_postcheck_does_not_use_another_tree_on_thread_read_error(tmp_path, monkeypatch):
    _, store, repository, service, _ = _setup_service(tmp_path)
    run = _reserve(repository, postcheck={"artifacts": [{"path": "report.txt", "min_bytes": 1}]})
    claim = _claim(repository, run["run_id"], now=1011.0)
    fallback = repository.store_path.parent / "report.txt"
    fallback.parent.mkdir(parents=True, exist_ok=True)
    fallback.write_text("other report", encoding="utf-8")

    def unavailable(_thread_id):
        raise OSError("simulated thread read error")

    monkeypatch.setattr(store.threads, "load", unavailable)
    terminal = service.finish(claim, status="done", now=1012.0)
    assert terminal["status"] == "unknown"
    assert terminal["error_code"] == "POSTCHECK_ROOT_UNAVAILABLE"


def test_frozen_postcheck_root_survives_repeated_admission(tmp_path, monkeypatch):
    from agent_py_agent.agent.agent_core import tool_runtime_ledger
    from agent_py_agent.agent.conversation import required_user_action

    workspace, _, repository, service, agent = _setup_service(tmp_path)
    agent.home_paths = SimpleNamespace(owner_id=repository.owner["id"])
    run = _reserve(repository, postcheck={"artifacts": [{"path": "report.txt", "min_bytes": 1}]})
    claim = _claim(repository, run["run_id"], now=1011.0)
    (workspace / "report.txt").write_text("original", encoding="utf-8")

    def boundary(_agent, params):
        assert params.run_id == claim.run_id
        return {"execution_cwd": str(workspace)}

    def work(_kwargs):
        assert repository.get_active_run(claim.run_id)["execution_root_ref"] == frozen

    monkeypatch.setattr(tool_runtime_ledger, "write_boundary_with_runtime_ledger", boundary)
    monkeypatch.setattr(required_user_action, "required_notice", lambda *_: None)
    frozen = snapshot_execution_root(workspace)
    assert frozen == {
        "path": str(workspace.resolve()), "st_dev": workspace.stat().st_dev, "st_ino": workspace.stat().st_ino,
    }
    dependencies = _ScheduledDeps(run_once=work, terminal_task=lambda _: False)
    scheduled = with_scheduled_work(dependencies, agent, service, claim, None, now=1012.0)
    kwargs = {"thread_id": run["thread_id"], "task_id": run["run_id"], "reason": "scheduled_job_due"}
    scheduled.run_once(kwargs)

    workspace.rename(tmp_path / "original")
    workspace.mkdir()
    (workspace / "report.txt").write_text("replacement", encoding="utf-8")
    scheduled.run_once(kwargs)
    completed = service.finish(claim, status="done", now=1013.0)
    assert completed["status"] == "unknown"
    assert completed["error_code"] == "POSTCHECK_ROOT_UNAVAILABLE"
    assert completed["postcheck_observation"]["root_error"] == "root_identity_changed"


def test_legacy_explicit_postcheck_root_remains_readable(tmp_path: Path):
    workspace, _, repository, service, _ = _setup_service(tmp_path)
    (workspace / "report.txt").write_text("old result", encoding="utf-8")
    run = _reserve(repository, postcheck={"artifacts": [{"path": "report.txt", "min_bytes": 1}]})
    with repository._mutation_scope() as (ledger, admission):
        ledger["runs"][run["run_id"]]["execution"]["cwd"] = str(workspace)
        repository._write_store_unlocked(ledger, admission)
    claim = _claim(repository, run["run_id"], now=1011.0)
    completed = service.finish(claim, status="done", now=1012.0)
    assert completed["status"] == "done"
    assert completed["postcheck_observation"]["status"] == "passed"


def test_ordinary_schedule_keeps_mutating_tool(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation import host_actor

    monkeypatch.setattr(host_actor, "background_host_actor", lambda *_: None)
    _, store, repository, _, agent = _setup_service(tmp_path)
    agent.home_paths = SimpleNamespace(owner_id=repository.owner["id"])
    run = _reserve(repository)
    tool = _CountingTool()
    tool.runtime_policy = ToolRuntimePolicy(
        effect_resolver=EffectResolverPolicy("mutating"),
        idempotency_policy=IdempotencyPolicy("operation"),
    )
    executed = _invoke(tmp_path, agent, store, run, "scheduled_job_due", tool, "save")
    assert executed.result.ok
    assert tool.executions == 1


def test_investigation_rejects_an_unclassifiable_call(tmp_path, monkeypatch):
    from agent_py_agent.agent.tooling import action_policy

    tool = _CountingTool()

    def unavailable(_call, _runtime):
        raise ValueError("simulated classification error")

    monkeypatch.setattr(action_policy, "_resolved_effect", unavailable)
    outcome = ToolExecutor().execute(ToolExecutorRequest(
        call=_call(tool, {"value": "inspect"}),
        runtime_snapshot=_snapshot(tool),
        workspace_root=tmp_path,
        write_boundary={"effect_policy": "read_only"},
        operation_store_required=False,
    ))
    assert outcome.result.error_code == "INVESTIGATION_READ_ONLY_REJECTED"
    assert outcome.result.effect_outcome == "not_started"
    assert outcome.decision.evidence["classification_error"] == "ValueError"
    assert tool.executions == 0
