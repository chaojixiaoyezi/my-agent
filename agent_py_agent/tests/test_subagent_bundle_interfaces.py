from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace


def test_patch_review_task_request_bundle_dry_run(tmp_path: Path):
    from agent_py_agent.agent.subagents.patch import (
        PatchReviewOptions,
        PatchReviewService,
        PatchReviewTaskRequest,
    )

    task = SimpleNamespace(
        id="run-1",
        output_json=str(tmp_path / "output.json"),
        work_log_file=str(tmp_path / "WORK_LOG.md"),
    )
    service = PatchReviewService(manager=SimpleNamespace())

    record = service._review_patch_task(
        PatchReviewTaskRequest(
            task=task,
            output={"patches": []},
            patches=[{"path": "a.py", "status": "applied"}],
            options=PatchReviewOptions(reviewer="reviewer-b", note="bundle"),
        )
    )

    assert record.dry_run is True
    assert record.ok is True
    assert record.reviewer == "reviewer-b"
    assert record.note == "bundle"


def test_patch_apply_task_accepts_params_bundle(tmp_path: Path):
    from agent_py_agent.agent.subagents.patch.patch_apply import PatchApplyService
    from agent_py_agent.agent.subagents.patch.patch_apply_task import ApplyPatchTaskParams

    output_path = tmp_path / "output.json"
    output_path.write_text('{"patches": []}', encoding="utf-8")
    task = SimpleNamespace(
        id="run-apply",
        output_json=str(output_path),
        work_log_file=str(tmp_path / "WORK_LOG.md"),
        acceptance_checks=[],
    )
    manager = SimpleNamespace(workspace=tmp_path, workspace_root=tmp_path)
    service = PatchApplyService(manager)

    record = service._apply_patch_task(
        task,
        params=ApplyPatchTaskParams(
            output={"patches": []},
            patches=[],
            apply=False,
            applier="applier-b",
            note="bundle",
        ),
    )

    assert record.decision == "NO_PATCHES"
    assert record.applier == "applier-b"
    assert record.note == "bundle"


def test_action_record_after_task_accepts_params_bundle():
    from agent_py_agent.agent.subagents.services.actions import (
        RecordAfterTaskActionParams,
        SubAgentActionService,
    )

    manager = SimpleNamespace(_new_id=lambda prefix: f"{prefix}-1")
    service = SubAgentActionService(manager)
    action = SimpleNamespace(id="action-1", run_id="run-1", action="run_acceptance")
    task = SimpleNamespace(status="DONE", channel_status="OK", work_log_file="WORK_LOG.md")

    record = service._record_after_task_action(
        RecordAfterTaskActionParams(
            action=action,
            task=task,
            before_status="RUNNING",
            before_channel_status="OK",
            message="bundle record",
        )
    )

    assert record.action_id == "action-1"
    assert record.before_status == "RUNNING"
    assert record.after_status == "DONE"


@dataclass
class _LifecycleTask:
    id: str
    status: str = "RUNNING"
    evidence: list = field(default_factory=list)
    result: str = ""
    failure_type: str = ""
    ended_at: float = 0.0
    updated_at: float = 0.0


def test_lifecycle_set_status_accepts_params_bundle():
    from agent_py_agent.agent.subagents.services.lifecycle import (
        SetStatusParams,
        SubAgentLifecycleService,
    )

    task = _LifecycleTask(id="run-status", evidence=["checked"])
    manager = SimpleNamespace(load=lambda run_id: task, save=lambda task: None)
    service = SubAgentLifecycleService(manager)

    updated = service.set_status(
        SetStatusParams(
            run_id="run-status",
            status="done",
            result="complete",
            require_evidence=True,
        )
    )

    assert updated.status == "DONE"
    assert updated.result == "complete"
