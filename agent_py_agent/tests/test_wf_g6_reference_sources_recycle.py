"""Comprehensive verification for G6 WorkflowReferenceSources.v1, BIZ-012, and V08-01..07 lifecycle.

Covers:
1. Normalization of workflow IDs (bare vs workflow: prefix, whitespace).
2. Authoritative detection of all 5 reference sources:
   - Scheduler rules / jobs (kind: 'schedule')
   - Nested subworkflow steps (kind: 'workflow')
   - Business space bindings / governance policies (kind: 'space')
   - Non-settled WorkflowRunStore runs (kind: 'workflow_run')
   - Open SessionTask records (kind: 'session_task')
3. Fail-closed unreadable/corrupted store coverage ('unknown').
4. Full end-to-end lifecycle:
   - create -> cleanup_status -> recycle -> recycled.list -> restore
   - blocker refusal (WORKFLOW_CLEANUP_BLOCKED) -> unbind -> recycle success
   - batch maintain_owner_workflows execution with counts
"""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_py_agent.agent.assets.descriptor import AssetDescriptor
from agent_py_agent.agent.assets.workflow_store import (
    WorkflowDefinition,
    WorkflowStepSpec,
    WorkflowStore,
    normalize_workflow_asset_id,
)
from agent_py_agent.agent.conversation.session_tasks import (
    SESSION_TASK_ACCEPTED,
    SESSION_TASK_DONE,
    SESSION_TASK_QUEUED,
    SessionTask,
    SessionTaskDraft,
    SessionTaskStore,
)
from agent_py_agent.agent.conversation.store_layout import ConversationStorage
from agent_py_agent.agent.workflows.business_scope import (
    WorkflowPolicyRefusal,
    WorkflowScope,
    WorkflowScopeService,
    with_state,
    workflow_state,
)
from agent_py_agent.agent.workflows.models import WorkflowRunRecord
from agent_py_agent.agent.workflows.orphan_maintenance import (
    cleanup_action,
    cleanup_status,
    maintain_owner_workflows,
)
from agent_py_agent.agent.workflows.reference_sources import (
    WorkflowManagementService,
    WorkflowReferenceReader,
    build_workflow_reference_reader,
)
from agent_py_agent.agent.workflows.run_store import WorkflowRunStore


def _flow_def(flow_id: str, *, steps: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "descriptor": {
            "id": flow_id,
            "kind": "workflow",
            "name": f"Workflow {flow_id}",
            "access_targets": [],
            "data_sources": [],
            "credential_refs": [],
            "io_contract": {},
        },
        "steps": steps or [],
    }


def _create_unassigned_flow(service: WorkflowScopeService, actor: Any, flow_id: str) -> dict[str, Any]:
    return service.execute(
        "workflows.create",
        {"definition": _flow_def(flow_id), "scope": {"scope_mode": "unassigned", "space_ids": []}},
        actor=actor,
        idempotency_key=f"create-{flow_id}",
    )


def test_normalize_workflow_asset_id_edge_cases():
    assert normalize_workflow_asset_id("flow_1") == "flow_1"
    assert normalize_workflow_asset_id("workflow:flow_1") == "flow_1"
    assert normalize_workflow_asset_id("  workflow:flow_1  ") == "flow_1"
    assert normalize_workflow_asset_id("  flow_1  ") == "flow_1"
    assert normalize_workflow_asset_id("") == ""
    assert normalize_workflow_asset_id(None) is None
    assert normalize_workflow_asset_id(123) == 123


def test_empty_owner_clean_complete_coverage(tmp_path: Path):
    reader = build_workflow_reference_reader(tmp_path)
    result = reader("my_flow")
    assert result == {"coverage": "complete", "blockers": []}


def test_source_1_scheduler_job_references(tmp_path: Path):
    class FakeScheduler:
        def __init__(self, jobs: list[dict[str, Any]]):
            self._jobs = jobs

        def list_jobs(self, include_deleted: bool = False):
            return self._jobs, []

    # 1. Matching target
    job1 = {
        "job_id": "job_101",
        "status": "active",
        "execution_target": {"kind": "workflow", "asset_id": "flow_target"},
    }
    # 2. Matching target in rules
    job2 = {
        "job_id": "job_102",
        "status": "active",
        "rules": [{"execution_target": "workflow:flow_target"}],
    }
    # 3. Deleted job matching target -> must NOT block
    job3 = {
        "job_id": "job_103",
        "status": "deleted",
        "execution_target": {"kind": "workflow", "asset_id": "flow_target"},
    }
    # 4. Job referencing different workflow -> must NOT block
    job4 = {
        "job_id": "job_104",
        "status": "active",
        "execution_target": {"kind": "workflow", "asset_id": "other_flow"},
    }

    scheduler = FakeScheduler([job1, job2, job3, job4])
    reader = build_workflow_reference_reader(tmp_path, scheduler_repo=scheduler)

    result = reader("flow_target")
    assert result["coverage"] == "complete"
    assert len(result["blockers"]) == 2
    assert {"ref": "job_101", "kind": "schedule", "reason_code": "REFERENCED_BY_SCHEDULE"} in result["blockers"]
    assert {"ref": "job_102", "kind": "schedule", "reason_code": "REFERENCED_BY_SCHEDULE"} in result["blockers"]
    # Check exact keys
    for b in result["blockers"]:
        assert set(b.keys()) == {"ref", "kind", "reason_code"}


def test_source_2_nested_subworkflows(tmp_path: Path):
    actor = SimpleNamespace(owner_id="alice")
    service = WorkflowManagementService(tmp_path, owner_id="alice", authorize=lambda a, act, ref: True)

    # 1. Target workflow itself
    _create_unassigned_flow(service, actor, "flow_target")

    # 2. Active parent workflow referencing flow_target
    service.execute(
        "workflows.create",
        {
            "definition": _flow_def(
                "flow_parent",
                steps=[{"step_id": "step1", "name": "Call Sub", "mode": "subworkflow", "subworkflow_ref": "workflow:flow_target"}],
            ),
            "scope": {"scope_mode": "unassigned", "space_ids": []},
        },
        actor=actor,
        idempotency_key="create-parent",
    )

    # 3. Recycled parent workflow referencing flow_target -> must NOT block
    service.execute(
        "workflows.create",
        {
            "definition": _flow_def(
                "flow_recycled",
                steps=[{"step_id": "step1", "name": "Call Sub", "mode": "subworkflow", "subworkflow_ref": "flow_target"}],
            ),
            "scope": {"scope_mode": "unassigned", "space_ids": []},
        },
        actor=actor,
        idempotency_key="create-recycled",
    )
    service.execute(
        "workflows.recycle",
        {"workflow_ref": "flow_recycled"},
        actor=actor,
        expected_revision=1,
        idempotency_key="recycle-parent",
    )

    reader = build_workflow_reference_reader(tmp_path)
    result = reader("flow_target")
    assert result["coverage"] == "complete"
    assert result["blockers"] == [
        {"ref": "flow_parent", "kind": "workflow", "reason_code": "REFERENCED_BY_SUBWORKFLOW"}
    ]


def test_source_3_business_space_catalog(tmp_path: Path):
    business_dir = tmp_path / "data" / "business"
    business_dir.mkdir(parents=True, exist_ok=True)
    catalog = {
        "spaces": {
            "sp_active_mount": {
                "space_id": "sp_active_mount",
                "state": "active",
                "mounts": [{"package_ref": "workflow:flow_mount"}],
            },
            "sp_active_bound": {
                "space_id": "sp_active_bound",
                "state": "active",
                "workflows": ["flow_mount"],
            },
            "sp_archived": {
                "space_id": "sp_archived",
                "state": "archived",
                "mounts": [{"package_ref": "flow_mount"}],
            },
        },
        "bindings": {
            "bind_1": {
                "binding_id": "bind_1",
                "asset_id": "flow_bound",
                "state": "bound",
                "space_ids": ["sp_active_mount"],
            },
            "bind_unbound": {
                "binding_id": "bind_unbound",
                "asset_id": "flow_bound",
                "state": "unbound",
                "space_ids": ["sp_active_mount"],
            },
        },
    }
    (business_dir / "catalog.json").write_text(json.dumps(catalog), encoding="utf-8")

    reader = build_workflow_reference_reader(tmp_path)
    res_mount = reader("flow_mount")
    assert res_mount["coverage"] == "complete"
    assert {"ref": "sp_active_mount", "kind": "space", "reason_code": "REFERENCED_BY_SPACE"} in res_mount["blockers"]
    assert {"ref": "sp_active_bound", "kind": "space", "reason_code": "REFERENCED_BY_SPACE"} in res_mount["blockers"]
    assert not any(b["ref"] == "sp_archived" for b in res_mount["blockers"])

    res_bound = reader("flow_bound")
    assert res_bound["coverage"] == "complete"
    assert res_bound["blockers"] == [
        {"ref": "sp_active_mount", "kind": "space", "reason_code": "REFERENCED_BY_SPACE"}
    ]


def test_source_4_active_workflow_runs(tmp_path: Path):
    run_store = WorkflowRunStore(tmp_path)

    # Active run: running
    run_active = WorkflowRunRecord(
        "run_101",
        "flow_run_target",
        "alice",
        "manual",
        "running",
        1.0,
        0.0,
        0.0,
    )
    run_store.create_run(run_active, {})

    # Active run: paused_for_resume
    run_paused = WorkflowRunRecord(
        "run_102",
        "flow_run_target",
        "alice",
        "manual",
        "paused_for_resume",
        1.0,
        0.0,
        0.0,
    )
    run_store.create_run(run_paused, {})

    # Terminal run: succeeded -> must NOT block
    run_done = WorkflowRunRecord(
        "run_103",
        "flow_run_target",
        "alice",
        "manual",
        "succeeded",
        1.0,
        2.0,
        1.0,
    )
    run_store.create_run(run_done, {})

    reader = build_workflow_reference_reader(tmp_path, run_store=run_store)
    result = reader("flow_run_target")
    assert result["coverage"] == "complete"
    refs = {b["ref"] for b in result["blockers"]}
    assert refs == {"run_101", "run_102"}
    for b in result["blockers"]:
        assert b["kind"] == "workflow_run"
        assert b["reason_code"] == "ACTIVE_WORKFLOW_RUN"


def test_source_5_open_session_tasks(tmp_path: Path):
    conv_dir = tmp_path / "conversation"
    storage = ConversationStorage(conv_dir)
    task_store = SessionTaskStore(storage)

    # 1. 任务的显式工作流引用；去重文本只作幂等键。
    t1 = task_store.create(
        SessionTaskDraft(
            sender_thread_id="t1",
            target_thread_id="t2",
            goal="run workflow",
            dedupe_key="workflow:run_555:step_1:flow_task_target",
        )
    )
    task_file = storage.session_tasks_dir / f"{t1.task_id}.json"
    data = json.loads(task_file.read_text(encoding="utf-8"))
    data["metadata"] = {"workflow_ref": "workflow:flow_task_target"}
    task_file.write_text(json.dumps(data), encoding="utf-8")

    # 2. Open task with metadata business_context
    t2 = task_store.create(
        SessionTaskDraft(
            sender_thread_id="t1",
            target_thread_id="t2",
            goal="task with bc",
            dedupe_key="second_task_key",
        )
    )
    # Inject business_context into task JSON on disk
    task_file = storage.session_tasks_dir / f"{t2.task_id}.json"
    data = json.loads(task_file.read_text(encoding="utf-8"))
    data["metadata"] = {"business_context": {"workflow_id": "flow_task_target"}}
    task_file.write_text(json.dumps(data), encoding="utf-8")

    reader = build_workflow_reference_reader(tmp_path, session_task_store=task_store)
    result = reader("flow_task_target")
    assert result["coverage"] == "complete"
    assert len(result["blockers"]) >= 2
    refs = {b["ref"] for b in result["blockers"]}
    assert t1.task_id in refs
    assert t2.task_id in refs
    for b in result["blockers"]:
        assert b["kind"] == "session_task"
        assert b["reason_code"] == "OPEN_SESSION_TASK"


def test_corrupt_store_fail_closed_unknown_coverage(tmp_path: Path):
    business_dir = tmp_path / "data" / "business"
    business_dir.mkdir(parents=True, exist_ok=True)
    # Corrupt catalog.json
    (business_dir / "catalog.json").write_text("{corrupt json", encoding="utf-8")

    reader = build_workflow_reference_reader(tmp_path)
    result = reader("any_flow")
    assert result["coverage"] == "unknown"


def test_lifecycle_recycle_restore_and_blockers(tmp_path: Path):
    clock = [1_700_000_000.0]
    actor = SimpleNamespace(owner_id="alice")

    service = WorkflowManagementService(
        tmp_path,
        owner_id="alice",
        authorize=lambda a, act, ref: True,
        clock=lambda: clock[0],
    )

    # 1. Create unassigned workflow
    _create_unassigned_flow(service, actor, "flow_lifecycle")

    # Initial status: unreferenced, unassigned -> blockers == []
    def_wf = service._get("flow_lifecycle")
    st = cleanup_status(service, def_wf)
    assert st["blockers"] == []

    # 2. Recycle workflow succeeds
    recycled = service.execute(
        "workflows.recycle",
        {"workflow_ref": "flow_lifecycle"},
        actor=actor,
        expected_revision=def_wf.descriptor.revision,
        idempotency_key="recycle-1",
    )
    assert recycled["lifecycle"] == "recycled"

    # 3. Appears in workflows.recycled.list
    recycled_list = service.execute("workflows.recycled.list", {}, actor=actor)
    assert any(item["workflow_ref"] == "flow_lifecycle" for item in recycled_list["items"])

    # 4. Restore workflow succeeds
    def_recycled = service._get("flow_lifecycle")
    restored = service.execute(
        "workflows.restore",
        {"workflow_ref": "flow_lifecycle", "scope": {"scope_mode": "unassigned", "space_ids": []}},
        actor=actor,
        expected_revision=def_recycled.descriptor.revision,
        idempotency_key="restore-1",
    )
    assert restored["workflow"]["lifecycle"] == "active"

    # 5. Add a reference blocker (parent subworkflow)
    parent_def = WorkflowDefinition(
        descriptor=AssetDescriptor(id="blocker_parent", kind="workflow", name="Blocker Parent"),
        steps=(
            WorkflowStepSpec(step_id="sub", name="Call", mode="subworkflow", subworkflow_ref="flow_lifecycle"),
        ),
    )
    service.store.save_workflow(parent_def)

    # Check cleanup_status detects the blocker
    st_blocked = cleanup_status(service, service._get("flow_lifecycle"))
    assert len(st_blocked["blockers"]) == 1
    assert st_blocked["blockers"][0]["reason_code"] == "REFERENCED_BY_SUBWORKFLOW"

    # workflows.recycle refuses with WORKFLOW_CLEANUP_BLOCKED
    with pytest.raises(WorkflowPolicyRefusal) as exc_info:
        service.execute(
            "workflows.recycle",
            {"workflow_ref": "flow_lifecycle"},
            actor=actor,
            expected_revision=service._get("flow_lifecycle").descriptor.revision,
            idempotency_key="recycle-blocked",
        )
    assert exc_info.value.code in ("WORKFLOW_CLEANUP_BLOCKED", "WORKFLOW_REFERENCED_OR_COVERAGE_UNKNOWN")

    # 6. Unbind blocker (delete or recycle parent)
    service.store.delete_workflow("blocker_parent")

    # Now recycle succeeds
    recycled_after = service.execute(
        "workflows.recycle",
        {"workflow_ref": "flow_lifecycle"},
        actor=actor,
        expected_revision=service._get("flow_lifecycle").descriptor.revision,
        idempotency_key="recycle-2",
    )
    assert recycled_after["lifecycle"] == "recycled"


def test_batch_maintain_owner_workflows_with_reference_sources(tmp_path: Path):
    clock = [1_700_000_000.0]
    actor = SimpleNamespace(owner_id="alice")

    service = WorkflowManagementService(
        tmp_path,
        owner_id="alice",
        authorize=lambda a, act, ref: True,
        clock=lambda: clock[0],
    )

    # Create unassigned workflow
    _create_unassigned_flow(service, actor, "flow_batch")

    # Advance clock past orphan_after_days (30 days)
    clock[0] += 31 * 86400

    # Preview cleanup
    preview = cleanup_action(
        service,
        "workflows.cleanup.preview",
        {},
        actor=actor,
        expected_revision=None,
        idempotency_key="preview-1",
    )
    assert len(preview["eligible"]) == 1
    assert preview["eligible"][0]["workflow_ref"] == "flow_batch"

    # Run batch maintenance
    run_res = maintain_owner_workflows(service, actor=actor, idempotency_key="run-batch-1")
    assert run_res["state"] == "completed"
    assert run_res["counts"]["recycled"] == 1

    # Verify flow is recycled
    assert workflow_state(service._get("flow_batch"))["lifecycle"] == "recycled"
