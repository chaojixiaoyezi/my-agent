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
        verification_status="NEEDS_ACCEPTANCE",
        channel_status="OK",
        has_progress=True,
    )

    snapshot = run_state_snapshot_from_task(task)

    assert snapshot["lifecycle_phase"] == "VERIFYING"
    assert snapshot["recovery_decision"]["action"] == "wait_for_acceptance"
    assert snapshot["can_closeout"] is False


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
    assert snapshot["recovery_decision"]["action"] == "repair_or_probe_channel"
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
