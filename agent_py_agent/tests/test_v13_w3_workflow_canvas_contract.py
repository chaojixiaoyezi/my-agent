"""W3 画布、旧字节/执行引用及 XC-1 生产者回归；不代表 W2 HTTP 集成。"""

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.assets.descriptor import AssetDescriptor
from agent_py_agent.agent.assets.workflow_store import (
    WorkflowDefinition,
    WorkflowStepSpec,
    WorkflowStore,
    with_workflow_step_refs,
)
from agent_py_agent.agent.plugin_host.business_action_specs import business_defs
from agent_py_agent.agent.tooling.workflows_tool import WorkflowsTool
from agent_py_agent.agent.workflows.engine import WorkflowEngine
from agent_py_agent.agent.workflows.models import StepExecutionRecord
from agent_py_agent.agent.workflows.run_store import WorkflowRunStore


def golden():
    return WorkflowDefinition(
        AssetDescriptor("golden", "workflow", "Golden"),
        (
            WorkflowStepSpec("a", "A", "llm_skill_analysis"),
            WorkflowStepSpec("b", "B", "llm_skill_analysis"),
        ),
    )


def canvas():
    value = golden().to_dict()
    value.update(
        edges=[{"from_step_id": "a", "to_step_id": "b", "label": "输出"}],
        schedule_ids=["job-one"],
        datasource_ids=["source-one"],
    )
    value["steps"][0].update(
        description="先查询", next_step_ids=["b"], ui_position={"x": 1.5, "y": -2}
    )
    return value


def test_old_record_bytes_and_golden_execution_ref_unchanged():
    value = golden().to_dict()
    assert set(value) == {"descriptor", "steps", "body_markdown", "enabled"}
    assert set(value["steps"][0]) == {
        "step_id",
        "name",
        "mode",
        "script_id",
        "skill_ids",
        "skill_tags",
        "condition",
        "on_error",
        "inputs_map",
        "timeout_seconds",
        "config",
    }
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
    assert (
        json.dumps(
            WorkflowDefinition.from_any(json.loads(encoded)).to_dict(),
            sort_keys=True,
            separators=(",", ":"),
        )
        == encoded
    )
    # af7112871 未改生产代码时，通过原实现实际计算的金标准。
    assert (
        golden().execution_definition_ref
        == "4ab9117c649280432af4e8e03209b6309c3813e32259d2f27c05ebc7c23448d1"
    )


def test_canvas_roundtrip_store_and_display_only_ref(tmp_path):
    value = canvas()
    definition = WorkflowDefinition.from_any(value)
    assert definition.to_dict() == value
    assert definition.execution_definition_ref == golden().execution_definition_ref
    saved = WorkflowStore(tmp_path).save_workflow(definition)
    assert WorkflowStore(tmp_path).get_workflow("golden") == saved
    assert saved.edges == definition.edges and saved.datasource_ids == ("source-one",)
    changed = replace(
        definition, steps=(replace(definition.steps[0], timeout_seconds=10), definition.steps[1])
    )
    assert changed.execution_definition_ref != definition.execution_definition_ref
    run = WorkflowEngine(tmp_path, analysis_handler=lambda *args: {"value": 1}).run_workflow(
        "golden"
    )
    assert (
        run.status == "succeeded"
        and run.execution_definition_ref == definition.execution_definition_ref
    )


@pytest.mark.parametrize(
    "change",
    [
        {"next_step_ids": ["missing"]},
        {"next_step_ids": ["../bad"]},
        {"ui_position": {"z": 1}},
        {"ui_position": {"x": True}},
        {"ui_position": {"x": "1"}},
        {"ui_position": {"x": float("inf")}},
    ],
)
def test_invalid_canvas_step_rejected(change):
    value = golden().to_dict()
    value["steps"][0].update(change)
    with pytest.raises((ValueError, TypeError)):
        WorkflowDefinition.from_any(value)


@pytest.mark.parametrize(
    "edges",
    [
        [{"from_step_id": "a", "to_step_id": "missing"}],
        [{"from_step_id": "a", "to_step_id": "b", "unknown": 1}],
        [{"from_step_id": "a"}],
        [{"from_step_id": "a", "to_step_id": "b", "label": 3}],
    ],
)
def test_invalid_canvas_edges_rejected(edges):
    with pytest.raises((ValueError, TypeError)):
        WorkflowDefinition.from_any({**golden().to_dict(), "edges": edges})


def test_refs_aggregate_scripts_skills_sources_and_schedules():
    definition = replace(
        golden(),
        steps=(
            replace(golden().steps[0], script_id="one", skill_ids=("builtin:triage",)),
            replace(golden().steps[1], script_id="two", skill_ids=("owner:triage",)),
        ),
    )
    result = with_workflow_step_refs(
        definition,
        script_datasources={"one": ("a", "b"), "two": ("b", "c"), "unused": ("d",)},
        schedule_ids=("job-a", "job-a", "job-b"),
    )
    assert result.descriptor.refs.scripts == ("one", "two")
    assert result.descriptor.refs.skills == ("triage",)
    assert result.datasource_ids == ("a", "b", "c") and result.schedule_ids == ("job-a", "job-b")
    assert with_workflow_step_refs(result).datasource_ids == result.datasource_ids


def test_business_action_schema_accepts_optional_canvas_fields():
    from agent_py_agent.agent.plugin_host.schemas import validate_object

    defs = business_defs()
    value = canvas()
    value["descriptor"] = {
        "id": "golden",
        "kind": "workflow",
        "name": "Golden",
        "access_targets": [],
        "data_sources": [],
        "credential_refs": [],
        "io_contract": {},
    }
    assert validate_object(value, {"$defs": defs, "$ref": "#/$defs/WorkflowDefinition"}) == value


def test_step_record_legacy_and_canvas_projection():
    legacy = StepExecutionRecord(
        "a", "A", "llm_skill_analysis", "pending", 0, 0, 0, False
    ).to_dict()
    legacy.pop("description", None)
    legacy.pop("next_step_ids", None)
    assert StepExecutionRecord.from_dict(legacy).status == "pending"
    new = StepExecutionRecord.from_dict({**legacy, "description": "先查询", "next_step_ids": ["b"]})
    assert new.description == "先查询" and new.next_step_ids == ("b",)
    assert StepExecutionRecord.from_dict(new.to_dict()) == new


@pytest.mark.parametrize("sink_raises", [False, True])
def test_step_progress_typed_sink_order_and_isolation(tmp_path, sink_raises):
    events = []

    def observe(event):
        events.append(event)
        if sink_raises:
            raise RuntimeError("sink unavailable")

    run = WorkflowEngine(
        tmp_path, analysis_handler=lambda *args: {"value": 1}, step_observer=observe
    ).run_workflow(canvas())
    assert run.status == "succeeded"
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    for step_id in ("a", "b"):
        assert [e["status"] for e in events if e["step_id"] == step_id] == [
            "pending",
            "running",
            "succeeded",
        ]
    for event in events:
        assert set(event) == {
            "workflow_run_id",
            "workflow_id",
            "step_id",
            "name",
            "status",
            "duration_ms",
            "description",
            "next_step_ids",
            "seq",
        }
        assert event["workflow_run_id"] == run.workflow_run_id and event["workflow_id"] == "golden"
        assert type(event["duration_ms"]) is int and event["duration_ms"] >= 0
        assert event["description"] == ("先查询" if event["step_id"] == "a" else "")
    assert run.steps[0].description == "先查询" and run.steps[0].next_step_ids == ("b",)


def test_tool_connects_current_run_sink_without_text_fallback(tmp_path):
    WorkflowStore(tmp_path).save_workflow(golden())
    events, text = [], []

    class Sink:
        def write_workflow_step_progress(self, event):
            events.append(event)

        def __call__(self, value):
            text.append(value)

    current = RunParams(on_chunk=Sink())
    tool = WorkflowsTool(
        tmp_path, analysis_handler=lambda *args: {"value": 1}, run_params_provider=lambda: current
    )
    assert tool.execute({"action": "run", "workflow_id": "golden"}).ok
    assert len(events) == 6 and not text
    for step_id in ('a', 'b'):
        assert [event['status'] for event in events if event['step_id'] == step_id] == ['pending', 'running', 'succeeded']
    current.on_chunk = lambda value: text.append(value)
    assert tool.execute({"action": "run", "workflow_id": "golden"}).ok
    assert len(events) == 6 and not text
    records = WorkflowRunStore(tmp_path).list_runs(limit=10)
    assert len(records) == 2 and all(record.status == 'succeeded' for record in records)


def test_tool_save_aggregates_real_script_bindings_and_schedule_listing(tmp_path):
    from agent_py_agent.agent.assets.descriptor import DataSourceSpec
    from agent_py_agent.agent.assets.script_store import ScriptStore

    scripts = ScriptStore(tmp_path)
    for name, sources in [("one", ("alpha", "beta")), ("two", ("beta", "gamma"))]:
        scripts.save_script(
            AssetDescriptor(
                name,
                "script",
                name,
                data_sources=tuple(DataSourceSpec(source, "fixture") for source in sources),
            ),
            'print("{}")',
        )
    definition = replace(
        golden(),
        steps=(
            replace(golden().steps[0], script_id="one"),
            replace(golden().steps[1], script_id="two"),
        ),
    )
    tool = WorkflowsTool(tmp_path, schedule_ids_provider=lambda identifier: ("job-one", "job-one"))
    outcome = tool.execute(
        {"action": "save", "workflow_id": "golden", "definition": definition.to_dict()}
    )
    assert outcome.ok
    saved = WorkflowStore(tmp_path).get_workflow("golden")
    assert saved.datasource_ids == ("alpha", "beta", "gamma") and saved.schedule_ids == ("job-one",)


def test_resume_progress_sequence_is_monotonic_for_same_run(tmp_path):
    events = []
    state = {"fail": True}

    def analyze(spec, *args):
        if spec["step_id"] == "b" and state["fail"]:
            raise ValueError("fixture failure")
        return {"value": 1}

    engine = WorkflowEngine(tmp_path, analysis_handler=analyze, step_observer=events.append)
    failed = engine.run_workflow(golden())
    assert failed.status == "failed"
    previous_seq = events[-1]["seq"]
    state["fail"] = False
    resumed = engine.resume_workflow_run(failed.workflow_run_id)
    assert resumed.status == "succeeded"
    assert events[-1]["seq"] > previous_seq
    assert [e["seq"] for e in events] == sorted({e["seq"] for e in events})


def test_canvas_round_trip_in_workspace_overlay(tmp_path):
    from dataclasses import replace

    from agent_py_agent.agent.assets.descriptor import AssetDescriptor
    from agent_py_agent.agent.assets.workflow_store import (
        WorkflowDefinition,
        WorkflowStepSpec,
        WorkflowStore,
    )

    definition = WorkflowDefinition(
        AssetDescriptor("canvas-overlay", "workflow", "画布"),
        (
            WorkflowStepSpec("a", "A", "llm_skill_analysis", next_step_ids=("b",)),
            WorkflowStepSpec("b", "B", "llm_skill_analysis"),
        ),
        edges=({"from_step_id": "a", "to_step_id": "b"},),
        datasource_ids=("d",),
        schedule_ids=("j",),
    )
    owner, workspace = tmp_path / "owner", tmp_path / "workspace"
    workspace.mkdir()
    store = WorkflowStore(owner, workspace_root=workspace)
    store.save_workflow(definition, target_scope="workspace")
    got = store.get_workflow("canvas-overlay")
    assert (
        got.edges == definition.edges
        and got.datasource_ids == ("d",)
        and got.schedule_ids == ("j",)
    )
    assert store.list_workflows()[0].edges == definition.edges
    assert (
        replace(got, descriptor=definition.descriptor).execution_definition_ref
        == definition.execution_definition_ref
    )


# LLM: 活动调度任务是工作流真实依赖，更新时传当前CAS；删除任务后仍验证无依赖保存路径，不放宽生产门。
# 函数用途: 核对工作流保存读取canonical调度引用并排除已删除任务。
def test_tool_save_reads_canonical_scheduler_job_id_and_excludes_deleted(tmp_path):
    from agent_py_agent.tests.test_plan_w8_schedule_target import asset_case

    case = asset_case(tmp_path, "workflow")
    home = case.agent.home_paths.owner_home_dir
    tool = WorkflowsTool(home)
    definition = case.workflows.get_workflow("scheduled_workflow").to_dict()
    saved = tool.execute(
        {"action": "save", "workflow_id": "scheduled_workflow", "definition": definition,
         "expected_revision": definition["descriptor"]["revision"]}
    )
    assert saved.ok, saved.output
    assert WorkflowStore(home).get_workflow("scheduled_workflow").schedule_ids == (
        case.job["job_id"],
    )
    deleted = case.tool.execute(
        {"action": "delete", "job_id": case.job["job_id"], "expected_version": case.job["version"]}
    )
    assert deleted.ok, deleted.output
    saved = tool.execute(
        {"action": "save", "workflow_id": "scheduled_workflow", "definition": definition}
    )
    assert saved.ok, saved.output
    assert WorkflowStore(home).get_workflow("scheduled_workflow").schedule_ids == ()


def test_patched_step_recovery_emits_success_without_reexecuting_prefix(tmp_path):
    events = []
    calls = []

    def analyze(spec, *args):
        calls.append(spec["step_id"])
        if spec["step_id"] == "a":
            raise ValueError("repair me")
        return {"value": 1}

    engine = WorkflowEngine(tmp_path, analysis_handler=analyze, step_observer=events.append)
    failed = engine.run_workflow(golden())
    boundary = len(events)
    resumed = engine.resume_workflow_run(
        failed.workflow_run_id, patched_step_output={"value": 2}, resolution_origin="user"
    )
    assert resumed.status == "succeeded"
    assert calls == ["a", "b"]
    assert [(e["step_id"], e["status"]) for e in events[boundary:]][0] == ("a", "succeeded")


@pytest.mark.parametrize("completion_status", ["completed", "failed"])
def test_native_agent_completion_emits_terminal_progress(tmp_path, completion_status):
    events = []

    class Port:
        def submit(self, run, spec, step):
            step.escalation_result["task_id"] = "task-one"

        def completion(self, run, step):
            return {
                "status": completion_status,
                "output": {"value": 1},
                "completion_ref": "done-one",
            }

    definition = WorkflowDefinition(
        AssetDescriptor("native", "workflow", "原生步骤"),
        (WorkflowStepSpec("agent", "Agent", "agent_task", config={"instruction": "检查"}),),
    )
    engine = WorkflowEngine(tmp_path, agent_steps=Port(), step_observer=events.append)
    waiting = engine.run_workflow(definition)
    assert waiting.status == "waiting_for_agent"
    previous_seq = events[-1]["seq"]
    resumed = engine.resume_workflow_run(waiting.workflow_run_id)
    expected = "succeeded" if completion_status == "completed" else "failed"
    assert resumed.status == expected
    assert events[-1]["step_id"] == "agent" and events[-1]["status"] == expected
    assert events[-1]["seq"] > previous_seq


def test_progress_sequence_is_durable_before_observer_and_execution(tmp_path):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from agent_py_agent.agent.workflows.run_store import WorkflowRunStore

    entered, release = threading.Event(), threading.Event()
    observed = []

    def observe(event):
        disk = WorkflowRunStore(tmp_path).load_run(event["workflow_run_id"])
        observed.append((disk.progress_seq, event["seq"]))

    def analyze(*args):
        entered.set()
        assert release.wait(5)
        return {"value": 1}

    engine = WorkflowEngine(tmp_path, analysis_handler=analyze, step_observer=observe)
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(engine.run_workflow, golden())
        try:
            assert entered.wait(3)
            # 在业务执行未完成的窗口，已经发出的pending/running序号也必须可从磁盘恢复。
            assert observed and all(disk == emitted for disk, emitted in observed)
            release.set()
            assert future.result(5).status == "succeeded"
        finally:
            release.set()
    assert all(disk == emitted for disk, emitted in observed)
