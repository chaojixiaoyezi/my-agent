"""Focused tests for long-task activity timeout and recovery contracts."""

from __future__ import annotations


# LLM: Active tools should keep long tasks alive even when wall-clock time is high.
# 函数用途: 验证长任务只要最近仍有活动，就不会因为总耗时长被误杀。
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


# LLM: Idle timeout should return a structured recovery action with checkpoint refs.
# 函数用途: 验证真正长时间无活动时，系统要求写恢复包并暂停，而不是直接丢任务。
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


# LLM: Missing last_activity_at should fall back to started_at without special prompt rules.
# 函数用途: 新 run 还没写活动时间时，用 started_at 计算空闲时长，保证状态机可预测。
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
