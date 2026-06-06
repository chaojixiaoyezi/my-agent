from __future__ import annotations

from types import SimpleNamespace


def test_run_state_snapshot_projects_running_without_progress_to_waiting_for_local_progress():
    from agent_py_agent.agent.contracts.state_machine import run_state_snapshot_from_task

    task = SimpleNamespace(
        id="run-1",
        status="RUNNING",
        verification_status="UNVERIFIED",
        channel_status="OK",
        has_progress=False,
    )

    snapshot = run_state_snapshot_from_task(task)

    assert snapshot["lifecycle_phase"] == "WAITING_FOR_LOCAL_PROGRESS"
    assert snapshot["recovery_decision"]["action"] == "wait_for_local_progress"
    assert snapshot["can_closeout"] is False


def test_run_state_snapshot_projects_done_without_acceptance_to_verifying():
    from agent_py_agent.agent.contracts.state_machine import run_state_snapshot_from_task

    task = SimpleNamespace(
        id="run-2",
        status="DONE",
        verification_status="VERIFIED",
        channel_status="OK",
        has_progress=True,
    )

    snapshot = run_state_snapshot_from_task(task)

    assert snapshot["lifecycle_phase"] == "DONE"
    assert snapshot["recovery_decision"]["action"] == "closeout"
    assert snapshot["can_closeout"] is True


def test_run_state_snapshot_projects_broken_channel_to_blocked():
    from agent_py_agent.agent.contracts.state_machine import run_state_snapshot_from_task

    task = SimpleNamespace(
        id="run-3",
        status="DONE",
        verification_status="VERIFIED",
        channel_status="BROKEN",
        has_progress=True,
    )

    snapshot = run_state_snapshot_from_task(task)

    assert snapshot["channel_status"] == "BROKEN"
    assert snapshot["lifecycle_phase"] == "BLOCKED"
    assert snapshot["recovery_decision"]["action"] == "repair_channel"
    assert snapshot["can_closeout"] is False


def test_run_state_snapshot_projects_verified_done_to_done():
    from agent_py_agent.agent.contracts.state_machine import run_state_snapshot_from_task

    task = SimpleNamespace(
        id="run-4",
        status="DONE",
        verification_status="VERIFIED",
        channel_status="OK",
        has_progress=True,
    )

    snapshot = run_state_snapshot_from_task(task)

    assert snapshot["lifecycle_phase"] == "DONE"
    assert snapshot["recovery_decision"]["action"] == "closeout"
    assert snapshot["can_closeout"] is True


def test_run_state_snapshot_normalizes_previous_planned_status_to_planning():
    from agent_py_agent.agent.contracts.state_machine import run_state_snapshot_from_task

    task = SimpleNamespace(
        id="run-planned",
        status="PLANNED",
        verification_status="UNVERIFIED",
        channel_status="OK",
        has_progress=True,
    )

    snapshot = run_state_snapshot_from_task(task)

    assert snapshot["status"] == "PLANNING"
    assert snapshot["lifecycle_phase"] == "PLANNING"
    assert snapshot["can_dispatch"] is True


def test_run_state_snapshot_does_not_promote_completed_alias_to_done():
    from agent_py_agent.agent.contracts.state_machine import run_state_snapshot_from_task

    task = SimpleNamespace(
        id="run-completed-alias",
        status="COMPLETED",
        verification_status="VERIFIED",
        channel_status="OK",
        has_progress=True,
    )

    snapshot = run_state_snapshot_from_task(task)

    assert snapshot["status"] == "COMPLETED"
    assert snapshot["lifecycle_phase"] == "COMPLETED"
    assert snapshot["can_closeout"] is False
    assert snapshot["recovery_decision"]["action"] == "manual_review"


def test_run_state_snapshot_projects_approval_wait_to_waiting_for_user():
    from agent_py_agent.agent.contracts.state_machine import run_state_snapshot_from_task

    task = SimpleNamespace(
        id="run-5",
        status="BLOCKED",
        verification_status="UNVERIFIED",
        channel_status="OK",
        failure_type="APPROVAL_REQUIRED",
        has_progress=True,
    )

    snapshot = run_state_snapshot_from_task(task)

    assert snapshot["lifecycle_phase"] == "WAITING_FOR_USER"
    assert snapshot["waiting_reason"] == "approval"
    assert snapshot["terminal_outcome"] == "blocked"
    assert snapshot["recovery_decision"]["action"] == "request_approval"
    assert snapshot["recovery_decision"]["secondary_action"] == "stop"


def test_run_state_snapshot_projects_waiting_for_tool_to_tool_wait_reason():
    from agent_py_agent.agent.contracts.state_machine import run_state_snapshot_from_task

    task = SimpleNamespace(
        id="run-6",
        status="WAITING_FOR_TOOL",
        verification_status="UNVERIFIED",
        channel_status="OK",
        has_progress=True,
    )

    snapshot = run_state_snapshot_from_task(task)

    assert snapshot["lifecycle_phase"] == "WAITING_FOR_TOOL"
    assert snapshot["waiting_reason"] == "tool"
    assert snapshot["terminal_outcome"] == "active"


def test_run_state_snapshot_projects_timeout_to_timeout_terminal_outcome():
    from agent_py_agent.agent.contracts.state_machine import run_state_snapshot_from_task

    task = SimpleNamespace(
        id="run-7",
        status="TIMEOUT",
        verification_status="UNVERIFIED",
        channel_status="OK",
        runner_last_error="tool timed out after 240 seconds",
        has_progress=False,
    )

    snapshot = run_state_snapshot_from_task(task)

    assert snapshot["lifecycle_phase"] == "BLOCKED"
    assert snapshot["waiting_reason"] == "none"
    assert snapshot["terminal_outcome"] == "timed_out"
    assert snapshot["recovery_decision"]["action"] == "repair"
