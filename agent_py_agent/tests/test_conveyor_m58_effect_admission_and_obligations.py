"""M58 使用真实工作流账本和空间目录验证准入及未结义务。"""
from __future__ import annotations

import json
import os
import time
import uuid
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.gateway_parts.business_host_ports import (
    build_default_business_active_references,
)
from agent_py_agent.agent.workflows.engine import WorkflowEngine
from agent_py_agent.agent.workflows.governance import (
    WorkflowEffectPhase,
    WorkflowOperationRecovery,
    WorkflowReconciliationRequired,
)
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


def _definitive_failure_engine(tmp_path, calls):
    from agent_py_agent.tests.test_wf_01_02_tool_and_agent_steps import MockTool, MockToolRegistry

    def execute(arguments, **_kwargs):
        calls.append(arguments)
        return {"ok": False, "effect_outcome": "failed", "handler_executed": True,
                "error_code": "WORKFLOW_TOOL_EXECUTION_FAILED", "error_message": "deterministic error case"}

    engine = WorkflowEngine(tmp_path)
    engine.tool_registry = MockToolRegistry({"deterministic_failure": MockTool("deterministic_failure", execute)})
    return engine


def _child_failed_receipt(*, engine, run, workflow, step, spec, context, outputs):
    # Shape written by subworkflow_steps.apply_child_result for a settled failed child run.
    step.escalation_result["subworkflow_receipt"] = {
        "child_workflow_id": "child-flow", "child_run_id": f"{run.workflow_run_id}-child", "status": "failed",
    }
    step.status, step.needs_repair = "failed", True
    step.error_code, step.error_message = "SUBWORKFLOW_RUN_FAILED", "child run failed"


@pytest.mark.parametrize("mode", ["tool_call", "subworkflow"])
@pytest.mark.parametrize("execution_mode", ["sequential", "dag"])
def test_definitive_receipt_settles_identically_on_sequential_and_dag_paths(tmp_path, mode, execution_mode):
    from agent_py_agent.agent.workflows.run_store import (
        _concurrency_settled,
        _is_blocking_workflow_run,
    )

    calls = []
    if mode == "tool_call":
        engine = _definitive_failure_engine(tmp_path, calls)
        spec = {"step_id": "act", "mode": "tool_call", "tool_spec": {"tool_name": "deterministic_failure"}}
    else:
        engine = WorkflowEngine(tmp_path, subworkflow_step_executor=_child_failed_receipt)
        spec = {"step_id": "act", "mode": "subworkflow", "subworkflow_ref": "child-flow"}
    options = {"execution_mode": "dag"} if execution_mode == "dag" else {}
    run = engine.run_workflow(_flow([spec], **options))
    saved = engine.store.load_run(run.workflow_run_id)
    facts = saved.steps[0].escalation_result
    assert saved.status == "failed" and saved.steps[0].status == "failed"
    assert facts["effect_started"] is True
    assert facts.get("effect_state") != "unknown" and facts.get("effect_outcome") != "unknown"
    assert saved.effect_outcome == "failed"
    assert _concurrency_settled(saved)
    assert not _is_blocking_workflow_run(saved)
    if mode == "tool_call":
        assert len(calls) == 1 and facts["tool_receipt"]["effect_outcome"] == "failed"
    if execution_mode == "dag":
        assert facts["effect_state"] == "admitted" and facts["effect_outcome"] == "failed"


class _RetryableBeforeEffect(RuntimeError):
    error_code = "TOOL_TIMEOUT"
    effect_outcome = "not_started"


def _retry_in_worker(*, step, execute_fn, **_):
    # Host retry runner that replays a retryable failure inside the same DAG worker under a new attempt.
    step.attempt_count = 1
    try:
        execute_fn()
    except _RetryableBeforeEffect:
        step.status = "failed"
    if step.status == "failed":
        step.status, step.attempt_count = "running", 2
        step.escalation_result.update(attempt_id=uuid.uuid4().hex, effect_started=False)
        step.escalation_result.pop("effect_outcome", None)
        execute_fn()


@pytest.mark.parametrize("retry_path", ["coordinator", "in_worker"])
def test_interrupted_dag_retry_attempt_is_never_recorded_not_started(tmp_path, retry_path):
    entries = []

    def analyze(*_):
        entries.append(len(entries) + 1)
        if len(entries) == 1:
            raise _RetryableBeforeEffect("simulated retryable error before any effect")
        raise InterruptedError("simulated interrupt inside the second attempt")

    # The default host installs this read-only recovery gate when it resumes a run without review governance.
    engine = WorkflowEngine(tmp_path, analysis_handler=analyze,
                            run_authorizer=WorkflowOperationRecovery(None).require_effect,
                            step_retry_runner=_retry_in_worker if retry_path == "in_worker" else None)
    definition = _flow([{"step_id": "first", "mode": "llm_skill_analysis",
                         "retry_policy": {"max_attempts": 3, "retryable_codes": ["TOOL_TIMEOUT"]}}],
                       execution_mode="dag")
    with pytest.raises(InterruptedError) as caught:
        engine.run_workflow(definition)
    run = engine.store.load_run(caught.value.record.workflow_run_id)
    step = run.steps[0]
    facts = step.escalation_result
    assert entries == [1, 2] and step.attempt_count == 2
    assert facts["effect_state"] == "unknown" and facts["effect_outcome"] == "unknown"
    assert facts["effect_started"] is True and run.effect_outcome == "unknown"
    attempts = engine.store.run_dir(run.workflow_run_id) / "attempts"
    assert (attempts / f"first.{facts['attempt_id']}.admitted").is_file()
    assert len(list(attempts.glob("first.*.admitted"))) == 2
    with pytest.raises(WorkflowReconciliationRequired):
        engine.resume_workflow_run(run.workflow_run_id)
    assert entries == [1, 2]


_REFERENCE_TARGET = "reference_target"


def _session_task_reader(tmp_path, *, entries=None):
    from agent_py_agent.agent.conversation.session_tasks import SessionTaskStore
    from agent_py_agent.agent.conversation.store_layout import ConversationStorage
    from agent_py_agent.agent.workflows.reference_sources import WorkflowReferenceReader

    store = SessionTaskStore(ConversationStorage(tmp_path / "conversation"))
    host = SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=tmp_path))
    if entries is not None:
        host.config = SimpleNamespace(retention_scan_slice_entries=entries)
    return store, WorkflowReferenceReader(host, session_task_store=store)


def _put_task(store, task_id, *, status="done", created_at=1_000.0, refers=False, publish=False, **metadata):
    from agent_py_agent.agent.gateway_parts.io import write_json_file_atomic

    if refers:
        metadata["workflow_ref"] = f"workflow:{_REFERENCE_TARGET}"
    path = store.storage.session_tasks_dir / f"{task_id}.json"
    write_json_file_atomic(path, {"schema_version": "session_task.v1", "task_id": task_id, "status": status,
                                  "created_at": created_at, "updated_at": created_at, "metadata": metadata})
    if publish:
        store._list_index().publish(path)


def _settled_history(store, count, *, referencing_every=0):
    directory = store.storage.session_tasks_dir
    for index in range(count):
        refers = bool(referencing_every) and index % referencing_every == 0
        metadata = {"workflow_ref": f"workflow:{_REFERENCE_TARGET}"} if refers else {}
        (directory / f"settled-{index:05d}.json").write_text(json.dumps({
            "schema_version": "session_task.v1", "task_id": f"settled-{index:05d}", "status": "done",
            "created_at": 1_000.0 + index, "updated_at": 1_000.0 + index, "metadata": metadata}), encoding="utf-8")


def _reference_row(tmp_path):
    from agent_py_agent.agent.workflows.run_list_index import RunListIndex

    return RunListIndex(tmp_path).reference_cursor(f"session_tasks:{_REFERENCE_TARGET}")


def _check(reader):
    blockers = []
    return reader._check_session_tasks(_REFERENCE_TARGET, blockers), {row["ref"] for row in blockers}


def _counting_walk(store, monkeypatch):
    listed = []
    original = store.list_page

    def list_page(**kwargs):
        page = original(**kwargs)
        if kwargs.get("read_only"):
            listed[-1] += max(1, page["read_count"])
        return page

    monkeypatch.setattr(store, "list_page", list_page)
    return listed


def test_session_task_coverage_completes_over_successive_bounded_calls(tmp_path, monkeypatch):
    store, reader = _session_task_reader(tmp_path, entries=1_000)
    _settled_history(store, 10_001, referencing_every=1_000)
    listed = _counting_walk(store, monkeypatch)
    results = []
    while not results or not results[-1][0]:
        assert len(results) < 12, "coverage did not complete within ceil(10,001 / 1,000) + 1 calls"
        listed.append(0)
        results.append(_check(reader))
        if len(results) == 1:
            generation = _reference_row(tmp_path)[0]
        assert _reference_row(tmp_path)[0] == generation
    assert results[-1] == (True, set()) and not any(result for result, _ in results[:-1])
    assert len(results) > 1 and max(listed) <= 1_000 and sum(listed) >= 10_001
    listed.append(0)
    assert _check(reader) == (True, set()) and listed[-1] == 0


def test_open_reference_beyond_ten_thousand_tasks_is_found(tmp_path, monkeypatch):
    store, reader = _session_task_reader(tmp_path, entries=1_000)
    _settled_history(store, 10_050)
    last = [name for name in os.listdir(store.storage.session_tasks_dir) if name.endswith(".json")][-1]
    task_id = last.removesuffix(".json")
    _put_task(store, task_id, status="queued", created_at=1.0, refers=True)
    found = []
    for _ in range(13):
        result, refs = _check(reader)
        if task_id in refs:
            found.append(result)
            break
        assert not result
    assert found, "the open referencing task in the last directory position was never reported"


def test_task_appended_mid_cycle_is_covered_without_a_new_generation(tmp_path):
    store, reader = _session_task_reader(tmp_path)
    _settled_history(store, 300, referencing_every=50)
    assert _check(reader) == (False, set())
    generation = _reference_row(tmp_path)[0]
    _put_task(store, "appended-open", status="queued", created_at=time.time(), refers=True, publish=True)
    assert _check(reader) == (False, {"appended-open"})
    assert _reference_row(tmp_path)[0] == generation
    _put_task(store, "appended-open", status="done", created_at=time.time(), refers=True, publish=True)
    for _ in range(6):
        result, refs = _check(reader)
        assert not refs
        if result:
            break
    assert result and _reference_row(tmp_path)[0] == generation


def test_reopened_visited_task_restarts_coverage(tmp_path):
    store, reader = _session_task_reader(tmp_path)
    _settled_history(store, 300, referencing_every=50)
    for _ in range(6):
        result, _ = _check(reader)
        if result:
            break
    assert result
    generation = _reference_row(tmp_path)[0]
    _put_task(store, "settled-00050", refers=True, publish=True, task_learning_enqueue={"status": "pending"})
    assert _check(reader) == (False, {"settled-00050"})
    restarted = _reference_row(tmp_path)
    assert restarted[0] != generation
    assert json.loads(restarted[1])["walk"] is None and json.loads(restarted[1])["done"] is False
    _put_task(store, "settled-00050", refers=True, publish=True, task_learning_enqueue={"status": "done"})
    for _ in range(6):
        result, refs = _check(reader)
        assert not refs
        if result:
            break
    assert result


def test_concurrent_cursor_cas_loss_never_reports_complete(tmp_path, monkeypatch):
    from agent_py_agent.agent.workflows.run_list_index import RunListIndex

    store, reader = _session_task_reader(tmp_path)
    _settled_history(store, 300)
    original = RunListIndex.advance_reference_cursor
    lost = []

    def concurrent_winner(self, source_id, *, previous, generation, cursor, now):
        # A concurrent reader commits the same progress first, so this call loses the compare-and-swap.
        assert original(self, source_id, previous=previous, generation=generation, cursor=cursor, now=now)
        lost.append(original(self, source_id, previous=previous, generation=generation, cursor=cursor, now=now))
        return lost[-1]

    monkeypatch.setattr(RunListIndex, "advance_reference_cursor", concurrent_winner)
    outcomes = []
    for _ in range(6):
        before = len(lost)
        outcomes.append((_check(reader)[0], len(lost) > before))
    assert lost and not any(lost)
    assert any(attempted for _, attempted in outcomes)
    assert not any(result for result, attempted in outcomes if attempted)
    assert outcomes[-1] == (True, False)


def test_append_burst_beyond_one_slice_restarts_the_cycle(tmp_path):
    store, reader = _session_task_reader(tmp_path)
    _settled_history(store, 300)
    assert _check(reader) == (False, set())
    first = json.loads(_reference_row(tmp_path)[1])
    # A burst larger than one budget slice inside the append window; its open task is the oldest member.
    _put_task(store, "burst-open", status="queued", created_at=first["floor"] + 1e-5, refers=True, publish=True)
    for index in range(200):
        _put_task(store, f"burst-{index:03d}", created_at=first["floor"] + 1e-5 * (index + 2), publish=True)
    assert _check(reader) == (False, set())
    restarted = json.loads(_reference_row(tmp_path)[1])
    assert restarted["cycle"] != first["cycle"] and restarted["walk"] is None
    for _ in range(6):
        result, refs = _check(reader)
        if refs:
            break
        assert not result
    assert refs == {"burst-open"}
