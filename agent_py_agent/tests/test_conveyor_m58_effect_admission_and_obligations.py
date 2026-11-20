"""M58 使用真实工作流账本和空间目录验证准入及未结义务。"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_py_agent.agent.gateway_parts.business_host_ports import (
    build_default_business_active_references,
)
from agent_py_agent.agent.workflows.engine import WorkflowEngine
from agent_py_agent.agent.workflows.governance import WorkflowEffectPhase
from agent_py_agent.agent.workflows.models import StepExecutionRecord, WorkflowRunRecord
from agent_py_agent.agent.workflows.run_store import WorkflowRunStore
from agent_py_agent.tests.test_business_v12_f2_spaces import World
from agent_py_agent.tests.test_wf_07_08_foreach_and_dag import _flow


def test_interrupt_before_admission_is_replayable(tmp_path):
    calls = []
    interrupted = True

    def authorize(_run, phase):
        if isinstance(phase, WorkflowEffectPhase) and interrupted:
            raise InterruptedError("simulated interrupt at effect authorization")

    engine = WorkflowEngine(tmp_path, run_authorizer=authorize,
                            analysis_handler=lambda *_: calls.append("effect") or {"value": 7})
    definition = _flow([{"step_id": "first", "mode": "llm_skill_analysis"}], execution_mode="dag")
    with pytest.raises(InterruptedError) as caught:
        engine.run_workflow(definition)
    previous = engine.store.load_run(caught.value.record.workflow_run_id)
    assert not calls
    assert previous.steps[0].escalation_result.get("effect_outcome") == "not_started"
    assert previous.effect_outcome == "not_started"
    interrupted = False
    resumed = engine.resume_workflow_run(previous.workflow_run_id)
    assert resumed.status == "succeeded" and calls == ["effect"]


def test_archive_blocks_terminal_run_with_outstanding_refs(tmp_path):
    world = World(tmp_path)
    world.service.active_references = build_default_business_active_references(
        SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=tmp_path)),
    )
    space = world.create()
    step = StepExecutionRecord("first", "first", "tool_call", "succeeded", 1, 2, 0, False,
                               escalation_result={"outstanding_refs": ["work-receipt"]})
    run = WorkflowRunRecord("open-refs-run", "workflow", world.owner, "manual", "succeeded", 1, 2, 0,
                            steps=(step,), business_context={"space_id": space["space_id"]})
    WorkflowRunStore(tmp_path).save_run(run)
    result = world.call("spaces.archive", {"space_id": space["space_id"]},
                        revision=space["revision"], key="archive-with-open-reference")
    assert result["space"]["state"] == "active"
    assert result["blockers"][0]["ref"] == run.workflow_run_id

def test_interrupt_after_marker_keeps_unknown(tmp_path):
    seen = []
    engine = WorkflowEngine(tmp_path)

    def analyze(*_):
        marker = next(engine.store.root.glob("*/attempts/first.*.admitted"))
        seen.append(marker)
        raise InterruptedError("simulated interrupt after admission")

    engine.analysis_handler = analyze
    definition = _flow([{"step_id": "first", "mode": "llm_skill_analysis"}], execution_mode="dag")
    with pytest.raises(InterruptedError) as caught:
        engine.run_workflow(definition)
    run = engine.store.load_run(caught.value.record.workflow_run_id)
    assert run.effect_outcome == "unknown"
    step = run.steps[0]
    assert step.escalation_result["effect_state"] == "unknown"
    assert step.escalation_result["effect_outcome"] == "unknown"
    assert seen == [engine.store.run_dir(run.workflow_run_id) / "attempts" /
                    f"first.{step.escalation_result['attempt_id']}.admitted"]
    assert seen[0].read_bytes() == b""


def test_drain_exception_logs_and_persists_unknown(tmp_path, monkeypatch, caplog):
    from threading import Event

    from agent_py_agent.agent.workflows import dag_scheduler

    second_finished, first_published = Event(), Event()
    engine = WorkflowEngine(tmp_path, analysis_handler=lambda *_: {"value": 7})
    worker = dag_scheduler._run_step_worker
    save = engine.store.save_step_result

    def run(worker_engine, step, *args):
        if step.step_id == "first":
            assert second_finished.wait(5)
            step.status, step.needs_repair = "failed", True
            step.escalation_result.update(effect_state="not_started", effect_outcome="not_started")
            return step, None
        _result = worker(worker_engine, step, *args)
        second_finished.set()
        assert first_published.wait(5)
        raise OSError("simulated drain receipt error")

    def publish(run_id, step, *, resuming=False):
        result = save(run_id, step, resuming=resuming)
        if step.step_id == "first" and step.status == "failed":
            first_published.set()
        return result

    monkeypatch.setattr(dag_scheduler, "_run_step_worker", run)
    monkeypatch.setattr(engine.store, "save_step_result", publish)
    definition = _flow([{"step_id": name, "mode": "llm_skill_analysis"} for name in ("first", "second")],
                       execution_mode="dag", max_parallel_steps=2)
    run_record = engine.run_workflow(definition)
    saved = engine.store.load_run(run_record.workflow_run_id)
    assert saved.status == "failed" and saved.effect_outcome == "unknown"
    assert saved.steps[1].escalation_result["effect_state"] == "unknown"
    assert saved.steps[1].escalation_result["drain_diagnostic"] == {
        "code": "WORKFLOW_EXECUTION_FAILED", "phase": "drain", "error_type": "OSError",
    }
    logged = [entry for entry in caplog.records if getattr(entry, "phase", None) == "drain"]
    assert len(logged) == 1
    assert (logged[0].error_code, logged[0].step_id, logged[0].effect_outcome) == (
        "WORKFLOW_EXECUTION_FAILED", "second", "unknown",
    )


def _settled_row(owner, space_id, run_id, *, refs=()):
    step = StepExecutionRecord("first", "first", "tool_call", "succeeded", 1, 2, 0, False,
                               escalation_result={"outstanding_refs": list(refs)})
    return WorkflowRunRecord(run_id, "workflow", owner, "manual", "succeeded", 1, 2, 0,
                             steps=(step,), business_context={"space_id": space_id})


def _seed_run(directory, run):
    import json

    path = directory / run.workflow_run_id
    path.mkdir(parents=True)
    (path / "run.json").write_text(json.dumps(run.to_dict()))


def test_archive_resumes_across_a_large_settled_history(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation.business_access import BusinessError

    world = World(tmp_path)
    space = world.create()
    sid = space["space_id"]
    count, entries = 165, 35
    root = WorkflowRunStore(tmp_path).root
    for index in range(count):
        _seed_run(root, _settled_row(world.owner, sid, f"archive-fixture-{index:03}"))
    world.service.active_references = build_default_business_active_references(
        SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=tmp_path),
                        config=SimpleNamespace(retention_scan_slice_entries=entries, retention_scan_slice_ms=1000)),
    )
    visited = []
    original = WorkflowRunStore.list_page

    def observe(store, *, limit, cursor=None):
        page = original(store, limit=limit, cursor=cursor)
        assert 0 < limit <= entries
        visited.extend(item["workflow_run_id"] for item in page["items"])
        return page

    monkeypatch.setattr(WorkflowRunStore, "list_page", observe)
    progress = []
    for _ in range(10):
        try:
            result = world.call("spaces.archive", {"space_id": sid},
                                revision=space["revision"], key="archive-paged-history")
        except BusinessError as error:
            assert error.code == "HOST_NOT_READY"
            assert error.details["reason"] == "OPEN_OBLIGATION_SCAN_PENDING"
            progress.append(error.details["checked_entries"])
            assert world.call("spaces.get", {"space_id": sid})["space"]["state"] == "active"
        else:
            break
    assert result["space"]["state"] == "archived"
    assert progress == [35, 70, 105, 140]
    assert len(visited) == len(set(visited)) == count


def test_archive_commit_rechecks_source_version_after_scan(tmp_path):
    from agent_py_agent.agent.conversation.business_access import BusinessError

    world = World(tmp_path)
    space = world.create()
    scope = space["space_id"]
    reader = build_default_business_active_references(
        SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=tmp_path)),
    )

    def insert_after_scan(space_id, actor):
        rows = reader(space_id, actor)
        assert rows == []
        _seed_run(WorkflowRunStore(tmp_path).root, _settled_row(world.owner, scope, "late", refs=("owed",)))
        return rows

    insert_after_scan.source_versions = reader.source_versions
    world.service.active_references = insert_after_scan
    with pytest.raises(BusinessError) as caught:
        world.call("spaces.archive", {"space_id": scope}, revision=space["revision"], key="archive-source-change")
    assert caught.value.details["reason"] == "OPEN_OBLIGATION_SOURCE_CHANGED"
    assert world.call("spaces.get", {"space_id": scope})["space"]["state"] == "active"


@pytest.mark.parametrize("field", ["outstanding_refs", "effect_state"])
def test_terminal_store_predicates_preserve_open_facts(field):
    from agent_py_agent.agent.conversation.background_team_lifecycle import (
        has_open_obligation as identity_is_open,
    )
    from agent_py_agent.agent.conversation.managed_subscriptions import ManagedSubscriptionService
    from agent_py_agent.agent.conversation.session_tasks import SessionTaskStore

    fact = {field: ["owed"] if field == "outstanding_refs" else "unknown"}
    assert SessionTaskStore.has_open_obligation({"status": "done", **fact})
    assert WorkflowRunStore.has_open_obligation({"status": "succeeded", **fact})
    assert identity_is_open({"identity_state": "retired", **fact})
    assert ManagedSubscriptionService.has_open_obligation({"state": "removed", **fact})

@pytest.mark.parametrize("admitted", [False, True])
def test_paused_reservation_reads_durable_marker_without_rewriting(tmp_path, admitted):
    store = WorkflowRunStore(tmp_path)
    step = StepExecutionRecord("first", "first", "tool_call", "paused_for_resume", 1, 2, 0, False,
                               escalation_result={"attempt_id": "original", "effect_state": "reserved"})
    run = WorkflowRunRecord("restart-run", "workflow", "alice", "manual", "paused_for_resume", 1, 2, 0, steps=(step,))
    run.effect_outcome = "unknown"
    store.save_run(run)
    path = store.run_dir(run.workflow_run_id) / "run.json"
    original = path.read_bytes()
    if admitted:
        store.admit_step_effect(run.workflow_run_id, step)
        with pytest.raises(FileExistsError):
            store.admit_step_effect(run.workflow_run_id, step)

    snapshot = store.load_run(run.workflow_run_id)
    expected = "unknown" if admitted else "not_started"
    assert snapshot.steps[0].escalation_result["effect_outcome"] == expected
    assert snapshot.effect_outcome == expected and path.read_bytes() == original

def test_retirement_pages_resume_read_only_without_creating_projection(tmp_path, monkeypatch):
    import json

    from agent_py_agent.agent.conversation import ConversationStore
    from agent_py_agent.agent.conversation.background_team_lifecycle import _retirement_task_scan
    from agent_py_agent.agent.conversation.session_tasks import SessionTask

    tasks = ConversationStore(tmp_path / "conversations").session_tasks
    for number in range(23):
        task = SessionTask(f"history-{number:02}", "sender", "target", "fixture", status="done")
        tasks._path(task.task_id).write_text(json.dumps(task.to_dict()))
    index_path = tasks._list_index().path
    assert not index_path.exists()

    config = SimpleNamespace(retention_scan_slice_entries=5, retention_scan_slice_ms=1000)
    observed = []
    page = tasks.list_page

    def observe(**options):
        assert options["read_only"] is True and options["limit"] <= 5
        result = page(**options)
        observed.extend(task.task_id for task in result["items"])
        return result

    monkeypatch.setattr(tasks, "list_page", observe)
    for _ in range(10):
        task, complete, _version = _retirement_task_scan(tasks, "target", config)
        assert task is None
        if complete:
            break
    assert complete and len(observed) == len(set(observed)) == 23
    assert not index_path.exists()
