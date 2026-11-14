from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace


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
            status="DONE",
            result="complete",
        )
    )

    assert updated.status == "DONE"
    assert updated.result == "complete"


def test_lifecycle_set_status_rejects_non_protocol_status():
    from agent_py_agent.agent.subagents.services.lifecycle import (
        SetStatusParams,
        SubAgentLifecycleService,
    )

    task = _LifecycleTask(id="run-status")
    manager = SimpleNamespace(load=lambda run_id: task, save=lambda task: None)
    service = SubAgentLifecycleService(manager)

    try:
        service.set_status(SetStatusParams(run_id="run-status", status="completed"))
    except ValueError as exc:
        assert str(exc) == "subagent_status_invalid"
    else:
        raise AssertionError("non-protocol status must not be written to task.status")
    assert task.status == "RUNNING"
