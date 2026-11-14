"""Wave 3 lossless consolidated tests (test_consolidated_c5_g2).

Sources:
- test_v13_w3_workflow_canvas_contract.py: W3 画布、旧字节/执行引用及 XC-1 生产者回归；不代表 W2 HTTP 集成。
- test_v13_w3_workflow_concurrency.py: 真实文件claim、Event交错与引擎快照；不使用sleep安排顺序。
- test_v13_w4_tui_activity_timer.py: W4：历史面板、回合计时和失败耗时不能互相污染。
- test_v13_w4_tui_queued_stream.py: W4：服务端观察任务不可回取，前台离开后观察流必须能继续显示。
- test_v13_w6_feishu_replay_window.py: 只使用合成数据的飞书时间窗与防重放测试。
- test_v13_w6_log_redaction_empty_user.py: 空用户名连接串：两条脱敏入口、实际日志组合和保留诊断信息。
- test_v13_w6_sqlite_index_contention.py: 真实SQLite短锁、WAL读者及按结构化错误码重试。
"""

import importlib
import io
import json
import logging
import multiprocessing
import os
import queue
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.adapter import feishu_crypto as crypto
from agent_py_agent.agent.adapter.feishu_replay_guard import FeishuReplayGuard
from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.assets import asset_root_lock
from agent_py_agent.agent.assets.descriptor import AssetDescriptor
from agent_py_agent.agent.assets.script_store import ScriptStore
from agent_py_agent.agent.assets.workflow_store import (
    WorkflowDefinition,
    WorkflowStepSpec,
    WorkflowStore,
    with_workflow_step_refs,
)
from agent_py_agent.agent.common.log_redaction import (
    RedactingFormatter,
    install_log_redaction,
    redact_sensitive_text,
)
from agent_py_agent.agent.conversation import task_locator
from agent_py_agent.agent.conversation.session_list_index import SessionListIndex
from agent_py_agent.agent.gateway_parts.request_client import GatewayAskExecutionOptions
from agent_py_agent.agent.plugin_host.business_action_specs import business_defs
from agent_py_agent.agent.tooling.workflows_tool import WorkflowsTool
from agent_py_agent.agent.user_space import home_indexes as indexes
from agent_py_agent.agent.workflows import concurrency_policy as policy
from agent_py_agent.agent.workflows.engine import WorkflowEngine
from agent_py_agent.agent.workflows.models import StepExecutionRecord
from agent_py_agent.agent.workflows.run_store import WorkflowRunStore
from agent_py_agent.cli.chat_parts.plain_state import ChatJob
from agent_py_agent.cli.chat_parts.tui_actions import _tui_attach_gateway_job
from agent_py_agent.cli.chat_parts.tui_input import restore_queued_prompts
from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime, TuiTurnSummary

# === Source: agent_py_agent/tests/test_v13_w3_workflow_canvas_contract.py ===

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


# === Source: agent_py_agent/tests/test_v13_w3_workflow_concurrency.py ===

def flow():
    return WorkflowDefinition(
        AssetDescriptor("shared", "workflow", "共享诊断"),
        (
            WorkflowStepSpec("a", "A", "llm_skill_analysis"),
            WorkflowStepSpec("b", "B", "llm_skill_analysis"),
        ),
    )


def test_dispatch_skip_queue_fifo_capacity_and_allow(tmp_path):
    entered, release = threading.Event(), threading.Event()
    second, release_second, third = threading.Event(), threading.Event(), threading.Event()
    order = []

    def first():
        order.append("a")
        entered.set()
        assert release.wait(10)
        return "first-result"

    def queued():
        order.append("b")
        second.set()
        assert release_second.wait(10)

    with ThreadPoolExecutor(1) as pool:
        running = pool.submit(policy.dispatch, tmp_path, "workflow", "shared", "a", "queue", first)
        try:
            assert entered.wait(5)
            skipped = policy.dispatch(
                tmp_path,
                "workflow",
                "shared",
                "skip",
                "skip_if_running",
                lambda: pytest.fail("skip executed"),
            )
            assert skipped == {
                "status": "skipped",
                "error_code": "WORKFLOW_RUN_SKIPPED_ALREADY_RUNNING",
                "running_run_id": "a",
            }
            assert (
                policy.dispatch(
                    tmp_path, "workflow", "shared", "allow", "allow", lambda: "parallel"
                )
                == "parallel"
            )
            queued_b = policy.dispatch(
                tmp_path, "workflow", "shared", "b", "queue", queued, queue_limit=2
            )
            queued_c = policy.dispatch(
                tmp_path,
                "workflow",
                "shared",
                "c",
                "queue",
                lambda: (order.append("c"), third.set()),
                queue_limit=2,
            )
            assert queued_b["status"] == queued_c["status"] == "queued"
            assert (queued_b["queue_position"], queued_c["queue_position"]) == (1, 2)
            full = policy.dispatch(
                tmp_path,
                "workflow",
                "shared",
                "d",
                "queue",
                lambda: pytest.fail("full executed"),
                queue_limit=2,
            )
            assert full["error_code"] == "WORKFLOW_RUN_QUEUE_FULL"
            assert not second.is_set() and not third.is_set()
            release.set()
            assert running.result(5) == "first-result"
            assert second.wait(5) and not third.is_set()
            release_second.set()
            assert third.wait(5)
            assert order == ["a", "b", "c"]
        finally:
            release.set()
            release_second.set()


@pytest.mark.parametrize("invalid", ["bad", "", None, 1, []])
def test_invalid_concurrency_policy_is_structured(tmp_path, invalid):
    result = policy.dispatch(
        tmp_path, "workflow", "shared", "bad", invalid, lambda: pytest.fail("invalid executed")
    )
    assert result["error_code"] == "INVALID_CONCURRENCY_POLICY"


def test_callback_exception_releases_claim(tmp_path):
    error = RuntimeError("fixture callback")

    def fail():
        raise error

    with pytest.raises(RuntimeError) as caught:
        policy.dispatch(tmp_path, "workflow", "shared", "one", "queue", fail)
    assert caught.value is error
    assert policy.dispatch(tmp_path, "workflow", "shared", "two", "queue", lambda: 8) == 8


def _die_holding_claim(home, entered):
    def die():
        entered.set()
        os._exit(0)

    policy.dispatch(home, "workflow", "shared", "dead", "queue", die)


def test_real_dead_process_claim_can_be_reclaimed(tmp_path):
    context = multiprocessing.get_context("spawn")
    entered = context.Event()
    process = context.Process(target=_die_holding_claim, args=(str(tmp_path), entered))
    process.start()
    process.join(10)
    assert entered.is_set() and process.exitcode == 0
    assert (
        policy.dispatch(tmp_path, "workflow", "shared", "alive", "queue", lambda: "reclaimed")
        == "reclaimed"
    )


def test_engine_event_interleaving_freezes_definition_and_queue(tmp_path):
    definition = flow()
    WorkflowStore(tmp_path).save_workflow(definition)
    entered, release, queued_done = threading.Event(), threading.Event(), threading.Event()
    active, maximum, results = 0, 0, []
    lock = threading.Lock()

    def analyze(spec, context, skills):
        nonlocal active, maximum
        if spec["step_id"] == "a":
            with lock:
                active += 1
                maximum = max(maximum, active)
            if context.run_inputs.get("first"):
                entered.set()
                assert release.wait(10)
        if spec["step_id"] == "b":
            with lock:
                active -= 1
            if not context.run_inputs.get("first"):
                queued_done.set()
        results.append(spec["name"])
        return {"value": 1}

    engine = WorkflowEngine(tmp_path, analysis_handler=analyze)
    with ThreadPoolExecutor(1) as pool:
        first = pool.submit(
            engine.run_workflow, "shared", {"first": True}, concurrency_policy="queue"
        )
        try:
            assert entered.wait(5)
            skip = engine.run_workflow("shared", concurrency_policy="skip_if_running")
            assert skip["error_code"] == "WORKFLOW_RUN_SKIPPED_ALREADY_RUNNING"
            queued = engine.run_workflow("shared", concurrency_policy="queue")
            assert queued["status"] == "queued"
            # 入队完成后编辑资产，两个run都只能消费入队前快照。
            WorkflowStore(tmp_path).save_workflow(
                replace(
                    definition,
                    steps=(definition.steps[0], replace(definition.steps[1], name="edited")),
                )
            )
            release.set()
            done = first.result(5)
            assert queued_done.wait(5)
            assert maximum == 1 and results == ["A", "B", "A", "B"]
            assert done.execution_definition_ref == definition.execution_definition_ref
        finally:
            release.set()


def test_tool_policy_is_in_schema_and_invalid_has_specific_code(tmp_path):
    WorkflowStore(tmp_path).save_workflow(flow())
    tool = WorkflowsTool(tmp_path, analysis_handler=lambda *args: {"value": 1})
    assert tool.model_spec.input_schema["properties"]["concurrency_policy"]["enum"] == [
        "allow",
        "skip_if_running",
        "queue",
    ]
    result = tool.execute(
        {"action": "run", "workflow_id": "shared", "concurrency_policy": "invalid"}
    )
    assert not result.ok and json.loads(result.output)["error_code"] == "INVALID_CONCURRENCY_POLICY"
    assert result.error_code == "TOOL_INVALID_ARGUMENTS"


def test_terminal_run_cannot_be_reversed_by_late_step_or_run(tmp_path):
    entered, release = threading.Event(), threading.Event()

    def analyze(*args):
        entered.set()
        assert release.wait(10)
        return {"value": 1}

    engine = WorkflowEngine(tmp_path, analysis_handler=analyze)
    with ThreadPoolExecutor(1) as pool:
        running = pool.submit(engine.run_workflow, flow())
        try:
            assert entered.wait(5)
            release.set()
            done = running.result(5)
            stale = replace(done.steps[-1], status="running")
            with pytest.raises(ValueError):
                engine.store.save_step_result(done.workflow_run_id, stale)
            reverse = replace(done, status="running")
            with pytest.raises(ValueError):
                engine.store.save_run(reverse)
            assert engine.store.load_run(done.workflow_run_id).status == "succeeded"
        finally:
            release.set()


@pytest.fixture
def store(tmp_path):
    # 合成owners祖先使默认全局发现也留在测试树内，不落到运行宿主的真实全局根。
    value = ScriptStore(tmp_path / "app" / "owners" / "local" / "sample")
    value.save_script(AssetDescriptor("sample", "script", "样本"), "print(1)")
    return value


# LLM: 主线程一直持有真实根锁直到写线程完成；宽等待只容纳调度/I/O，不容许等锁后通过。
# 函数用途: 验证非阻塞脚本摘要不依赖Owner根锁，显式fold仍只记一次。
def test_summary_write_does_not_wait_for_asset_lock(store):
    started = time.monotonic()
    with ThreadPoolExecutor(1) as pool:
        with asset_root_lock(store.owner_home):
            pool.submit(
                store.record_run_summary, "sample", "succeeded", 12,
                blocking=False, run_id="run-1",
            ).result(timeout=5)
            print(f"owner-lock-contention elapsed_ms={(time.monotonic() - started) * 1000:.3f}")
    result = store.fold_run_summaries("sample")
    assert result["runtime_summary"]["run_count"] == 1
    assert result["runtime_summary"]["last_duration_ms"] == 12
    store.record_run_summary("sample", "succeeded", 12, blocking=False, run_id="run-1")
    assert store.fold_run_summaries("sample")["runtime_summary"]["run_count"] == 1


# LLM: 新完成直接入聚合，旧输入才有spool ACK；保留提交后失败及重启去重的原断言。
# 函数用途: 用旧叶准备原迁移窗口，核对提交后ACK失败不会导致重复累计。
def test_commit_before_ack_replay_survives_new_store(store, monkeypatch):
    spool = importlib.import_module("agent_py_agent.agent.workflows.run_summary_spool")
    # 新完成没有spool ACK；此处沿原内部入口准备旧输入，保留迁移ACK重放的全部断言。
    spool.append(store.owner_home, "script", "sample",
                 dict(status="failed", duration_ms=4, run_id="durable"))
    unlink = spool.unlink_file_beneath

    def fail_ack(*args, **kwargs):
        raise PermissionError("ACK故障")

    monkeypatch.setattr(spool, "unlink_file_beneath", fail_ack)
    with pytest.raises(PermissionError):
        store.fold_run_summaries("sample")
    assert store.get_script("sample")["runtime_summary"]["run_count"] == 1
    monkeypatch.setattr(spool, "unlink_file_beneath", unlink)
    reopened = ScriptStore(store.owner_home)
    assert reopened.fold_run_summaries("sample")["runtime_summary"]["run_count"] == 1
    reopened.record_run_summary("sample", "failed", 4, blocking=True, run_id="durable")
    assert reopened.get_script("sample")["runtime_summary"]["run_count"] == 1


def test_failed_commit_leaves_pending(store, monkeypatch):
    store.record_run_summary("sample", "succeeded", 3, blocking=False, run_id="retry")
    write = store._write

    def fail(*args, **kwargs):
        raise OSError("写入故障")

    monkeypatch.setattr(store, "_write", fail)
    with pytest.raises(OSError):
        store.fold_run_summaries("sample")
    monkeypatch.setattr(store, "_write", write)
    assert store.fold_run_summaries("sample")["runtime_summary"]["run_count"] == 1


def test_append_during_fold_snapshot_is_not_lost(store, monkeypatch):
    store.record_run_summary("sample", "succeeded", 1, blocking=False, run_id="first")
    entered, release = threading.Event(), threading.Event()
    write = store._write

    def paused(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return write(*args, **kwargs)

    monkeypatch.setattr(store, "_write", paused)
    with ThreadPoolExecutor(max_workers=2) as pool:
        folding = pool.submit(store.fold_run_summaries, "sample")
        assert entered.wait(2)
        try:
            appending = pool.submit(
                store.record_run_summary, "sample", "succeeded", 2, blocking=False, run_id="late"
            )
            appending.result(timeout=1)
        finally:
            release.set()
        assert folding.result(timeout=3)["runtime_summary"]["run_count"] == 1
    monkeypatch.setattr(store, "_write", write)
    assert store.fold_run_summaries("sample")["runtime_summary"]["run_count"] == 2


def test_parallel_append_and_repeated_fold(store):
    def add(index):
        store.record_run_summary(
            "sample", "succeeded", index, blocking=False, run_id=f"run-{index}"
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(add, range(32)))
    assert store.fold_run_summaries("sample")["runtime_summary"]["run_count"] == 32
    assert store.fold_run_summaries("sample")["runtime_summary"]["run_count"] == 32


@pytest.mark.parametrize("asset_id", ["../escape", "/absolute", "a/b", ".."])
def test_spool_rejects_traversal(tmp_path, asset_id):
    spool = importlib.import_module("agent_py_agent.agent.workflows.run_summary_spool")
    with pytest.raises(ValueError):
        spool.append(
            tmp_path, "script", asset_id, {"run_id": "r", "status": "succeeded", "duration_ms": 1}
        )


def test_spool_rejects_symlink(store, tmp_path):
    spool = importlib.import_module("agent_py_agent.agent.workflows.run_summary_spool")
    outside = tmp_path / "outside"
    outside.mkdir()
    (store.owner_home / ".run-summary-spool").symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        spool.append(
            store.owner_home,
            "script",
            "sample",
            {"run_id": "r", "status": "succeeded", "duration_ms": 1},
        )
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("duration", [float("nan"), float("inf"), -1, True])
def test_invalid_record_is_not_queued(store, duration):
    with pytest.raises(ValueError):
        store.record_run_summary("sample", "succeeded", duration, blocking=False, run_id="bad")
    assert store.fold_run_summaries("sample")["runtime_summary"]["run_count"] == 0


def test_workspace_shadow_and_global_are_not_written(store, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    overlay = ScriptStore(store.owner_home, workspace_root=workspace)
    overlay.save_script(
        AssetDescriptor("sample", "script", "覆盖"), "print(2)", target_scope="workspace"
    )
    before = (store.owner_home / "scripts/sample/script.json").read_bytes()
    assert (
        overlay.record_run_summary("sample", "succeeded", 1, blocking=False)["scope_layer"]
        == "workspace"
    )
    assert (store.owner_home / "scripts/sample/script.json").read_bytes() == before
    assert not (store.owner_home / ".run-summary-spool").exists()
    global_root = tmp_path / "global"
    ScriptStore(global_root).save_script(AssetDescriptor("builtin", "script", "全局"), "print(3)")
    reader = ScriptStore(store.owner_home, extra_roots=(global_root,))
    assert (
        reader.record_run_summary("builtin", "succeeded", 1, blocking=False)["scope_layer"]
        == "global"
    )
    assert not (store.owner_home / ".run-summary-spool").exists()


def test_runner_nonblocking_and_bounded_redacted_warning(tmp_path, monkeypatch, caplog):
    from agent_py_agent.agent.workflows import script_runner

    calls = []

    class FakeStore:
        def __init__(self, owner_home):
            pass

        def record_run_summary(self, script_id, **kwargs):
            calls.append(kwargs)
            raise PermissionError("token=synthetic-secret " + "x" * 2000)

    monkeypatch.setattr(script_runner, "ScriptStore", FakeStore)
    result = SimpleNamespace(
        script_id="sample",
        ok=True,
        error_code="",
        stderr_preview="",
        duration_ms=1,
        started_at=1,
        ended_at=2,
    )
    with caplog.at_level("WARNING"):
        script_runner._record_summary(tmp_path, result)
        script_runner._record_summary(tmp_path, result)
    assert calls[0]["blocking"] is False
    assert calls[0]["run_id"] == calls[1]["run_id"]
    records = [row for row in caplog.records if "summary" in row.getMessage().lower()]
    assert len(records) == 1
    assert records[0].error_type == "PermissionError"
    assert "synthetic-secret" not in records[0].getMessage()
    assert len(records[0].getMessage()) < 800


def test_append_during_open_asset_transaction(store, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    store.record_run_summary("sample", "succeeded", 1, blocking=False, run_id="initial")
    put = store._put

    def paused(asset_id, filename, content):
        result = put(asset_id, filename, content)
        if filename == "asset-transaction.json" and content is not None:
            entered.set()
            assert release.wait(5)
        return result

    monkeypatch.setattr(store, "_put", paused)
    with ThreadPoolExecutor(max_workers=2) as pool:
        folding = pool.submit(store.fold_run_summaries, "sample")
        assert entered.wait(2)
        try:
            pool.submit(
                store.record_run_summary,
                "sample",
                "failed",
                2,
                blocking=False,
                run_id="during-transaction",
            ).result(timeout=1)
        finally:
            release.set()
        folding.result(timeout=3)
    monkeypatch.setattr(store, "_put", put)
    assert store.fold_run_summaries("sample")["runtime_summary"]["run_count"] == 2


def test_edit_does_not_erase_idempotency_receipt(store):
    store.record_run_summary("sample", "succeeded", 1, run_id="stable")
    store.save_script(AssetDescriptor("sample", "script", "编辑后"), "print(2)")
    store.record_run_summary("sample", "succeeded", 1, run_id="stable")
    assert store.get_script("sample")["runtime_summary"]["run_count"] == 1


# LLM: 只按实际生命周期定位旧输入目录，坏JSON和叶链接仍必须拒绝且不污染摘要。
# 函数用途: 保持旧输入损坏和真实链接的安全拒绝回归。
def test_spool_corrupt_record_and_leaf_symlink_fail_closed(store, tmp_path):
    spool = importlib.import_module("agent_py_agent.agent.workflows.run_summary_spool")
    spool.append(
        store.owner_home,
        "script",
        "sample",
        {"run_id": "r", "status": "succeeded", "duration_ms": 1},
    )
    # 只跟随内部spool生命周期分区，不改变损坏记录和链接拒绝断言。
    directory = store.owner_home.joinpath(*spool._parts("script", "sample",
        store._summary_snapshot("sample")["metadata"].get("_run_summary_generation", "")))
    leaf = next(directory.glob("*.json"))
    leaf.write_text("{bad json", encoding="utf-8")
    with pytest.raises(ValueError):
        store.fold_run_summaries("sample")
    leaf.write_text("{}", encoding="utf-8")
    outside = tmp_path / "not-a-record"
    outside.write_text("unchanged", encoding="utf-8")
    (directory / ("f" * 32 + ".json")).symlink_to(outside)
    # 先修复第一条，确保此断言观察的是链接而非前面的坏JSON。
    leaf.write_text(
        '{"run_id":"r","status":"succeeded","duration_ms":1,"run_at":"2026-11-02T00:00:00+00:00"}',
        encoding="utf-8",
    )
    with pytest.raises(OSError):
        store.fold_run_summaries("sample")
    assert outside.read_text(encoding="utf-8") == "unchanged"
    assert store.get_script("sample")["runtime_summary"]["run_count"] == 0


def test_spool_requires_root_lock_for_fold(store):
    spool = importlib.import_module("agent_py_agent.agent.workflows.run_summary_spool")
    from agent_py_agent.agent.assets.descriptor import RuntimeSummary

    with pytest.raises(RuntimeError):
        spool.fold(store.owner_home, "script", "sample", RuntimeSummary(), {}, lambda *args: None)


# LLM: 真实启动临时脚本，关闭内核沙箱不代表验证内核隔离；摘要明确通过正式fold收口。
# 函数用途: 验证执行器结果经spool到持久摘要的完整本地链路。
def test_real_script_runner_spools_then_folds(store):
    from agent_py_agent.agent.workflows import ScriptRunner, script_runner

    store.save_script(AssetDescriptor("sample", "script", "真实运行"), 'print("{\\"value\\": 7}")')
    result = ScriptRunner(store.owner_home, use_sandbox=False).run_script("sample")
    assert result.ok, (result.error_code, result.stderr_preview)
    assert result.output_data == {"value": 7}
    folded = store.fold_run_summaries("sample")["runtime_summary"]
    assert folded["run_count"] == 1 and folded["last_status"] == "succeeded"
    script_runner._record_summary(store.owner_home, result)
    assert store.fold_run_summaries("sample")["runtime_summary"]["run_count"] == 1
    print("real-script -> spool -> fold -> replay: succeeded")


# LLM: 子进程真正退出，不用内存缓存模拟重启；只在测试资产根注入中断点。
# 函数用途: 验证事务中断回滚与提交后ACK中断均可重放且最终只计一次。
@pytest.mark.parametrize("phase", ["journal", "ack"])
# LLM: 原journal/ACK退出边界仍属于旧spool迁移；真实子进程和重启断言不替换。
# 函数用途: 准备旧输入后在原两个持久化边界退出，核对重开不丢失、不双算。
def test_process_crash_and_reopen(store, phase):
    import os
    import subprocess
    import sys

    from agent_py_agent.agent.workflows import run_summary_spool
    # journal/ACK是旧输入迁移的两个边界；新完成事务的退出重放另有独立回归。
    run_summary_spool.append(store.owner_home, "script", "sample",
                             dict(status="succeeded", duration_ms=1, run_id="crash-run"))
    code = """import os, sys
from agent_py_agent.agent.assets.script_store import ScriptStore
from agent_py_agent.agent.workflows import run_summary_spool
store = ScriptStore(sys.argv[1])
if sys.argv[2] == 'journal':
    original = store._put
    def stop(asset_id, filename, content):
        original(asset_id, filename, content)
        if filename == 'asset-transaction.json' and content is not None:
            os._exit(17)
    store._put = stop
else:
    run_summary_spool.unlink_file_beneath = lambda *args: os._exit(17)
store.fold_run_summaries('sample')
"""
    crashed = subprocess.run(
        [sys.executable, "-c", code, str(store.owner_home), phase],
        env=dict(os.environ),
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert crashed.returncode == 17, crashed.stderr
    reopened = ScriptStore(store.owner_home)
    assert reopened.fold_run_summaries("sample")["runtime_summary"]["run_count"] == 1
    assert reopened.fold_run_summaries("sample")["runtime_summary"]["run_count"] == 1


# LLM: 与脚本摘要一样在主线程根锁未释放时等待真正完成，不能靠持锁线程超时解锁通过。
# 函数用途: 验证工作流摘要的非阻塞写入、显式fold及编辑后的幂等回执。
def test_workflow_summary_nonblocking_fold_and_edit_receipt(tmp_path):
    owner = tmp_path / "app/owners/local/test"
    store = WorkflowStore(owner)
    store.save_workflow(flow())
    with ThreadPoolExecutor(1) as pool:
        with asset_root_lock(owner):
            result = pool.submit(
                store.record_run_summary,
                "shared",
                "succeeded",
                12,
                blocking=False,
                run_id="workflow-stable",
            ).result(timeout=5)
            assert result.descriptor.runtime_summary.run_count == 0
    assert store.fold_run_summaries("shared").descriptor.runtime_summary.run_count == 1
    store.save_workflow(flow())
    store.record_run_summary("shared", "succeeded", 12, blocking=False, run_id="workflow-stable")
    assert store.fold_run_summaries("shared").descriptor.runtime_summary.run_count == 1


@pytest.mark.parametrize("action", ["workflows.run", "workflow_runs.resume"])
@pytest.mark.parametrize("strategy", ["skip_if_running", "queue"])
def test_business_entry_preserves_typed_receipts_under_overlap(tmp_path, action, strategy):
    from agent_py_agent.agent.common.cancellation import (
        CancellationToken,
        ToolCancelled,
        bind_cancellation_token,
    )
    from agent_py_agent.tests.test_business_v12_f4_workflows import service

    api, actor, _ = service(tmp_path)
    WorkflowStore(tmp_path).save_workflow(flow())
    calls = []
    finished = threading.Event()
    engine = WorkflowEngine(
        tmp_path, analysis_handler=lambda spec, *args: calls.append(spec["step_id"]) or {"value": 1}
    )
    params = {
        "action": action,
        "workflow_ref": "shared",
        "idempotency_key": "requested",
        "concurrency_policy": strategy,
    }
    if action == "workflow_runs.resume":
        token = CancellationToken()
        token.cancel("preflight fixture")
        with pytest.raises(ToolCancelled) as caught, bind_cancellation_token(token):
            engine.run_workflow("shared", owner_id="alice")
        source = caught.value.record
        assert source.effect_outcome == "not_started" and not calls
        params.pop("workflow_ref")
        params.update(workflow_run_id=source.workflow_run_id, expected_revision=source.revision)
    original_summary = engine._record_summary

    def summarize(record):
        original_summary(record)
        finished.set()

    engine._record_summary = summarize
    tool = WorkflowsTool(
        tmp_path, engine=engine, business_service=api, business_actor_provider=lambda: actor
    )
    entered, release = threading.Event(), threading.Event()

    def hold():
        entered.set()
        assert release.wait(10)

    with ThreadPoolExecutor(1) as pool:
        holder = pool.submit(
            policy.dispatch, tmp_path, "workflow", "shared", "holder", "queue", hold
        )
        try:
            assert entered.wait(3)
            outcome = tool.execute(params)
            assert outcome.ok, outcome.output
            receipt = json.loads(outcome.output)
            assert receipt["status"] == ("queued" if strategy == "queue" else "skipped")
            assert receipt["state"] == ("pending" if strategy == "queue" else "completed")
            replay = tool.execute(params)
            assert replay.ok, replay.output
            assert json.loads(replay.output)["state"] == receipt["state"]
            if action == "workflows.run":
                assert json.loads(replay.output) == receipt
            if strategy == "skip_if_running":
                assert receipt["error_code"] == "WORKFLOW_RUN_SKIPPED_ALREADY_RUNNING"
            assert not calls
            # 并发协议目录不能被当作run_id，业务公开列表仍可读取。
            listing = tool.execute({"action": "workflow_runs.list", "workflow_ref": "shared"})
            assert listing.ok, listing.output
            release.set()
            holder.result(5)
            if strategy == "queue":
                assert finished.wait(5)
                assert engine.store.load_run(receipt["workflow_run_id"]).status == "succeeded"
                assert calls == ["a", "b"]
            else:
                assert not calls
        finally:
            release.set()


def test_queued_cancel_keeps_recoverable_ledger_and_never_executes(tmp_path):
    from agent_py_agent.agent.common.cancellation import CancellationToken, bind_cancellation_token

    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    calls = []
    engine = WorkflowEngine(tmp_path, analysis_handler=lambda *args: calls.append("executed"))
    original_summary = engine._record_summary

    def summarize(record):
        original_summary(record)
        finished.set()

    engine._record_summary = summarize

    def hold():
        entered.set()
        assert release.wait(10)

    with ThreadPoolExecutor(1) as pool:
        holder = pool.submit(
            policy.dispatch, tmp_path, "workflow", "shared", "holder", "queue", hold
        )
        try:
            assert entered.wait(3)
            token = CancellationToken()
            with bind_cancellation_token(token):
                receipt = engine.run_workflow(flow(), concurrency_policy="queue")
                token.cancel("cancel while queued")
            assert receipt["status"] == "queued"
            release.set()
            holder.result(5)
            assert finished.wait(5)
            saved = engine.store.load_run(receipt["workflow_run_id"])
            assert saved.status == "paused_for_resume" and saved.effect_outcome == "not_started"
            assert saved.breakpoint["can_resume"] and not calls
        finally:
            release.set()


def test_summary_pause_resume_final_status_and_repeated_fold(tmp_path):
    store = WorkflowStore(tmp_path)
    store.save_workflow(flow())
    state = {"fail": True}

    def analyze(*args):
        if state["fail"]:
            raise ValueError("first attempt")
        return {"value": 1}

    engine = WorkflowEngine(tmp_path, analysis_handler=analyze)
    failed = engine.run_workflow("shared")
    assert failed.status == "failed"
    assert store.fold_run_summaries("shared").descriptor.runtime_summary.last_status == "failed"
    state["fail"] = False
    resumed = engine.resume_workflow_run(failed.workflow_run_id)
    assert resumed.status == "succeeded"
    summary = store.fold_run_summaries("shared").descriptor.runtime_summary
    assert (
        summary.run_count == 2
        and summary.last_status == "succeeded"
        and summary.consecutive_failures == 0
    )
    engine._record_summary(resumed)
    assert store.fold_run_summaries("shared").descriptor.runtime_summary == summary


def test_admission_write_failure_preserves_queued_execution_unknown(tmp_path, monkeypatch):
    import agent_py_agent.agent.workflows.engine as engine_module

    WorkflowStore(tmp_path).save_workflow(flow())
    engine = WorkflowEngine(tmp_path)
    monkeypatch.setattr(
        engine_module,
        "dispatch",
        lambda *args, **kwargs: {"status": "queued", "queue_position": 1, "run_id": args[3]},
    )

    def fail(*args):
        raise OSError("admission ACK failed")

    monkeypatch.setattr(engine.store, "save_admission_receipt", fail)
    outcome = WorkflowsTool(tmp_path, engine=engine).execute(
        {"action": "run", "workflow_id": "shared", "concurrency_policy": "queue"}
    )
    assert not outcome.ok and outcome.effect_outcome == "unknown"
    assert json.loads(outcome.output)["status"] == "queued"


def test_queue_full_is_not_a_successful_skip(tmp_path, monkeypatch):
    import agent_py_agent.agent.workflows.engine as engine_module

    WorkflowStore(tmp_path).save_workflow(flow())
    monkeypatch.setattr(
        engine_module,
        "dispatch",
        lambda *args, **kwargs: {"status": "skipped", "error_code": "WORKFLOW_RUN_QUEUE_FULL"},
    )
    outcome = WorkflowsTool(tmp_path).execute(
        {"action": "run", "workflow_id": "shared", "concurrency_policy": "queue"}
    )
    assert not outcome.ok and json.loads(outcome.output)["error_code"] == "WORKFLOW_RUN_QUEUE_FULL"


# === Source: agent_py_agent/tests/test_v13_w4_tui_activity_timer.py ===

def _history():
    return [{"run_id": f"old-{index}", "status": "DONE", "ended_at": 100.0}
            for index in range(64)]


def test_idle_history_does_not_hold_timer_origin(monkeypatch):
    import time

    now = [1000.0]
    monkeypatch.setattr(time, "time", lambda: now[0])
    runtime = TuiRuntime("history-timer")
    runtime.update_background_activity(0, {"subagents": _history()})
    state = runtime._background_activity._state
    assert state.started_at == 0.0
    assert len(state.subagents) == 64
    runtime.update_background_activity(1, {"subagents": _history()})
    assert runtime._background_activity._state.started_at == 1000.0
    now[0] = 1020.0
    runtime.update_background_activity(0, {"subagents": _history()})
    assert runtime._background_activity._state.started_at == 0.0
    now[0] = 1100.0
    runtime.update_background_activity(1, {"subagents": _history()})
    assert runtime._background_activity._state.started_at == 1100.0


def test_timer_uses_current_live_main_start(monkeypatch):
    import time

    monkeypatch.setattr(time, "time", lambda: 2000.0)
    runtime = TuiRuntime("main-timer")
    runtime.update_background_activity(1, {"main_activity": {
        "task_id": "reused", "phase": "running", "started_at": 1990.0}})
    assert runtime._background_activity._state.started_at == 1990.0
    runtime.update_background_activity(1, {"main_activity": {
        "task_id": "reused", "phase": "running", "started_at": 1995.0}})
    assert runtime._background_activity._state.started_at == 1995.0
    runtime.update_background_activity(0, {"main_activity": {
        "task_id": "reused", "phase": "completed", "ended_at": 2000.0}})
    assert runtime._background_activity._state.started_at == 0.0


def test_idle_goal_keeps_panel_without_timer(monkeypatch):
    import time

    monkeypatch.setattr(time, "time", lambda: 2000.0)
    runtime = TuiRuntime("goal-timer")
    runtime.update_background_activity(0, {"goals": [{"goal_id": "paused", "status": "paused"}]})
    assert runtime._background_activity._state.started_at == 0.0
    assert any(block.role == "background" for block in runtime.store.snapshot().active_blocks)


def test_main_activity_uses_turn_start_and_activation_fallback(monkeypatch):
    from types import SimpleNamespace

    from agent_py_agent.agent.conversation import agent_activity

    link = SimpleNamespace(task_id="reused", created_at=100.0,
                           expires_at=2100.0, duration_seconds=120)
    row = {"task_id": "reused", "phase": "running", "started_at": 1990.0}
    monkeypatch.setattr(agent_activity, "background_main_activity", lambda *_: dict(row))
    kwargs = {"workspace_task_id": "reused"}
    assert agent_activity._conversation_main_activity(None, "thread", [link], **kwargs)["started_at"] == 1990.0
    row.clear()
    assert agent_activity._conversation_main_activity(None, "thread", [link], **kwargs)["started_at"] == 1980.0
    link.expires_at = 0.0
    assert agent_activity._conversation_main_activity(None, "thread", [link], **kwargs)["started_at"] == 100.0


def test_gateway_failure_duration_overrides_missing_clock():
    import inspect

    from agent_py_agent.cli.turn_completion import TurnCompletionLedger, render_completion

    assert "gateway_timing" in inspect.signature(TurnCompletionLedger.finish).parameters
    ledger = TurnCompletionLedger()
    completed, first = ledger.finish("early-failure", "failed", gateway_timing={"duration_seconds": 0.584})
    assert first
    assert completed.elapsed_seconds == 0.584
    assert "未知" not in render_completion(completed)
    assert ledger.finish("early-failure", "completed", gateway_timing={"duration_seconds": 99.0}) == (completed, False)


def test_gateway_failure_duration_fallbacks(monkeypatch):
    import inspect

    from agent_py_agent.cli.turn_completion import TurnCompletionLedger

    assert "gateway_timing" in inspect.signature(TurnCompletionLedger.finish).parameters
    monkeypatch.setattr("time.monotonic", lambda: 20.0)
    ledger = TurnCompletionLedger()
    ledger.accept("local")
    monkeypatch.setattr("time.monotonic", lambda: 23.0)
    completed, _ = ledger.finish("local", "failed", gateway_timing={"duration_seconds": float("nan")})
    assert completed.elapsed_seconds == 3.0
    completed, _ = ledger.finish("range", "failed", gateway_timing={"duration_seconds": -1.0, "started_at": 10.0, "ended_at": 10.584})
    assert abs(completed.elapsed_seconds - 0.584) < 1e-9
    completed, _ = ledger.finish("unknown", "failed", gateway_timing={"duration_seconds": float("inf"), "started_at": 20.0, "ended_at": 10.0})
    assert completed.elapsed_seconds is None


def test_failed_tui_summary_uses_gateway_duration():
    from dataclasses import fields

    from agent_py_agent.cli.chat_parts.tui_runtime import TuiTurnSummary

    assert "gateway_timing" in {field.name for field in fields(TuiTurnSummary)}
    runtime = TuiRuntime("early-failure-runtime")
    runtime.begin_turn("failure")
    runtime.complete_turn("failure", TuiTurnSummary(ok=False, error="failed", gateway_timing={"duration_seconds": 0.584}))
    completion = runtime.completions.clocks["failure"].completion
    assert completion.elapsed_seconds == 0.584
    blocks = runtime.store.snapshot().stable_blocks
    assert any(block.metadata.get("source_kind") == "turn_timing" and "未知" not in block.text for block in blocks)


# === Source: agent_py_agent/tests/test_v13_w4_tui_queued_stream.py ===

def _params(runtime, jobs):
    return SimpleNamespace(tui_runtime=runtime, jobs=jobs,
                           state_lock=threading.RLock(), pending_jobs_ref_for_enqueue=[0])


def _attach(params):
    _tui_attach_gateway_job(params, "observed", display_text="observed",
                            gateway_request_id="remote", client_message_id="input",
                            execution_options=GatewayAskExecutionOptions())


def _background(runtime, request_id):
    identity = f"bg-main:{request_id}"
    runtime.publish_background_transcript_events([{
        "schema": "background_transcript_event.v1", "seq": 1,
        "gateway_request_id": request_id, "request_id": identity,
        "block_id": f"{identity}:assistant", "kind": "assistant_completed",
        "phase": "completed", "payload": {"text": "后台继续输出"},
    }])
    return any(block.text == "后台继续输出" for block in runtime.store.snapshot().stable_blocks)


def test_restore_skips_observed_gateway_job():
    runtime, jobs = TuiRuntime("observed"), queue.Queue()
    params = _params(runtime, jobs)
    _attach(params)
    runtime.enqueue_prompt("draft", "draft", queued=True)
    jobs.put(ChatJob(user="draft", show_prompt=False, inject=[], prompt_files=[], request_id="draft"))
    params.pending_jobs_ref_for_enqueue[0] += 1
    assert not runtime.has_queued_prompt("remote")
    restored = restore_queued_prompts(jobs=jobs, runtime=runtime, state_lock=params.state_lock,
                                      pending_jobs_ref=params.pending_jobs_ref_for_enqueue,
                                      current_text="editing", current_cursor=3)
    assert restored.request_ids == ("draft",)
    assert restored.text == "draft\nediting"
    assert jobs.qsize() == 1 and jobs.queue[0].gateway_request_id == "remote"
    assert params.pending_jobs_ref_for_enqueue == [1]
    assert len(runtime.store.snapshot().queued_inputs) == 1
    runtime.restore_prompts(("remote",))
    assert len(runtime.store.snapshot().queued_inputs) == 1


def test_attach_failure_releases_observer_ownership():
    runtime = TuiRuntime("attach-failed")

    def reject(_job):
        raise RuntimeError("queue closed")

    params = _params(runtime, SimpleNamespace(put=reject))
    with pytest.raises(RuntimeError, match="queue closed"):
        _attach(params)
    assert params.pending_jobs_ref_for_enqueue == [0]
    assert "remote" not in runtime._owned_gateway_requests
    assert not runtime.store.snapshot().queued_inputs
    assert _background(runtime, "remote")


@pytest.mark.parametrize("summary", [TuiTurnSummary(), TuiTurnSummary(ok=False),
                                    TuiTurnSummary(ok=False, interrupted=True),
                                    TuiTurnSummary(ok=False, outcome_unknown=True)])
def test_released_request_renders_background_events(summary):
    runtime = TuiRuntime("release")
    runtime.register_gateway_request("remote")
    runtime.enqueue_prompt("remote", "observed", queued=True, observed=True)
    runtime.begin_turn("remote")
    runtime.complete_turn("remote", summary)
    assert "remote" not in runtime._owned_gateway_requests
    assert _background(runtime, "remote")


def test_restore_local_draft_releases_foreground_identity():
    runtime = TuiRuntime("restore-release")
    runtime.enqueue_prompt("draft", "draft", queued=True)
    runtime.register_gateway_request("draft")
    runtime.restore_prompts(("draft",))
    assert "draft" not in runtime._owned_gateway_requests
    assert _background(runtime, "draft")


def test_already_consumed_background_event_replays_when_foreground_fails():
    runtime = TuiRuntime("early-background")
    runtime.register_gateway_request("remote")
    runtime.begin_turn("remote")
    assert not _background(runtime, "remote")
    runtime.complete_turn("remote", TuiTurnSummary(ok=False, outcome_unknown=True))
    assert runtime.publish_background_transcript_events([]) == 0
    blocks = runtime.store.snapshot().stable_blocks
    assert sum(block.text == "后台继续输出" for block in blocks) == 1
    runtime.release_gateway_request("remote")
    assert sum(block.text == "后台继续输出" for block in runtime.store.snapshot().stable_blocks) == 1


def test_successful_foreground_does_not_replay_its_buffered_copy():
    runtime = TuiRuntime("success-background")
    runtime.register_gateway_request("remote")
    runtime.begin_turn("remote")
    assert not _background(runtime, "remote")
    runtime.complete_turn("remote", TuiTurnSummary(ok=True, response_text="后台继续输出"))
    assert sum(block.text == "后台继续输出" for block in runtime.store.snapshot().stable_blocks) == 1


# === Source: agent_py_agent/tests/test_v13_w6_feishu_replay_window.py ===

@pytest.fixture
def now():
    return 1_800_000_000.0


def _body(*, token=None):
    header = {"event_id": "SYNTHETIC_ONLY_EVENT_ID"}
    if token is not None:
        header["token"] = token

    return json.dumps(
        {
            "header": header,
            "event": {
                "message": "SYNTHETIC_ONLY_MESSAGE",
                "url": "https://example.invalid/SYNTHETIC_ONLY_EVENT",
            },
        },
        sort_keys=True,
    ).encode("utf-8")


def _request(
    now,
    *,
    timestamp=None,
    nonce="SYNTHETIC_ONLY_NONCE",
    guard=None,
    tolerance=300,
):
    if timestamp is None:
        timestamp = str(int(now))
    if guard is None:
        guard = FeishuReplayGuard()

    raw_body = _body()
    encrypt_key = "SYNTHETIC_ONLY_ENCRYPT_KEY"
    signature = crypto.compute_signature(
        crypto.FeishuSignParts(
            timestamp,
            nonce,
            encrypt_key,
            raw_body,
        )
    )
    return crypto.FeishuWebhookDecodeRequest(
        raw_body=raw_body,
        encrypt_key=encrypt_key,
        timestamp=timestamp,
        nonce=nonce,
        signature=signature,
        timestamp_tolerance_seconds=tolerance,
        now=now,
        replay_guard=guard,
    )


def _codes(caplog):
    return [
        getattr(record, "diagnostic_code", None)
        for record in caplog.records
    ]


def test_request_appends_defaults_without_breaking_old_positionals():
    request = crypto.FeishuWebhookDecodeRequest(
        b"{}",
        "",
        "",
        "",
        "",
        "",
    )

    assert request.timestamp_tolerance_seconds == 300
    assert request.now is None
    assert request.replay_guard is None


def test_verify_signature_still_accepts_nonnumeric_ts():
    # "ts" 是需求指定的非数字时间戳回归样例；
    # 纯签名函数不承担 webhook 时间窗校验。
    parts = crypto.FeishuSignParts(
        "ts",
        "SYNTHETIC_ONLY_NONCE",
        "SYNTHETIC_ONLY_ENCRYPT_KEY",
        _body(),
    )
    signature = crypto.compute_signature(parts)

    assert crypto.verify_signature(parts, signature)
    assert not crypto.verify_signature(
        parts,
        "SYNTHETIC_ONLY_INVALID_SIGNATURE",
    )


@pytest.mark.parametrize("offset", [-300, -299, 0, 300])
def test_signed_timestamp_closed_window_accepts(now, offset):
    request = _request(
        now,
        timestamp=str(int(now) + offset),
    )

    assert crypto.verify_and_decode_webhook(request) == json.loads(
        request.raw_body
    )


@pytest.mark.parametrize("offset", [-301, 301])
def test_signed_timestamp_outside_window_rejects(
    now,
    offset,
    caplog,
):
    guard = FeishuReplayGuard()
    request = _request(
        now,
        timestamp=str(int(now) + offset),
        guard=guard,
    )

    assert crypto.verify_and_decode_webhook(request) is None
    assert "FEISHU_TIMESTAMP_OUT_OF_WINDOW" in _codes(caplog)
    assert len(guard) == 0


def test_expired_1609459200_rejects(now, caplog):
    guard = FeishuReplayGuard()
    request = _request(
        now,
        timestamp="1609459200",
        guard=guard,
    )

    assert crypto.verify_and_decode_webhook(request) is None
    assert "FEISHU_TIMESTAMP_OUT_OF_WINDOW" in _codes(caplog)
    assert len(guard) == 0


def test_abc_timestamp_rejects_with_structured_diagnostic(
    now,
    caplog,
):
    guard = FeishuReplayGuard()
    request = _request(
        now,
        timestamp="abc",
        guard=guard,
    )

    # 签名本身正确；拒绝原因必须来自时间戳解析。
    parts = crypto.FeishuSignParts(
        request.timestamp,
        request.nonce,
        request.encrypt_key,
        request.raw_body,
    )
    assert crypto.verify_signature(parts, request.signature)

    assert crypto.verify_and_decode_webhook(request) is None
    assert "FEISHU_TIMESTAMP_INVALID" in _codes(caplog)
    assert len(guard) == 0


@pytest.mark.parametrize("invalid_now", [float("nan"), float("inf")])
def test_nonfinite_now_fails_closed(now, invalid_now, caplog):
    guard = FeishuReplayGuard()
    request = replace(
        _request(now, guard=guard),
        now=invalid_now,
    )

    assert crypto.verify_and_decode_webhook(request) is None
    assert "FEISHU_TIMESTAMP_INVALID" in _codes(caplog)
    assert len(guard) == 0


def test_same_request_second_time_rejects_without_secret_logs(
    now,
    caplog,
):
    guard = FeishuReplayGuard()
    request = _request(now, guard=guard)

    assert crypto.verify_and_decode_webhook(request) is not None
    assert crypto.verify_and_decode_webhook(request) is None

    assert "FEISHU_REPLAY_DETECTED" in _codes(caplog)
    assert len(guard) == 1

    for forbidden in (
        request.signature,
        request.encrypt_key,
        request.nonce,
        request.raw_body.decode("utf-8"),
    ):
        assert forbidden not in caplog.text


def test_different_nonce_is_not_same_replay_key(now):
    guard = FeishuReplayGuard()
    first = _request(
        now,
        nonce="SYNTHETIC_ONLY_NONCE_A",
        guard=guard,
    )
    second = _request(
        now,
        nonce="SYNTHETIC_ONLY_NONCE_B",
        guard=guard,
    )

    assert crypto.verify_and_decode_webhook(first) is not None
    assert crypto.verify_and_decode_webhook(second) is not None
    assert len(guard) == 2


def test_invalid_signature_does_not_consume_slot(now):
    guard = FeishuReplayGuard(max_entries=1)
    valid = _request(now, guard=guard)
    invalid = replace(
        valid,
        signature="SYNTHETIC_ONLY_INVALID_SIGNATURE",
    )

    assert crypto.verify_and_decode_webhook(invalid) is None
    assert len(guard) == 0

    assert crypto.verify_and_decode_webhook(valid) is not None
    assert len(guard) == 1


def test_none_guard_preserves_no_deduplication(now):
    request = replace(_request(now), replay_guard=None)
    assert crypto.verify_and_decode_webhook(request) is not None
    assert crypto.verify_and_decode_webhook(request) is not None


def test_future_timestamp_does_not_expire_at_first_seen_ttl(
    now,
    caplog,
):
    guard = FeishuReplayGuard(window_seconds=300)
    request = _request(
        now,
        timestamp=str(int(now) + 300),
        guard=guard,
    )

    assert crypto.verify_and_decode_webhook(request) is not None

    # 已超过首次接收时间 + 300，但 timestamp 仍在容差内。
    assert crypto.verify_and_decode_webhook(
        replace(request, now=now + 301)
    ) is None
    assert "FEISHU_REPLAY_DETECTED" in _codes(caplog)

    caplog.clear()

    # timestamp + tolerance 边界仍必须拒绝重放。
    assert crypto.verify_and_decode_webhook(
        replace(request, now=now + 600)
    ) is None
    assert "FEISHU_REPLAY_DETECTED" in _codes(caplog)

    caplog.clear()

    # 再往后由时间窗直接拒绝，而非重新准入。
    assert crypto.verify_and_decode_webhook(
        replace(request, now=now + 601)
    ) is None
    assert "FEISHU_TIMESTAMP_OUT_OF_WINDOW" in _codes(caplog)


def test_guard_expiry_is_inclusive_and_duplicate_does_not_extend_it():
    guard = FeishuReplayGuard(window_seconds=2)

    def claim(observed_now):
        return guard.check_and_record(
            "100",
            "SYNTHETIC_ONLY_NONCE",
            "SYNTHETIC_ONLY_SIGNATURE",
            now=observed_now,
            valid_until=observed_now,
        )

    assert claim(100.0)
    assert not claim(102.0)
    assert claim(102.001)
    assert len(guard) == 1


def test_capacity_is_bounded_and_eviction_is_lru(now):
    guard = FeishuReplayGuard(max_entries=2)

    first = _request(
        now,
        nonce="SYNTHETIC_ONLY_NONCE_A",
        guard=guard,
    )
    second = _request(
        now,
        nonce="SYNTHETIC_ONLY_NONCE_B",
        guard=guard,
    )
    third = _request(
        now,
        nonce="SYNTHETIC_ONLY_NONCE_C",
        guard=guard,
    )

    assert crypto.verify_and_decode_webhook(first) is not None
    assert crypto.verify_and_decode_webhook(second) is not None

    # 重复命中刷新 A 的 LRU 顺序，但仍拒绝该次调用。
    assert crypto.verify_and_decode_webhook(first) is None
    assert crypto.verify_and_decode_webhook(third) is not None
    assert len(guard) == 2

    # 此时 A 尚在，B 已被容量淘汰。
    assert crypto.verify_and_decode_webhook(first) is None
    assert crypto.verify_and_decode_webhook(second) is not None
    assert len(guard) == 2


def test_concurrent_same_request_has_exactly_one_success(now):
    guard = FeishuReplayGuard()
    request = _request(now, guard=guard)
    barrier = threading.Barrier(16)

    def invoke(_):
        barrier.wait(timeout=10)
        return crypto.verify_and_decode_webhook(request)

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(invoke, range(16)))

    assert sum(result is not None for result in results) == 1
    assert sum(result is None for result in results) == 15
    assert len(guard) == 1


def test_clock_rollback_keeps_live_replay_record(now, caplog):
    guard = FeishuReplayGuard()
    original = _request(
        now,
        nonce="SYNTHETIC_ONLY_NONCE_A",
        guard=guard,
    )

    assert crypto.verify_and_decode_webhook(original) is not None

    # 现存记录不因回拨被清理。
    assert crypto.verify_and_decode_webhook(
        replace(original, now=now - 1)
    ) is None
    assert "FEISHU_REPLAY_DETECTED" in _codes(caplog)

    # 尚未遗忘到期记录时，不把小幅回拨等同于所有请求重放。
    distinct = _request(
        now - 1,
        timestamp=original.timestamp,
        nonce="SYNTHETIC_ONLY_NONCE_B",
        guard=guard,
    )
    assert crypto.verify_and_decode_webhook(distinct) is not None


def test_clock_rollback_cannot_resurrect_expired_record(
    now,
    caplog,
):
    guard = FeishuReplayGuard()
    original = _request(
        now,
        nonce="SYNTHETIC_ONLY_NONCE_A",
        guard=guard,
    )
    assert crypto.verify_and_decode_webhook(original) is not None

    # 推进时间并触发原记录的到期清理。
    advanced = _request(
        now + 301,
        nonce="SYNTHETIC_ONLY_NONCE_B",
        guard=guard,
    )
    assert crypto.verify_and_decode_webhook(advanced) is not None

    caplog.clear()

    # 原记录已被清理，但回拨使其重新落入时间容差。
    # 必须 fail closed，不能因缓存中找不到它就重新接受。
    assert crypto.verify_and_decode_webhook(
        replace(original, now=now + 300)
    ) is None
    assert "FEISHU_CLOCK_ROLLBACK" in _codes(caplog)

    # 超过已清理截止线后，可继续接受新的有效请求。
    recovered = _request(
        now + 302,
        nonce="SYNTHETIC_ONLY_NONCE_C",
        guard=guard,
    )
    assert crypto.verify_and_decode_webhook(recovered) is not None


def test_token_only_does_not_use_signature_window_or_guard():
    token = "SYNTHETIC_ONLY_VERIFICATION_TOKEN"
    raw_body = _body(token=token)
    request = crypto.FeishuWebhookDecodeRequest(
        raw_body=raw_body,
        verification_token=token,
        timestamp="abc",
        timestamp_tolerance_seconds=-1,
        now=float("nan"),
        replay_guard=object(),
    )
    expected = json.loads(raw_body)

    # 此层不新增 token-only 去重，保留上层已有 event_id 准入。
    assert crypto.verify_and_decode_webhook(request) == expected
    assert crypto.verify_and_decode_webhook(request) == expected

    assert crypto.verify_and_decode_webhook(
        replace(
            request,
            verification_token="SYNTHETIC_ONLY_WRONG_TOKEN",
        )
    ) is None


def test_unsigned_request_is_still_rejected(now):
    request = replace(
        _request(now),
        signature="",
    )

    assert crypto.verify_and_decode_webhook(request) is None


# === Source: agent_py_agent/tests/test_v13_w6_log_redaction_empty_user.py ===

@pytest.mark.parametrize("scheme", ["redis", "http"])
@pytest.mark.parametrize("user", ["", "user"])
@pytest.mark.parametrize("code_file", [False, True])
def test_url_literal_is_redacted(scheme, user, code_file):
    url = f"{scheme}://{user}:SYNTHETIC_ONLY_56@example.invalid/0"
    assert redact_sensitive_text(url, code_file=code_file) == (
        f"{scheme}://{user}:<redacted>@example.invalid/0"
    )


@pytest.mark.parametrize("user", ["", "user"])
def test_source_interpolation_keeps_shell_but_masks_literal(user):
    value = f"redis://{user}:${{encodeURIComponent('SYNTHETIC_ONLY_56')}}@example.invalid/0"
    assert redact_sensitive_text(value, code_file=True) == (
        f"redis://{user}:${{encodeURIComponent('<redacted>')}}@example.invalid/0"
    )


@pytest.mark.parametrize("user", ["", "user"])
def test_source_variable_reference_unchanged(user):
    value = f"redis://{user}:${{PASSWORD}}@example.invalid/0"
    assert redact_sensitive_text(value, code_file=True) == value


@pytest.mark.parametrize("user", ["", "user"])
@pytest.mark.parametrize("traceback", [False, True])
def test_log_factory_and_formatter_cover_arguments_and_traceback(user, traceback):
    original = logging.getLogRecordFactory()
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    handler.setFormatter(RedactingFormatter("%(message)s"))
    logger = logging.Logger("SYNTHETIC_ONLY_56", level=logging.INFO)
    logger.addHandler(handler)
    url = f"redis://{user}:SYNTHETIC_ONLY_56@example.invalid/0"
    try:
        install_log_redaction()
        if traceback:
            try:
                raise ValueError(url)
            except ValueError:
                logger.exception("connection failed")
        else:
            logger.info("connection %s", url)
        text = output.getvalue()
        assert "SYNTHETIC_ONLY_56" not in text
        assert f"redis://{user}:<redacted>@example.invalid/0" in text
        if traceback:
            assert "Traceback" in text and "ValueError" in text
    finally:
        logging.setLogRecordFactory(original)
        logger.removeHandler(handler)
        handler.close()


@pytest.mark.parametrize("code_file", [False, True])
@pytest.mark.parametrize("url", ["http://example.invalid/path", "redis://:@example.invalid/0"])
def test_absent_or_empty_password_preserves_url(url, code_file):
    assert redact_sensitive_text(url, code_file=code_file) == url


@pytest.mark.parametrize("code_file", [False, True])
def test_percent_encoded_password_is_hidden(code_file):
    value = "redis://:SYNTHETIC_ONLY_56%40%3A@example.invalid:6379/path"
    assert redact_sensitive_text(value, code_file=code_file) == (
        "redis://:<redacted>@example.invalid:6379/path"
    )


@pytest.mark.parametrize("code_file", [False, True])
def test_long_unmatched_text_is_bounded(code_file):
    value = "redis://:" + "x" * 100_000
    start = time.perf_counter()
    actual = redact_sensitive_text(value, code_file=code_file)
    elapsed = time.perf_counter() - start
    assert actual == value
    assert elapsed < 0.25


# === Source: agent_py_agent/tests/test_v13_w6_sqlite_index_contention.py ===

def _busy(code=sqlite3.SQLITE_BUSY):
    error = sqlite3.OperationalError("SYNTHETIC_ONLY_ERROR")
    error.sqlite_errorcode = code
    error.sqlite_errorname = "SQLITE_BUSY" if code == sqlite3.SQLITE_BUSY else "SQLITE_ERROR"
    return error


def test_task_projection_waits_for_100ms_writer(tmp_path):
    with indexes.task_ref_projection(tmp_path) as (scope, _):
        path = scope.projection
    locked, release = threading.Event(), threading.Event()
    failures = []

    def holder():
        try:
            with sqlite3.connect(path) as conn:
                conn.execute("BEGIN IMMEDIATE")
                locked.set()
                assert release.wait(5)
        except BaseException as exc:
            failures.append(exc)

    thread = threading.Thread(target=holder)
    thread.start()
    assert locked.wait(5)
    timer = threading.Timer(0.1, release.set)
    timer.start()
    try:
        with indexes.task_ref_projection(tmp_path) as (_, conn):
            assert conn.execute("SELECT 1").fetchone()[0] == 1
    finally:
        release.set()
        thread.join(5)
        timer.join(5)
    assert not thread.is_alive() and not failures


@pytest.mark.parametrize("kind", ["task", "session"])
def test_wal_and_normal_allow_reader_during_write(tmp_path, kind):
    if kind == "task":
        context = indexes.task_ref_projection(tmp_path)
    else:
        root = tmp_path / "threads"
        root.mkdir()
        index = SessionListIndex(root, kind="thread", reader=lambda path: ({}, None))
        context = index._connection()
    with context as item:
        writer = item[1] if kind == "task" else item
        path = item[0].projection if kind == "task" else index.path
        assert writer.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert writer.execute("PRAGMA synchronous").fetchone()[0] == 1
    writer = sqlite3.connect(path)
    reader = sqlite3.connect(path, timeout=0.05)
    try:
        writer.execute("BEGIN EXCLUSIVE")
        table = "locator_meta" if kind == "task" else "meta"
        writer.execute(f"INSERT OR REPLACE INTO {table} VALUES ('SYNTHETIC_ONLY_KEY','value')")
        assert reader.execute(f"SELECT value FROM {table} WHERE key='SYNTHETIC_ONLY_KEY'").fetchone() is None
    finally:
        writer.rollback()
        writer.close()
        reader.close()


@pytest.mark.parametrize("code,expected", [(sqlite3.SQLITE_BUSY, 3), (sqlite3.SQLITE_LOCKED, 3), (sqlite3.SQLITE_ERROR, 1)])
def test_lookup_retries_only_busy_codes(tmp_path, monkeypatch, code, expected):
    original = indexes.task_ref_projection
    calls, waits = [], []
    error = _busy(code)

    @contextmanager
    def flaky(*args, **kwargs):
        calls.append(1)
        if len(calls) < 3:
            raise error
        with original(*args, **kwargs) as value:
            yield value

    monkeypatch.setattr(indexes, "task_ref_projection", flaky)
    monkeypatch.setattr(time, "sleep", waits.append)
    if code == sqlite3.SQLITE_ERROR:
        with pytest.raises(sqlite3.OperationalError) as caught:
            indexes.lookup_task_ref(tmp_path, "SYNTHETIC_ONLY_TASK")
        assert caught.value is error and not waits
    else:
        result = indexes.lookup_task_ref(tmp_path, "SYNTHETIC_ONLY_TASK")
        assert result["status"] == "missing"
        assert waits == [0.05, 0.1]
    assert len(calls) == expected


def test_locator_reports_busy_not_missing(tmp_path, monkeypatch):
    error = _busy()
    def fail(*args, **kwargs):
        raise error
    monkeypatch.setattr(task_locator, "lookup_task_ref", fail)
    result = task_locator.resolve_task_location(tmp_path, "SYNTHETIC_ONLY_TASK")
    assert result["status"] == "partial" and result["reason"] == "sqlite_busy"


def test_exhausted_lookup_reraises_original_and_name_fallback(tmp_path, monkeypatch):
    error = sqlite3.OperationalError("SYNTHETIC_ONLY_ERROR")
    error.sqlite_errorname = "SQLITE_LOCKED"
    calls = []
    @contextmanager
    def fail(*args, **kwargs):
        calls.append(1)
        raise error
        yield
    monkeypatch.setattr(indexes, "task_ref_projection", fail)
    with pytest.raises(sqlite3.OperationalError) as caught:
        indexes.lookup_task_ref(tmp_path, "SYNTHETIC_ONLY_TASK", busy_retries=2, backoff_seconds=0)
    assert caught.value is error and len(calls) == 3


def test_busy_projection_must_not_be_truncated(tmp_path, monkeypatch):
    root = tmp_path / "threads"
    root.mkdir()
    index = SessionListIndex(root, kind="thread", reader=lambda path: ({}, None))
    with index._connection():
        pass
    original = index.path.read_bytes()
    error = _busy()
    def fail():
        raise error
    monkeypatch.setattr(index, "_open_projection", fail)
    with pytest.raises(sqlite3.OperationalError) as caught:
        with index._connection():
            pass
    assert caught.value is error
    assert index.path.read_bytes() == original
