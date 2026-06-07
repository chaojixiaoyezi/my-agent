from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.orchestration.dispatch import (
    workflow_records as workflow_records,
)


def test_build_workflow_records_respects_task_workflow_off(monkeypatch: pytest.MonkeyPatch) -> None:
    task = SimpleNamespace(
        id="worker-1",
        parent_id="",
        workflow_parent_run_id="",
        status="PLANNING",
        workflow_mode="off",
    )
    agent = SimpleNamespace()
    ctx = SimpleNamespace(normalized_workflow_mode="auto", limit=20, apply=True)

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("workflow planner should not run for task.workflow_mode=off")

    monkeypatch.setattr(workflow_records, "_try_workflow_plan", fail_if_called)

    result = workflow_records.build_workflow_records(
        agent,
        ctx,
        [task],
    )

    assert result == []


@pytest.mark.parametrize("status", ["CANCELLED", "ABANDONED", "PAUSED"])
def test_build_workflow_records_skips_dispatch_ineligible_statuses(
    monkeypatch: pytest.MonkeyPatch,
    status: str,
) -> None:
    task = SimpleNamespace(
        id=f"worker-{status.lower()}",
        parent_id="",
        workflow_parent_run_id="",
        status=status,
        workflow_mode="auto",
    )
    agent = SimpleNamespace()
    ctx = SimpleNamespace(normalized_workflow_mode="auto", limit=20, mutate_state=True)

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError(f"workflow planner should not run for {status}")

    monkeypatch.setattr(workflow_records, "_try_workflow_plan", fail_if_called)

    result = workflow_records.build_workflow_records(
        agent,
        ctx,
        [task],
    )

    assert result == []
