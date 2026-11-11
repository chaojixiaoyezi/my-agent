# LLM: WF-05 (持久化通知与无人值守告警) 及 WF-06 (定义锁细化与脚本缺失恢复) 综合回归测试。
# 模块用途: 验证 notification 节点、无人值守失败告警、动态资源锁细化、并发无阻塞以及 version_hook 缺失脚本依赖的优雅处理。
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent_py_agent.agent.assets.descriptor import AssetDescriptor
from agent_py_agent.agent.assets.revision_history import (
    latest_version,
    pending_publication,
)
from agent_py_agent.agent.assets.workflow_store import (
    WorkflowDefinition,
    WorkflowStepSpec,
    WorkflowStore,
)
from agent_py_agent.agent.common.nofollow_fs import read_text_beneath
from agent_py_agent.agent.tooling.scripts_tool import ScriptsTool
from agent_py_agent.agent.tooling.workflows_tool import WorkflowsTool
from agent_py_agent.agent.workflows.engine import WorkflowEngine
from agent_py_agent.agent.workflows.models import (
    StepExecutionRecord,
    WorkflowRunRecord,
)
from agent_py_agent.agent.workflows.notification_steps import (
    execute_notification_step,
    notify_unattended_run_failure,
)


def _make_run(owner_home: Path, run_id: str = "run-001", inputs: dict | None = None) -> WorkflowRunRecord:
    now = datetime.now(timezone.utc).timestamp()
    run = WorkflowRunRecord(
        workflow_run_id=run_id,
        workflow_id="flow-test",
        owner_id="test_owner",
        trigger_kind="manual",
        status="running",
        started_at=now,
        ended_at=0.0,
        duration_ms=0.0,
        workflow_name="测试工作流",
        inputs=inputs or {},
    )
    run.owner_home = owner_home
    return run


def _make_step(step_id: str = "step-notif", mode: str = "notification") -> StepExecutionRecord:
    now = datetime.now(timezone.utc).timestamp()
    return StepExecutionRecord(
        step_id=step_id,
        name="通知步骤",
        mode=mode,
        status="running",
        started_at=now,
        ended_at=0.0,
        duration_ms=0.0,
        llm_involved=False,
        input_snapshot={},
        output_snapshot={},
        escalation_result={"attempt_id": 1},
    )


# ==============================================================================
# WF-05 Tests: Notification Step Execution & Safe Templating
# ==============================================================================

def test_notification_step_success_with_template_and_redaction(tmp_path: Path):
    owner_home = tmp_path / "owner"
    owner_home.mkdir()
    run = _make_run(owner_home, "run-101", {"user_name": "Alice", "secret_token": "sk-proj-supersecret12345"})
    step = _make_step()
    spec = {
        "notification_spec": {
            "channel": "im",
            "target": "test_owner",
            "template": "Hello {user_name}, token={secret_token}, run={run_id}",
            "title": "Pipeline Notice",
        }
    }
    sent_notifications = []

    def fake_sender(channel, target, title, message, payload):
        sent_notifications.append({"channel": channel, "target": target, "title": title, "message": message, "payload": payload})
        return {"status": "ok"}

    engine = SimpleNamespace(owner_home=owner_home, notification_sender=fake_sender)

    execute_notification_step(
        engine=engine,
        run=run,
        workflow=None,
        step=step,
        spec=spec,
        context=None,
        outputs={},
    )

    assert step.status == "succeeded"
    assert step.effect_id.startswith("wf-step-notify:run-101:step-notif:1")
    assert len(sent_notifications) == 1
    msg = sent_notifications[0]["message"]
    assert "Alice" in msg
    assert "sk-proj" not in msg  # Redacted!
    assert "<redacted>" in msg

    # Verify durable receipt written under date-bucketed path
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    import hashlib
    key_hash = hashlib.sha256(step.effect_id.encode("utf-8")).hexdigest()[:24]
    receipt_raw = read_text_beneath(owner_home, ("data", "workflow_notifications", date_str, f"{key_hash}.json"))
    assert receipt_raw is not None
    receipt = json.loads(receipt_raw)
    assert receipt["status"] == "delivered"
    assert receipt["dedupe_key"] == step.effect_id


def test_notification_step_durable_deduplication(tmp_path: Path):
    owner_home = tmp_path / "owner"
    owner_home.mkdir()
    run = _make_run(owner_home, "run-102")
    step = _make_step()
    spec = {
        "notification_spec": {
            "channel": "im",
            "template": "Test deduplication notice",
        }
    }
    calls = []

    def fake_sender(channel, target, title, message, payload):
        calls.append(payload)
        return {"status": "ok"}

    engine = SimpleNamespace(owner_home=owner_home, notification_sender=fake_sender)

    # First execution -> calls transport
    execute_notification_step(engine=engine, run=run, workflow=None, step=step, spec=spec, context=None, outputs={})
    assert step.status == "succeeded"
    assert len(calls) == 1

    # Second execution with same dedupe key -> idempotent return, transport NOT called again
    step_second = _make_step()
    execute_notification_step(engine=engine, run=run, workflow=None, step=step_second, spec=spec, context=None, outputs={})
    assert step_second.status == "succeeded"
    assert len(calls) == 1  # Still 1!


def test_notification_step_invalid_spec_rejected(tmp_path: Path):
    owner_home = tmp_path / "owner"
    owner_home.mkdir()
    run = _make_run(owner_home)
    step = _make_step()
    engine = SimpleNamespace(owner_home=owner_home)

    # Missing template
    spec = {"notification_spec": {"channel": "im", "template": ""}}
    execute_notification_step(engine=engine, run=run, workflow=None, step=step, spec=spec, context=None, outputs={})
    assert step.status == "failed"
    assert step.error_code == "WORKFLOW_NOTIFICATION_SPEC_INVALID"
    assert step.needs_repair is True


def test_notification_step_cross_owner_target_forbidden(tmp_path: Path):
    owner_home = tmp_path / "owner"
    owner_home.mkdir()
    run = _make_run(owner_home)
    step = _make_step()
    engine = SimpleNamespace(owner_home=owner_home)

    spec = {
        "notification_spec": {
            "channel": "im",
            "target": "owner:other_unauthorized_owner",
            "template": "Hello",
        }
    }
    execute_notification_step(engine=engine, run=run, workflow=None, step=step, spec=spec, context=None, outputs={})
    assert step.status == "failed"
    assert step.error_code == "WORKFLOW_NOTIFICATION_TARGET_FORBIDDEN"
    assert step.needs_repair is True


def test_notification_step_unauthorized_external_url_forbidden(tmp_path: Path):
    owner_home = tmp_path / "owner"
    owner_home.mkdir()
    run = _make_run(owner_home)
    step = _make_step()
    engine = SimpleNamespace(owner_home=owner_home)

    spec = {
        "notification_spec": {
            "channel": "webhook",
            "target": "https://evil.example.com/exfiltrate",
            "template": "Hello",
        }
    }
    execute_notification_step(engine=engine, run=run, workflow=None, step=step, spec=spec, context=None, outputs={})
    assert step.status == "failed"
    assert step.error_code == "WORKFLOW_NOTIFICATION_TARGET_FORBIDDEN"
    assert step.needs_repair is True


def test_notification_step_authorized_webhook_succeeds(tmp_path: Path):
    owner_home = tmp_path / "owner"
    owner_home.mkdir()
    run = _make_run(owner_home)
    step = _make_step()

    delivered = []
    def fake_sender(**kwargs):
        delivered.append(kwargs)
        return {"status": "ok"}

    engine = SimpleNamespace(owner_home=owner_home, notification_sender=fake_sender)

    spec = {
        "notification_spec": {
            "channel": "webhook_authorized",
            "target": "https://hooks.slack.com/services/T00/B00/X00",
            "authorized_url": True,
            "template": "Webhook notification",
        }
    }
    execute_notification_step(engine=engine, run=run, workflow=None, step=step, spec=spec, context=None, outputs={})
    assert step.status == "succeeded"
    assert len(delivered) == 1
    assert delivered[0]["target"] == "https://hooks.slack.com/services/T00/B00/X00"


def test_notification_step_delivery_failure_marks_step_repair(tmp_path: Path):
    owner_home = tmp_path / "owner"
    owner_home.mkdir()
    run = _make_run(owner_home)
    step = _make_step()

    def failing_sender(**kwargs):
        raise ConnectionResetError("network unavailable")

    engine = SimpleNamespace(owner_home=owner_home, notification_sender=failing_sender)
    spec = {
        "notification_spec": {
            "channel": "im",
            "template": "Notice",
        }
    }
    execute_notification_step(engine=engine, run=run, workflow=None, step=step, spec=spec, context=None, outputs={})
    assert step.status == "failed"
    assert step.error_code == "WORKFLOW_NOTIFICATION_DELIVERY_FAILED"
    assert step.needs_repair is True


def test_safe_templating_avoids_attribute_traversal(tmp_path: Path):
    owner_home = tmp_path / "owner"
    owner_home.mkdir()
    run = _make_run(owner_home)
    step = _make_step()
    captured = []
    engine = SimpleNamespace(owner_home=owner_home, notification_sender=lambda **kw: captured.append(kw) or {"status": "ok"})

    spec = {
        "notification_spec": {
            "channel": "im",
            "template": "Inspect: {__class__} {inputs.__class__}",
        }
    }
    execute_notification_step(engine=engine, run=run, workflow=None, step=step, spec=spec, context=None, outputs={})
    assert step.status == "succeeded"
    # Placeholder was untouched or not traversed into object types
    msg = captured[0]["message"]
    assert "<class" not in msg


# ==============================================================================
# WF-05 Tests: Unattended Failure Notification
# ==============================================================================

def test_unattended_run_failure_notification_triggered(tmp_path: Path):
    owner_home = tmp_path / "owner"
    owner_home.mkdir()
    run = _make_run(owner_home, "run-fail-1")
    run.status = "failed"
    run.failed_step_id = "step-compute"
    run.error_message = "Index out of range"
    run.trigger_source = "schedule"

    notified = []
    def fake_sender(**kw):
        notified.append(kw)
        return {"status": "ok"}

    engine = SimpleNamespace(owner_home=owner_home, notification_sender=fake_sender)

    receipt = notify_unattended_run_failure(engine=engine, run=run)
    assert receipt["status"] == "notified"
    assert receipt["unattended"] is True
    assert receipt["failed_step_id"] == "step-compute"
    assert len(notified) == 1

    # Deduplicated second call returns cached receipt
    receipt2 = notify_unattended_run_failure(engine=engine, run=run)
    assert receipt2["dedupe_key"] == receipt["dedupe_key"]
    assert len(notified) == 1  # No second notification sent


def test_unattended_failure_anti_recursion_guard(tmp_path: Path):
    owner_home = tmp_path / "owner"
    owner_home.mkdir()
    run = _make_run(owner_home, "run-rec")
    run.status = "failed"
    run._suppress_failure_notification = True
    engine = SimpleNamespace(owner_home=owner_home)

    res = notify_unattended_run_failure(engine=engine, run=run)
    assert res == {"status": "skipped", "reason": "anti_recursion"}

    # Also skipped if run.failure_notification is already set
    run._suppress_failure_notification = False
    run.failure_notification = {"status": "already_done"}
    res2 = notify_unattended_run_failure(engine=engine, run=run)
    assert res2 == {"status": "skipped", "reason": "anti_recursion"}


def test_unattended_failure_transport_error_does_not_mask_run_status(tmp_path: Path):
    owner_home = tmp_path / "owner"
    owner_home.mkdir()
    run = _make_run(owner_home, "run-fail-transport")
    run.status = "failed"
    run.failed_step_id = "step-x"
    run.error_code = "ORIGINAL_STEP_ERROR"

    def bad_sender(**kw):
        raise TimeoutError("notification service down")

    engine = SimpleNamespace(owner_home=owner_home, notification_sender=bad_sender)
    receipt = notify_unattended_run_failure(engine=engine, run=run)

    assert receipt["status"] == "notification_failed"
    assert receipt["error_code"] == "WORKFLOW_FAILURE_NOTIFICATION_ERROR"
    # Crucial invariant: original run status and failed_step_id are preserved
    assert run.status == "failed"
    assert run.failed_step_id == "step-x"


# ==============================================================================
# WF-06 Tests: Lock Narrowing (WorkflowsTool & ScriptsTool)
# ==============================================================================

def test_workflows_tool_effective_resource_scopes_narrowing():
    tool = WorkflowsTool(Path("/tmp/fake_owner"))

    # Long-running execution actions MUST return () to avoid holding definition write locks
    assert tool.effective_resource_scopes({"action": "run", "workflow_id": "flow_1"}, None, Path("/tmp")) == ()
    assert tool.effective_resource_scopes({"action": "resume", "workflow_run_id": "r_1"}, None, Path("/tmp")) == ()
    assert tool.effective_resource_scopes({"action": "cancel", "workflow_run_id": "r_1"}, None, Path("/tmp")) == ()
    assert tool.effective_resource_scopes({"action": "workflows.run", "workflow_ref": "flow_1"}, None, Path("/tmp")) == ()
    assert tool.effective_resource_scopes({"action": "workflow_runs.resume", "workflow_run_id": "r_1"}, None, Path("/tmp")) == ()

    # Read-only actions return ()
    assert tool.effective_resource_scopes({"action": "list"}, None, Path("/tmp")) == ()
    assert tool.effective_resource_scopes({"action": "get", "workflow_id": "flow_1"}, None, Path("/tmp")) == ()
    assert tool.effective_resource_scopes({"action": "versions", "workflow_id": "flow_1"}, None, Path("/tmp")) == ()

    # Definition-mutating actions narrow to per-workflow identity
    assert tool.effective_resource_scopes({"action": "save", "workflow_id": "flow_1"}, None, Path("/tmp")) == ("workflow:flow_1",)
    assert tool.effective_resource_scopes({"action": "save", "workflow_id": "workflow:flow_1"}, None, Path("/tmp")) == ("workflow:flow_1",)
    assert tool.effective_resource_scopes({"action": "delete", "workflow_id": "flow_1"}, None, Path("/tmp")) == ("workflow:flow_1",)
    assert tool.effective_resource_scopes({"action": "workflows.update", "workflow_ref": "flow_2"}, None, Path("/tmp")) == ("workflow:flow_2",)
    assert tool.effective_resource_scopes({"action": "workflows.enable", "workflow_ref": "flow_2"}, None, Path("/tmp")) == ("workflow:flow_2",)

    # Creation with no supplied ID falls back to brief creation scope
    assert tool.effective_resource_scopes({"action": "workflows.create", "definition": {}}, None, Path("/tmp")) == ("workflow:create",)


def test_scripts_tool_effective_resource_scopes_narrowing():
    tool = ScriptsTool(Path("/tmp/fake_owner"))

    # Execution and read-only actions return ()
    assert tool.effective_resource_scopes({"action": "run", "script_id": "calc"}, None, Path("/tmp")) == ()
    assert tool.effective_resource_scopes({"action": "list"}, None, Path("/tmp")) == ()
    assert tool.effective_resource_scopes({"action": "get", "script_id": "calc"}, None, Path("/tmp")) == ()

    # Definition mutations lock per-script identity
    assert tool.effective_resource_scopes({"action": "register", "script_id": "calc"}, None, Path("/tmp")) == ("script:calc",)
    assert tool.effective_resource_scopes({"action": "update", "script_id": "script:calc"}, None, Path("/tmp")) == ("script:calc",)
    assert tool.effective_resource_scopes({"action": "delete", "script_id": "calc"}, None, Path("/tmp")) == ("script:calc",)
    assert tool.effective_resource_scopes({"action": "register", "descriptor": {"name": "New"}}, None, Path("/tmp")) == ("script:create",)


def test_concurrent_run_flow_a_allows_saving_flow_b(tmp_path: Path):
    """Diagnosed in Wave 1: WorkflowsTool owner lock prevented saving flow B while flow A was running.

    Verify that with lock narrowing, flow A execution has zero lock contention with saving flow B.
    """
    owner_home = tmp_path / "owner"
    owner_home.mkdir()

    tool = WorkflowsTool(owner_home)

    # Scopes for running workflow A
    run_a_scopes = tool.effective_resource_scopes({"action": "run", "workflow_id": "flow_a"}, None, owner_home)
    assert run_a_scopes == ()  # Run does not claim definition lock

    # Scopes for saving unrelated workflow B
    save_b_scopes = tool.effective_resource_scopes({"action": "save", "workflow_id": "flow_b"}, None, owner_home)
    assert save_b_scopes == ("workflow:flow_b",)

    # Scopes for editing workflow A while running
    save_a_scopes = tool.effective_resource_scopes({"action": "save", "workflow_id": "flow_a"}, None, owner_home)
    assert save_a_scopes == ("workflow:flow_a",)

    # Sets of scopes do not overlap with run_a
    assert set(run_a_scopes).intersection(set(save_b_scopes)) == set()
    assert set(run_a_scopes).intersection(set(save_a_scopes)) == set()
    assert set(save_b_scopes).intersection(set(save_a_scopes)) == set()


# ==============================================================================
# version_hook.py Fix Tests: Missing Referenced script_id
# ==============================================================================

def test_version_hook_missing_referenced_script_id_does_not_fail(tmp_path: Path):
    """When a workflow step references a script_id missing from ScriptStore,

    version_hook cleanly retains publication debt without deadlocking per-workflow locks.
    """
    owner_home = tmp_path / "owner"
    owner_home.mkdir()

    store = WorkflowStore(owner_home)
    definition = WorkflowDefinition(
        AssetDescriptor("test-flow", "workflow", "测试缺失脚本引用的工作流"),
        (
            WorkflowStepSpec("step1", "第一步", "deterministic_script", script_id="synthetic-never-run-script"),
        ),
    )

    # Save the workflow definition succeeds without deadlocking
    saved = store.save_workflow(definition)
    assert saved.descriptor.id == "test-flow"

    # Forward reference script is not yet published, so version publication stays pending
    latest = latest_version(owner_home, "workflow", "test-flow")
    assert latest is None

    # Verify that pending publication debt was retained with error_code
    pending = pending_publication(owner_home, "workflow", "test-flow")
    assert pending is not None
    assert pending["snapshot"]["error_code"] == "TOOL_EXECUTION_FAILED"


def test_version_hook_genuine_script_pins_correctly_when_present(tmp_path: Path):
    """When referenced script exists in ScriptStore, version_hook pins its exact version."""
    from agent_py_agent.agent.assets.script_store import ScriptStore

    owner_home = tmp_path / "owner"
    owner_home.mkdir()

    script_store = ScriptStore(owner_home)
    script_desc = AssetDescriptor("real-script", "script", "真实脚本")
    script_store.save_script(script_desc, "print('hello')", entrypoint="main.py", interpreter="python3")

    wf_store = WorkflowStore(owner_home)
    wf_def = WorkflowDefinition(
        AssetDescriptor("test-wf-real", "workflow", "测试真实脚本"),
        (
            WorkflowStepSpec("step1", "执行", "deterministic_script", script_id="real-script"),
        ),
    )
    wf_store.save_workflow(wf_def)

    latest = latest_version(owner_home, "workflow", "test-wf-real")
    assert latest is not None
    pins = latest.get("pins", {}).get("scripts", {})
    assert "real-script" in pins
    assert "version" in pins["real-script"]
    assert "code_sha256" in pins["real-script"]


def test_workflow_engine_executes_notification_step_formal_run(tmp_path: Path):
    """End-to-end integration: WorkflowEngine dispatches notification step in formal DAG run."""
    owner_home = tmp_path / "owner"
    owner_home.mkdir()

    delivered = []
    def fake_sender(**kw):
        delivered.append(kw)
        return {"status": "ok"}

    engine = WorkflowEngine(owner_home)
    engine.notification_sender = fake_sender

    wf_store = WorkflowStore(owner_home)
    step1 = WorkflowStepSpec("notif1", "发送通知", "notification", notification_spec={
        "channel": "owner_inbox",
        "template": "Formal run {run_id} started with val={inputs.test_val}",
    })
    wf_def = WorkflowDefinition(
        AssetDescriptor("test-flow-formal-notif", "workflow", "测试正式通知DAG"),
        (step1,),
    )
    wf_store.save_workflow(wf_def)

    result = engine.run_workflow(wf_def.to_dict(), inputs={"test_val": "value_42"})
    assert result.status == "succeeded"
    assert len(delivered) == 1
    assert "value_42" in delivered[0]["message"]
    assert delivered[0]["channel"] == "owner_inbox"


def test_reference_sources_dynamic_host_conversation_store(tmp_path: Path):
    """WorkflowReferenceReader dynamically resolves host.conversation_store.session_tasks."""
    from agent_py_agent.agent.workflows.reference_sources import build_workflow_reference_reader

    owner_home = tmp_path / "owner"
    owner_home.mkdir()

    host = SimpleNamespace(owner_home=owner_home, conversation_store=None)
    reader = build_workflow_reference_reader(host)

    mock_tasks = SimpleNamespace(
        list_page=lambda limit=100: {
            "items": [
                SimpleNamespace(
                    status="running",
                    task_id="task_live_1",
                    dedupe_key="wf:flow_dyn_target:step1",
                    metadata={},
                )
            ]
        }
    )
    host.conversation_store = SimpleNamespace(session_tasks=mock_tasks)

    result = reader("flow_dyn_target")
    blockers = [b for b in result["blockers"] if b["kind"] == "session_task"]
    assert len(blockers) == 1
    assert blockers[0]["ref"] == "task_live_1"


def test_reference_sources_index_queried_ok_skips_runs_dir_walk(tmp_path: Path, monkeypatch):
    """When RunListIndex query completes without exception, runs_root.iterdir() is not called."""
    from agent_py_agent.agent.workflows.reference_sources import build_workflow_reference_reader
    from agent_py_agent.agent.workflows.run_store import WorkflowRunStore

    owner_home = tmp_path / "owner"
    owner_home.mkdir()

    run_store = WorkflowRunStore(owner_home)
    run_rec = _make_run(owner_home, "run_idx_1")
    run_rec.workflow_id = "flow_indexed"
    run_store.create_run(run_rec, {})

    reader = build_workflow_reference_reader(owner_home)

    iterdir_called = []
    real_iterdir = Path.iterdir

    def spy_iterdir(path_obj):
        if "workflow_runs" in str(path_obj):
            iterdir_called.append(str(path_obj))
        return real_iterdir(path_obj)

    monkeypatch.setattr(Path, "iterdir", spy_iterdir)

    result = reader("flow_indexed")
    assert any(b["ref"] == "run_idx_1" for b in result["blockers"])
    assert iterdir_called == []
