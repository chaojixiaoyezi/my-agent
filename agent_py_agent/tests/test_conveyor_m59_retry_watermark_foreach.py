"""M59 行为回归，使用实际运行账本验证重试、水位和并发。"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields
from heapq import heappop
from pathlib import Path
from threading import Barrier, Event, Lock, current_thread
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.assets.workflow_store import WorkflowStore
from agent_py_agent.agent.settings.config import AgentConfig, load_config
from agent_py_agent.agent.settings.parameter_registry import parameter_registry
from agent_py_agent.agent.workflows import batching, dag_scheduler, step_watermarks
from agent_py_agent.agent.workflows.engine import WorkflowEngine
from agent_py_agent.agent.workflows.models import StepExecutionRecord, WorkflowRunRecord
from agent_py_agent.agent.workflows.run_store import WorkflowRunStore
from agent_py_agent.agent.workflows.step_retry import (
    _is_transient_error,
    _retry_code_allowed,
    run_step_with_retry,
)
from agent_py_agent.tests.test_wf_07_08_foreach_and_dag import _flow


def _stored_step(tmp_path, *, status="running"):
    store = WorkflowRunStore(tmp_path)
    step = StepExecutionRecord("s", "Step", "llm_skill_analysis", status, 1, 0, 0, True,
                               escalation_result={"attempt_id": "original"})
    run = WorkflowRunRecord("m59-run", "m59-flow", "local/main", "manual", "running", 1, 0, 0,
                            steps=(step,))
    store.save_run(run)
    return store, run, step


def test_two_workers_can_consume_only_one_retry(tmp_path):
    store, run, _ = _stored_step(tmp_path)
    engine = SimpleNamespace(store=store, owner_home=tmp_path)
    both_failed = Barrier(2, timeout=5)

    def worker():
        step = store.load_run(run.workflow_run_id).steps[0]
        calls = 0

        def execute():
            nonlocal calls
            calls += 1
            if calls == 1:
                step.status = "failed"
                step.error_code = "TOOL_TIMEOUT"
                step.escalation_result["effect_outcome"] = "not_started"
                both_failed.wait()
            else:
                step.status = "succeeded"

        run_step_with_retry(engine=engine, step=step, spec={
            "retry_policy": {"max_attempts": 2, "retryable_codes": ["TOOL_TIMEOUT"]},
        }, outputs={}, definition={}, run=run, execute_fn=execute)
        return calls

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(worker) for _ in range(2)]
        assert sorted(future.result(timeout=10) for future in futures) == [1, 2]
    retry = store.load_run(run.workflow_run_id).steps[0].escalation_result["retry"]
    assert retry["consumed_budget"] == 1
    assert retry["replay_proof"] == "not_started"


def test_failed_watermark_ack_is_repairable(tmp_path, monkeypatch):
    store, run, step = _stored_step(tmp_path, status="succeeded")
    engine = SimpleNamespace(store=store, owner_home=tmp_path)
    step.watermark = {"cursor": 4}
    publish = step_watermarks.save_workflow_step_watermark

    def ack_lost(*args, **kwargs):
        publish(*args, **kwargs)
        raise OSError("simulated lost watermark ACK")

    monkeypatch.setattr(step_watermarks, "save_workflow_step_watermark", ack_lost)
    try:
        step_watermarks.commit_verified_watermark(engine=engine, run=run, step=step, spec={})
    except OSError:
        pass
    recovered = store.load_run(run.workflow_run_id).steps[0]
    assert recovered.escalation_result["watermark_publication"] == {
        "state": "repair", "value": {"cursor": 4},
    }
    monkeypatch.setattr(step_watermarks, "save_workflow_step_watermark", publish)
    step_watermarks.commit_verified_watermark(engine=engine, run=run, step=recovered, spec={})
    checkpoint = step_watermarks.load_workflow_step_watermark(tmp_path, run.workflow_id, step.step_id)
    assert checkpoint["revision"] == 1
    assert store.load_run(run.workflow_run_id).steps[0].escalation_result["watermark_publication"]["state"] == "published"


@pytest.mark.parametrize("after_publication", [False, True])
def test_resume_repairs_only_the_watermark(tmp_path, monkeypatch, after_publication):
    executions = []

    def analyze(*_):
        executions.append("effect")
        return {"watermark": {"cursor": 10}}

    engine = WorkflowEngine(tmp_path, analysis_handler=analyze)
    publish = step_watermarks.save_workflow_step_watermark

    def fail_publication(*args, **kwargs):
        if after_publication:
            publish(*args, **kwargs)
        raise OSError("simulated watermark publication I/O error")

    monkeypatch.setattr(step_watermarks, "save_workflow_step_watermark", fail_publication)
    workflow = _flow([{"step_id": "checkpoint", "mode": "llm_skill_analysis"}], execution_mode="dag")
    run = engine.run_workflow(workflow)
    assert run.status == "succeeded"
    saved = engine.store.load_run(run.workflow_run_id)
    assert saved.steps[0].escalation_result["watermark_publication"]["state"] == "repair"
    monkeypatch.setattr(step_watermarks, "save_workflow_step_watermark", publish)
    resumed = engine.resume_workflow_run(run.workflow_run_id)
    assert resumed.steps[0].escalation_result["watermark_publication"]["state"] == "published"
    checkpoint = step_watermarks.load_workflow_step_watermark(tmp_path, run.workflow_id, "checkpoint")
    assert checkpoint["revision"] == 1
    assert engine.resume_workflow_run(run.workflow_run_id).status == "succeeded"
    assert executions == ["effect"]


def _configure_ceiling(monkeypatch, maximum):
    configurer = getattr(batching, "configure_from_config", None)
    if configurer is not None:
        monkeypatch.setattr(batching, "_configured_max_parallel", batching.resolved_max_parallel())
        configurer(SimpleNamespace(workflow_foreach_max_parallel=maximum))


@pytest.mark.parametrize("spelling", ["max_parallel", "concurrency"])
def test_foreach_accepts_configured_ceiling(monkeypatch, spelling):
    _configure_ceiling(monkeypatch, 12)
    assert batching.validate_foreach({"from": "inputs.items", spelling: 12})["max_parallel"] == 12
    with pytest.raises(batching.ForeachInvalid, match="超出范围"):
        batching.validate_foreach({"from": "inputs.items", spelling: 13})


def test_dag_runs_twelve_ready_steps(tmp_path, monkeypatch):
    _configure_ceiling(monkeypatch, 12)
    active = 0
    peak = 0
    reached = Event()
    guard = Lock()

    def analyze(*_):
        nonlocal active, peak
        with guard:
            active += 1
            peak = max(peak, active)
            if peak == 12:
                reached.set()
        try:
            assert reached.wait(10), "all ready leaves must receive their configured slots"
            return {"value": 1}
        finally:
            with guard:
                active -= 1

    engine = WorkflowEngine(tmp_path, analysis_handler=analyze)
    definition = _flow([{"step_id": f"s{i}", "mode": "llm_skill_analysis"} for i in range(12)],
                       execution_mode="dag", max_parallel_steps=12)
    assert engine.run_workflow(definition).status == "succeeded"
    assert peak == 12


def test_typed_codes_ignore_substrings_and_use_tool_contract():
    assert _is_transient_error("TOOL_TIMEOUT")
    assert not _is_transient_error("PERMISSION_DENIED_WITH_TIMEOUT")
    assert not _is_transient_error("SCRIPT_TIMEOUT")
    assert not _retry_code_allowed(("SCRIPT_TIMEOUT",), "SCRIPT_TIMEOUT")


def test_dag_backoff_releases_its_only_worker(tmp_path, monkeypatch):
    monkeypatch.setattr(batching, "_configured_host_parallel", 1)
    seen = []
    def tool_step(engine, run, workflow, step, spec, context, outputs):
        seen.append((step.step_id, current_thread()))
        if len(seen) == 1:
            step.status, step.error_code = "failed", "TOOL_TIMEOUT"
            step.escalation_result["effect_outcome"] = "not_started"
        else:
            step.status = "succeeded"
            step.output_snapshot = {"value": 1}

    def no_worker_sleep(*_):
        pytest.fail("worker used the sleep hook")

    engine = WorkflowEngine(tmp_path, tool_step_executor=tool_step)
    monkeypatch.setattr(engine, "retry_sleep", no_worker_sleep, raising=False)
    workflow = _flow([
        {"step_id": "retry", "mode": "tool_call", "tool_spec": {"tool_name": "read_file"},
         "retry_policy": {"max_attempts": 2, "delay_seconds": 0.15}},
        {"step_id": "sibling", "mode": "tool_call", "tool_spec": {"tool_name": "read_file"}},
    ], execution_mode="dag", max_parallel_steps=1)
    result = engine.run_workflow(workflow)
    assert result.status == "succeeded"
    assert [step for step, _ in seen] == ["retry", "sibling", "retry"]
    assert len({thread for _, thread in seen}) == 1
    snapshot = engine.store.load_run(result.workflow_run_id)
    assert snapshot.steps[0].attempt_count == 2
    assert len(list((engine.store.run_dir(result.workflow_run_id) / "attempts").glob("retry.*.admitted"))) == 2


@pytest.mark.parametrize("dag", [True, False], ids=["dag", "sequential"])
def test_resume_retains_reserved_retry_attempt_and_budget(tmp_path, monkeypatch, dag):
    calls = []

    def execute(engine, run, workflow, step, spec, context, outputs):
        calls.append(step.escalation_result["attempt_id"])
        step.status, step.error_code = "failed", "TOOL_TIMEOUT"
        step.escalation_result["effect_outcome"] = "not_started"

    engine = WorkflowEngine(tmp_path, tool_step_executor=execute)

    def interrupted(*_):
        raise InterruptedError("simulated stop during backoff")

    workflow = _flow([
        {"step_id": "s", "mode": "tool_call", "tool_spec": {"tool_name": "read_file"},
         "retry_policy": {"max_attempts": 2, "delay_seconds": 0.2}},
    ], **({"execution_mode": "dag"} if dag else {}))
    from agent_py_agent.agent.workflows import step_retry

    with monkeypatch.context() as patch:
        patch.setattr(dag_scheduler if dag else step_retry, "wait_for_retry", interrupted)
        with pytest.raises(InterruptedError) as caught:
            engine.run_workflow(workflow)
    paused = engine.store.load_run(caught.value.record.workflow_run_id)
    assert paused.effect_outcome == "not_started"
    retry = paused.steps[0].escalation_result
    assert len(calls) == 1 and retry["retry"]["consumed_budget"] == 1
    assert retry["retry"]["state"] == "waiting"
    resumed = engine.resume_workflow_run(paused.workflow_run_id)
    assert resumed.steps[0].attempt_count == 2 and resumed.status == "failed"
    assert len(calls) == 2 and calls[1] == retry["attempt_id"]


def test_dag_edge_readiness_consumes_each_edge_once_per_pass():
    count = 1000
    edges_checked = 0

    class Edges(list):
        def __iter__(self):
            nonlocal edges_checked
            for edge in super().__iter__():
                edges_checked += 1
                yield edge

    deps = {sid: Edges([sid - 1] if sid else []) for sid in range(count)}
    reverse_deps = {sid: Edges([sid + 1] if sid + 1 < count else []) for sid in range(count)}
    ready = dag_scheduler._ReadySteps(deps, reverse_deps, dict.fromkeys(deps, "pending"))
    for expected in range(count):
        assert heappop(ready.ready)[1] == expected
        ready.complete(expected)
    assert not ready.ready
    assert edges_checked == 2 * (count - 1)


@pytest.mark.parametrize("spelling", ["max_parallel", "concurrency"])
def test_configured_foreach_loads_and_executes_with_both_spellings(tmp_path, monkeypatch, spelling):
    _configure_ceiling(monkeypatch, 12)
    ready = Barrier(12, timeout=5)

    def analyze(spec, context, skills):
        ready.wait()
        return {"items": context.step_inputs["items"]}

    workflow = _flow([
        {"step_id": "batches", "mode": "llm_skill_analysis",
         "foreach": {"from": "inputs.items", "batch_size": 1, spelling: 12, "mode": "aggregate"}},
    ])
    store = WorkflowStore(tmp_path)
    saved = store.save_workflow(workflow)
    loaded = store.get_workflow(saved.descriptor.id)
    result = WorkflowEngine(tmp_path, analysis_handler=analyze).run_workflow(loaded, {"items": list(range(12))})
    assert result.status == "succeeded" and result.final_output == {"items": list(range(12))}
    with pytest.raises(batching.ForeachInvalid, match="超出范围"):
        store.save_workflow(_flow([{"step_id": "batches", "mode": "llm_skill_analysis",
                                  "foreach": {"from": "inputs.items", spelling: 13}}]))
    _configure_ceiling(monkeypatch, 8)
    with pytest.raises(batching.ForeachInvalid, match="超出范围"):
        store.get_workflow(saved.descriptor.id)
    with pytest.raises(batching.ForeachInvalid, match="超出范围"):
        WorkflowEngine(tmp_path).run_workflow(loaded, {"items": []})


def test_deployment_registry_and_owner_pool_use_the_configured_ceiling(tmp_path, monkeypatch):
    from agent_py_agent.agent.owner_scoped_pool import OwnerScopedAgentPool

    key = "workflow_foreach_max_parallel"
    av = Path(__file__).resolve().parents[1]
    assert load_config(av / "config/agent_config.yaml").workflow_foreach_max_parallel == 8
    cfg = tmp_path / "agent_config.yaml"
    cfg.write_text(f"{key}: 12\n", encoding="utf-8")
    custom = load_config(cfg)
    parameter = next(row for row in fields(AgentConfig) if row.name == key)
    assert parameter.default == 8 and parameter.metadata["bounds"] == (1, None)
    assert parameter.metadata["unit"] == "个线程" and not parameter.metadata.get("model_read_only")
    spec = parameter_registry()[key]
    assert spec.tier == "deployment" and not spec.writable
    catalog = json.loads((av / "config/constants_catalog.json").read_text(encoding="utf-8"))
    assert any(entry["name"] == "DEFAULT_WORKFLOW_FOREACH_MAX_PARALLEL_COUNT" and entry["value"] == 8
               for entry in catalog["constants"])
    _configure_ceiling(monkeypatch, 8)
    OwnerScopedAgentPool(base_config=custom, root=tmp_path)
    assert batching.resolved_max_parallel() == 12
    assert batching.validate_foreach({"from": "inputs.items"})["max_parallel"] == 1


def test_unreadable_predecessor_recovers_to_the_next_revision(tmp_path, monkeypatch):
    executions = []
    def analyze(*_):
        executions.append(1)
        return {"watermark": {"cursor": 2}}

    flow = _flow([{"step_id": "checkpoint", "mode": "llm_skill_analysis"}], execution_mode="dag")
    engine = WorkflowEngine(tmp_path, analysis_handler=analyze)
    step_watermarks.save_workflow_step_watermark(tmp_path, "dag-flow", "checkpoint", {"cursor": 1})
    load = step_watermarks.load_workflow_step_watermark
    monkeypatch.setattr(step_watermarks, "load_workflow_step_watermark",
                        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("simulated baseline read error")))
    initial = engine.run_workflow(flow)
    assert initial.status == "succeeded"
    assert initial.steps[0].escalation_result["watermark_publication"]["state"] == "repair"

    monkeypatch.setattr(step_watermarks, "load_workflow_step_watermark", load)
    repaired = engine.resume_workflow_run(initial.workflow_run_id)
    engine.resume_workflow_run(initial.workflow_run_id)
    assert repaired.steps[0].escalation_result["watermark_publication"]["state"] == "published"
    checkpoint = load(tmp_path, "dag-flow", "checkpoint")
    assert (checkpoint["revision"], checkpoint["watermark"]) == (2, {"cursor": 2})
    assert executions == [1]


@pytest.mark.parametrize("dag", [False, True], ids=["sequential", "dag"])
@pytest.mark.parametrize("after_step_write", [False, True], ids=["before_step_write", "after_step_write"])
def test_run_ack_error_preserves_succeeded_attempt(tmp_path, monkeypatch, dag, after_step_write):
    executions = []
    def analyze(*_):
        executions.append(1)
        return {"watermark": {"cursor": 2}}

    engine = WorkflowEngine(tmp_path, analysis_handler=analyze)
    save = engine.store.save_step_watermark
    hit = []
    def lose_ack(*args, **kwargs):
        if not hit:
            hit.append(1)
            if after_step_write:
                def fail_run_write(root, parts, value):
                    if parts[-1] == "run.json":
                        raise OSError("simulated run ACK I/O error")
                    return write(root, parts, value)
                with monkeypatch.context() as fault:
                    fault.setattr(run_store_module, "_write_json", fail_run_write)
                    return save(*args, **kwargs)
            raise OSError("simulated step ACK I/O error")
        return save(*args, **kwargs)

    from agent_py_agent.agent.workflows import run_store as run_store_module
    write = run_store_module._write_json
    monkeypatch.setattr(engine.store, "save_step_watermark", lose_ack)
    flow = _flow([{"step_id": "checkpoint", "mode": "llm_skill_analysis"}],
                 **({"execution_mode": "dag"} if dag else {}))
    initial = engine.run_workflow(flow)
    assert initial.status == initial.steps[0].status == "succeeded"
    assert hit == [1]

    repaired = engine.resume_workflow_run(initial.workflow_run_id)
    assert repaired.steps[0].status == "succeeded"
    assert repaired.steps[0].escalation_result["watermark_publication"]["state"] == "published"
    assert step_watermarks.load_workflow_step_watermark(tmp_path, "dag-flow", "checkpoint")["revision"] == 1
    assert executions == [1]


def test_sequential_retry_parks_the_coordinator_outside_the_execution_worker(tmp_path, monkeypatch):
    monkeypatch.setattr(batching, "_configured_host_parallel", 1)
    started, resume = Event(), Event()
    attempts = []
    wait_threads = []

    def execute(engine, run, workflow, step, spec, context, outputs):
        attempts.append((current_thread(), step.escalation_result["attempt_id"]))
        if len(attempts) == 1:
            step.status, step.error_code = "failed", "TOOL_TIMEOUT"
            step.escalation_result["effect_outcome"] = "not_started"
        else:
            step.status = "succeeded"
            step.output_snapshot = {"value": 1}

    def pause(_seconds):
        wait_threads.append(current_thread())
        started.set()
        assert resume.wait(10)

    engine = WorkflowEngine(tmp_path / "retry", tool_step_executor=execute)
    monkeypatch.setattr(engine, "retry_sleep", pause, raising=False)
    workflow = _flow([{"step_id": "retry", "mode": "tool_call", "tool_spec": {"tool_name": "read_file"},
                       "retry_policy": {"max_attempts": 2, "delay_seconds": 60}}])
    other = WorkflowEngine(tmp_path / "other", analysis_handler=lambda *_: {"ready": True})
    independent = _flow([{"step_id": "other", "mode": "llm_skill_analysis"}], execution_mode="dag")
    with ThreadPoolExecutor(max_workers=2) as callers:
        queued = callers.submit(engine.run_workflow, workflow)
        try:
            assert started.wait(10)
            assert callers.submit(other.run_workflow, independent).result(timeout=5).status == "succeeded"
            assert attempts[0][0] is not wait_threads[0]
        finally:
            resume.set()
        assert queued.result(timeout=10).status == "succeeded"
    assert len(attempts) == len({attempt for _, attempt in attempts}) == 2


@pytest.mark.parametrize("prior", ["absent", "legacy", "competing"])
def test_baseline_recovery_preserves_predecessor_identity(tmp_path, monkeypatch, prior):
    engine = WorkflowEngine(tmp_path, analysis_handler=lambda *_: {"watermark": {"cursor": 2}})
    write = step_watermarks.save_workflow_step_watermark
    read = step_watermarks.load_workflow_step_watermark
    if prior == "legacy":
        path = tmp_path / "data" / "workflow_watermarks" / "dag-flow" / "w.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"watermark": {"cursor": 1}}))
    elif prior == "competing":
        write(tmp_path, "dag-flow", "w", {"cursor": 1})

    def unreadable(*args, **kwargs):
        raise OSError("simulated baseline read error")

    flow = _flow([{"step_id": "w", "mode": "llm_skill_analysis"}])
    with monkeypatch.context() as fault:
        fault.setattr(step_watermarks, "load_workflow_step_watermark", unreadable)
        initial = engine.run_workflow(flow)
    assert initial.steps[0].escalation_result["watermark_base_revision"] is None
    if prior == "competing":
        rival = write(tmp_path, "dag-flow", "w", {"cursor": 99}, run_id="competing-run",
                      step_attempt_id="competing-attempt", expected_revision=1)
    resumed = engine.resume_workflow_run(initial.workflow_run_id)
    assert resumed.status == resumed.steps[0].status == "succeeded"
    publication = resumed.steps[0].escalation_result["watermark_publication"]
    saved = read(tmp_path, "dag-flow", "w")
    if prior == "competing":
        assert saved == rival
        assert publication["state"] == "repair"
        assert publication["error_code"] == "WORKFLOW_RUN_REVISION_CONFLICT"
    else:
        assert (saved["revision"], saved["watermark"]) == (1, {"cursor": 2})
        assert publication["state"] == "published"
