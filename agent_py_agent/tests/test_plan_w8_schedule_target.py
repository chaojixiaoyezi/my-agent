"""W8：公开 schedule -> 原后台执行 -> 原资产工具 -> 历史收口。"""
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent_py_agent.agent.assets.descriptor import AssetDescriptor
from agent_py_agent.agent.assets.script_store import ScriptStore
from agent_py_agent.agent.assets.workflow_store import (
    WorkflowDefinition,
    WorkflowStepSpec,
    WorkflowStore,
)
from agent_py_agent.agent.conversation import (
    BackgroundMainAgentRuntime,
    BackgroundMainAgentScheduler,
)
from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
from agent_py_agent.agent.conversation.host_actor import HostActorError
from agent_py_agent.agent.gateway_parts.control_service import GatewayControlScope
from agent_py_agent.agent.gateway_parts.schedule_control import execute_schedule_control
from agent_py_agent.agent.scheduler.control import authorize_job
from agent_py_agent.agent.scheduler.creator_migration import migrate_monitor_creators
from agent_py_agent.agent.scheduler.repository import SchedulerJobCreateRequest
from agent_py_agent.agent.scheduler.service import SchedulerService
from agent_py_agent.agent.scheduler.tool import ScheduleTool, build_schedule_tool_model_spec
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
from agent_py_agent.tests.test_schedule_control import _agent
from agent_py_agent.tests.test_scheduler_runtime import _Backend, _RetryDelivery


def asset_case(tmp_path, kind="script", *, analysis=False):
    agent = _agent(tmp_path)
    # 旧用例观察未错峰的精确周期身份；抖动单独由v15的真实派发测试验证。
    agent.scheduler_service.monitor_jitter_window_seconds = 0
    agent.backend = _Backend()
    home = agent.home_paths.owner_home_dir
    scripts = ScriptStore(home, workspace_root=tmp_path)
    scripts.save_script(AssetDescriptor(id="scheduled_script", kind="script", name="合成脚本"),
        'import json,sys\nx=json.load(sys.stdin)\nprint(json.dumps({"ok":True,"value":x.get("value")}))')
    workflows = WorkflowStore(home, workspace_root=tmp_path)
    workflows.save_workflow(WorkflowDefinition(AssetDescriptor(id="scheduled_workflow", kind="workflow", name="合成工作流"),
        steps=(WorkflowStepSpec(step_id="one", name="分析" if analysis else "执行", mode="llm_skill_analysis" if analysis else "deterministic_script",
            script_id="" if analysis else "scheduled_script"),)))
    thread = agent.conversation_store.threads.get_or_create({"channel": "local", "channel_user_id": "synthetic",
        "canonical_user_id": "synthetic", "owner_id": agent.home_paths.owner_id, "owner_home": str(home), "cwd": str(tmp_path), "now": 900})
    agent._current_run_params = SimpleNamespace(request_id="schedule-target", task_id="", task_attributes={"conversation_thread_id": thread.thread_id})
    tool = ScheduleTool(agent)
    with patch('agent_py_agent.agent.scheduler.tool.time.time', return_value=900):
        result = tool.execute({"action": "create", "name": "资产定时", "schedule_kind": "every", "every_seconds": 600,
            "anchor_at": 1000, "timezone": "UTC", "execution_target": {"kind": kind, "asset_id": "scheduled_" + kind,
            "input_ref": {"inputs": {"value": 7}}}, "delivery": {"mode": "none"}})
    assert result.ok, result.output
    job = agent.scheduler_repository.list_jobs()[0][0]
    runtime = BackgroundMainAgentRuntime(agent=agent, store=agent.conversation_store, channels=_RetryDelivery())
    scheduler = BackgroundMainAgentScheduler({"runtime": runtime, "store": agent.conversation_store, "scheduler_service": agent.scheduler_service})
    return SimpleNamespace(agent=agent, job=job, tool=tool, scheduler=scheduler, scripts=scripts, workflows=workflows)


def test_public_schedule_declares_structured_target():
    properties = build_schedule_tool_model_spec().input_schema["properties"]
    assert "execution_target" in properties
    assert {"kind", "asset_id", "execution_definition_ref", "input_ref"} <= properties["execution_target"]["properties"].keys()


@pytest.mark.parametrize("kind", ["script", "workflow"])
def test_pinned_target_runs_once_with_no_outer_model_and_ignores_summary_revision(tmp_path, kind):
    case = asset_case(tmp_path, kind)
    repo = case.agent.scheduler_repository
    target = case.job["execution_target"]
    assert target["execution_definition_ref"]
    # 资产运行摘要的 CAS 版本变化不是执行定义变化。
    store = case.scripts if kind == "script" else case.workflows
    store.record_run_summary("scheduled_" + kind, "succeeded", 1)
    case.agent.scheduler_service.enqueue_ready_runs(now=1000)
    run = repo.queued_runs(now=1000)[0]
    assert run["execution_target"] == target
    assert run["execution_definition_ref"] == target["execution_definition_ref"]
    assert run["scheduled_for"] == 1000
    SchedulerService(repo, conversation_store=case.agent.conversation_store).enqueue_ready_runs(now=1001)
    assert [row["run_id"] for row in repo.queued_runs(now=1001)] == [run["run_id"]]
    case.scheduler.tick(now=1001)
    case.scheduler.tick(now=1002)
    history, errors = repo.history()
    assert errors == [] and len(history) == 1
    row = history[0]
    assert row["run_id"] == run["run_id"] and row["status"] == "done", row
    assert row["model_call_count"] == 0 and case.agent.backend.prompts == []
    assert row["execution_definition_ref"] == target["execution_definition_ref"]
    value = row["structured_result"]
    output = value["output_data"] if kind == "script" else value["final_output"]
    assert output == {"ok": True, "value": 7}
    assert row["delivery_status"] in {"suppressed", "not_applicable", "local_only", "recorded"}


def test_changed_asset_is_rejected_instead_of_silently_rebinding(tmp_path):
    case = asset_case(tmp_path)
    old = case.scripts.get_script("scheduled_script")
    case.scripts.save_script(AssetDescriptor(id="scheduled_script", kind="script", name="合成脚本"),
        'raise RuntimeError("must not execute replacement")', expected_revision=old["revision"])
    case.agent.scheduler_service.enqueue_ready_runs(now=1000)
    case.scheduler.tick(now=1001)
    row = case.agent.scheduler_repository.history()[0][0]
    assert row["status"] == "failed" and row["error_code"] == "SCHEDULER_EXECUTION_DEFINITION_STALE"
    assert row["model_call_count"] == 0 and not case.agent.backend.prompts


def test_run_freezes_target_and_delivery_when_job_updated(tmp_path):
    case = asset_case(tmp_path)
    repo = case.agent.scheduler_repository
    run = repo.reserve_due_runs(now=1000)[0]
    current = repo.get_job(case.job["job_id"])
    changed = case.tool.execute({"action": "update", "job_id": current["job_id"], "expected_version": current["version"],
        "execution_target": {"kind": "workflow", "asset_id": "scheduled_workflow", "input_ref": {"inputs": {"value": 8}}},
        "delivery": {"mode": "local"}})
    assert changed.ok, changed.output
    frozen = repo.get_active_run(run["run_id"])
    assert frozen["execution_target"] == case.job["execution_target"]
    assert frozen["delivery"]["mode"] == "none"
    assert repo.get_job(current["job_id"])["execution_target"]["kind"] == "workflow"


@pytest.mark.parametrize("operation", ["pause", "cancel", "delete"])
@pytest.mark.parametrize("entry", ["tool", "slash"])
def test_local_legacy_unknown_creator_same_owner_control(tmp_path, operation, entry):
    agent = _agent(tmp_path)
    repo = agent.scheduler_repository
    job = repo.create_job(SchedulerJobCreateRequest(name="任意历史名称", prompt="旧计划", thread_id="legacy",
        schedule={"kind": "every", "every_seconds": 600, "anchor_at": 1000, "timezone": "UTC"}, now=900))[0]
    with repo._mutation_scope() as (store, admission):
        store["jobs"][job["job_id"]].pop("creator", None)
        repo._write_store_unlocked(store, admission)
    assert migrate_monitor_creators(repo, agent.conversation_store, now=901)["migrated"] == 1
    migrated = repo.get_job(job["job_id"])
    assert migrated.get("creator") is None
    assert migrated["creator_migration"]["status"] == "owner_managed"
    assert migrated["creator_migration"]["reason_code"] == "SCHEDULER_LEGACY_LOCAL_OWNER"
    stable = repo.store_path.read_bytes()
    migrate_monitor_creators(repo, agent.conversation_store, now=902)
    assert repo.store_path.read_bytes() == stable
    thread = agent.conversation_store.threads.get_or_create({'channel': 'local', 'channel_user_id': 'synthetic',
        'owner_id': agent.home_paths.owner_id, 'owner_home': str(agent.home_paths.owner_home_dir)})
    agent._current_run_params = SimpleNamespace(request_id='control', task_id='',
        task_attributes={'conversation_thread_id': thread.thread_id})
    if entry == "tool":
        result = ScheduleTool(agent).execute({"action": operation, "job_id": job["job_id"], "expected_version": migrated["version"]})
    else:
        result = execute_schedule_control(agent, parse_conversation_control(f"/schedule {operation} {job['alias']}"),
            GatewayControlScope("local", "local", "", resolved_owner=OwnerIdentity.local_main()))
    assert result.ok, result
    after = repo.get_job(job["job_id"], include_deleted=True)
    assert after["status"] == ("paused" if operation == "pause" else "deleted" if operation == "delete" else "active")


def test_local_legacy_migration_does_not_authorize_foreign_owner_or_group(tmp_path):
    agent = _agent(tmp_path)
    repo = agent.scheduler_repository
    job = repo.create_job(SchedulerJobCreateRequest(name="历史", prompt="旧计划", thread_id="legacy",
        schedule={"kind": "every", "every_seconds": 600, "anchor_at": 1000, "timezone": "UTC"}, now=900))[0]
    foreign = {**job, "owner": {**repo.owner, "id": "someone-else"}}
    with pytest.raises(HostActorError):
        authorize_job(repo, foreign)
    group = _agent(tmp_path / "group", group="oc_group")
    with pytest.raises(HostActorError):
        authorize_job(group.scheduler_repository, {**job, "owner": group.scheduler_repository.owner})


def test_analysis_workflow_records_actual_native_model_calls(tmp_path):
    from agent_py_agent.agent.agent_core.model.call_runtime import model_call_ledger
    from agent_py_agent.agent.backends import ModelResponse

    case = asset_case(tmp_path, "workflow", analysis=True)
    definition = case.workflows.get_workflow("scheduled_workflow")
    case.workflows.save_workflow(replace(definition, steps=definition.steps + (
        WorkflowStepSpec(step_id="two", name="再次分析", mode="llm_skill_analysis"),)))
    current = case.agent.scheduler_repository.get_job(case.job["job_id"])
    rebound = case.tool.execute({"action": "update", "job_id": current["job_id"], "expected_version": current["version"],
        "execution_target": {"kind": "workflow", "asset_id": "scheduled_workflow", "input_ref": {"inputs": {"value": 7}}}})
    assert rebound.ok, rebound.output
    calls = []

    def structured(prompt, *, response_schema, **kwargs):
        calls.append((prompt, response_schema))
        return ModelResponse(text='{"ok":true,"value":7}', backend="synthetic-structured")

    case.agent.backend.generate_structured = structured
    case.agent.scheduler_service.enqueue_ready_runs(now=1000)
    case.scheduler.tick(now=1001)
    row = case.agent.scheduler_repository.history()[0][0]
    assert row["status"] == "done", row
    assert len(calls) == row["model_call_count"] == 2
    assert model_call_ledger(case.agent).cumulative_summary(run_id=row["run_id"])["physical_model_attempt_count"] == 2
    assert case.agent.backend.prompts == []


@pytest.mark.parametrize("kind", ["script", "workflow"])
def test_execution_consumes_the_validated_snapshot_not_a_later_replacement(tmp_path, monkeypatch, kind):
    from agent_py_agent.agent.tooling.registry import ToolRegistry

    case = asset_case(tmp_path, kind)
    original = ToolRegistry.execute_tool
    replaced = []

    def replace_after_snapshot(registry, call, **kwargs):
        if call.tool_name == ("scripts" if kind == "script" else "workflows") and not replaced:
            replaced.append(True)
            if kind == "script":
                case.scripts.save_script(AssetDescriptor(id="scheduled_script", kind="script", name="替代代码"),
                    'raise RuntimeError("replacement must not run")')
            else:
                definition = case.workflows.get_workflow("scheduled_workflow")
                case.workflows.save_workflow(replace(definition, steps=(
                    WorkflowStepSpec(step_id="replacement", name="替代分析", mode="llm_skill_analysis"),)))
        return original(registry, call, **kwargs)

    monkeypatch.setattr(ToolRegistry, "execute_tool", replace_after_snapshot)
    case.agent.scheduler_service.enqueue_ready_runs(now=1000)
    case.scheduler.tick(now=1001)
    row = case.agent.scheduler_repository.history()[0][0]
    assert replaced == [True] and row["status"] == "done", row
    data = row["structured_result"]["output_data" if kind == "script" else "final_output"]
    assert data == {"ok": True, "value": 7}
    assert row["model_call_count"] == 0 and not case.agent.backend.prompts


def test_tool_disabled_after_reservation_uses_target_failure_closeout(tmp_path):
    case = asset_case(tmp_path)
    case.agent.scheduler_service.enqueue_ready_runs(now=1000)
    run = case.agent.scheduler_repository.queued_runs(now=1000)[0]
    case.agent.tools.disabled_tool_names.add("scripts")
    case.scheduler.tick(now=1001)
    row = case.agent.scheduler_repository.history()[0][0]
    assert row["run_id"] == run["run_id"] and row["status"] == "failed"
    assert row["error_code"] == "SCHEDULER_EXECUTION_TARGET_UNAVAILABLE"
    assert row["model_call_count"] == 0 and not case.agent.backend.prompts
    assert case.agent.scheduler_repository.get_active_run(run["run_id"]) is None
    assert case.agent.conversation_store.tasks.load(run["run_id"]).status == "abandoned"
    assert not case.agent.conversation_store.wakes.pending()
