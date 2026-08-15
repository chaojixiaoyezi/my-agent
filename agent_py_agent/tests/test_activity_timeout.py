"""Focused tests for long-task activity timeout and recovery contracts."""

from __future__ import annotations


def test_activity_timeout_keeps_recently_active_task_running():
    from agent_py_agent.agent.contracts.activity_timeout import (
        ActivitySnapshot,
        ActivityTimeoutPolicy,
        decide_activity_timeout,
    )

    decision = decide_activity_timeout(
        ActivityTimeoutPolicy(idle_timeout_seconds=120, wall_timeout_seconds=300),
        ActivitySnapshot(
            started_at=0,
            now=900,
            last_activity_at=880,
            latest_checkpoint_ref="checkpoint.json",
            active_tool_count=1,
        ),
    )

    assert decision.timed_out is False
    assert decision.action == "keep_running"
    assert decision.recovery_refs["latest_checkpoint_ref"] == "checkpoint.json"


def test_activity_timeout_pauses_idle_task_with_recovery_refs():
    from agent_py_agent.agent.contracts.activity_timeout import (
        ActivitySnapshot,
        ActivityTimeoutPolicy,
        decide_activity_timeout,
    )

    decision = decide_activity_timeout(
        ActivityTimeoutPolicy(idle_timeout_seconds=120),
        ActivitySnapshot(
            started_at=0,
            now=500,
            last_activity_at=100,
            latest_checkpoint_ref="checkpoint.json",
            latest_recovery_snapshot_ref="snapshot.json",
        ),
    )

    assert decision.timed_out is True
    assert decision.action == "write_recovery_and_pause"
    assert decision.reason == "idle_timeout"
    assert decision.recovery_refs == {
        "latest_checkpoint_ref": "checkpoint.json",
        "latest_recovery_snapshot_ref": "snapshot.json",
    }


def test_activity_timeout_uses_started_at_when_activity_missing():
    from agent_py_agent.agent.contracts.activity_timeout import (
        ActivitySnapshot,
        ActivityTimeoutPolicy,
        decide_activity_timeout,
    )

    decision = decide_activity_timeout(
        ActivityTimeoutPolicy(idle_timeout_seconds=10),
        ActivitySnapshot(started_at=20, now=35, last_activity_at=0),
    )

    assert decision.timed_out is True
    assert decision.reason == "idle_timeout"
