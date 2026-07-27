from __future__ import annotations

from pathlib import Path

from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.services.hierarchy.scheduler import (
    HierarchyChildSpec,
    HierarchyScheduleRequest,
)
from agent_py_agent.agent.subagents.services.recovery.modes import (
    RecoveryMode,
    action_for_recovery_mode,
    is_rerun_mode,
    is_takeover_mode,
    recovery_mode_from_protocol_value,
)
from agent_py_agent.agent.subagents.services.recovery.strategy import (
    SubagentRecoveryStrategyRequest,
    build_subagent_recovery_strategy,
)


def _saved_task(tmp_path: Path, *, status: str = "BLOCKED"):
    manager = SubAgentManager(tmp_path)
    task = manager.create_run(
        goal="继续示例网站 checkout 任务",
        thought="需要从本地 checkpoint 接着做。",
        plan=["读 checkpoint", "继续实现"],
        role="worker",
    )
    task.status = status
    task.current_step = "继续补 checkout QA 证据"
    task.latest_summary = "条目页已完成，checkout QA 还没结束。"
    manager.save(task)
    return manager, manager.load(task.id)


def test_recovery_strategy_uses_existing_task_local_checkpoint(tmp_path: Path) -> None:
    _, task = _saved_task(tmp_path)

    result = build_subagent_recovery_strategy(SubagentRecoveryStrategyRequest(task=task))

    assert result.recommended_action == "recover_from_checkpoint"
    assert result.recovery_mode == "rerun_from_checkpoint"
    assert task.agent_run_checkpoint_json in result.recovery_refs
    assert "checkpoint/state/summary" in result.runner_instruction
    assert "不要从用户目标重新规划" in result.runner_instruction
    assert result.to_dict()["context_scope"] == "task_local"


def test_recovery_strategy_does_not_treat_error_alias_as_recoverable(tmp_path: Path) -> None:
    _, task = _saved_task(tmp_path, status="ERROR")

    result = build_subagent_recovery_strategy(SubagentRecoveryStrategyRequest(task=task))

    assert result.recovery_mode == "manual_review_missing_recovery_refs"


def test_recovery_strategy_does_not_case_coerce_raw_status(tmp_path: Path) -> None:
    _, task = _saved_task(tmp_path, status="failed")

    result = build_subagent_recovery_strategy(SubagentRecoveryStrategyRequest(task=task))

    assert result.status == "failed"
    assert result.recovery_mode == "manual_review_missing_recovery_refs"
    assert result.recommended_action == "manual_review"


def test_recovery_strategy_stops_after_repeated_failures(tmp_path: Path) -> None:
    _, task = _saved_task(tmp_path, status="FAILED")
    task.runner_attempts = 5

    result = build_subagent_recovery_strategy(
        SubagentRecoveryStrategyRequest(task=task, no_progress_attempt_limit=3)
    )

    assert result.no_progress_fuse is True
    assert result.recommended_action == "report_blocker"
    assert result.recovery_mode == "no_progress_limit_reached"
    assert "不要继续自动重试" in result.runner_instruction


def test_recovery_strategy_suggests_takeover_for_dead_worker(tmp_path: Path) -> None:
    _, task = _saved_task(tmp_path, status="TIMEOUT")
    task.failure_type = "runner_timeout"

    result = build_subagent_recovery_strategy(SubagentRecoveryStrategyRequest(task=task))

    assert result.recommended_action == "takeover"
    assert result.recovery_mode == "takeover_from_checkpoint"
    assert task.task_dir in result.takeover_refs
    assert task.agent_run_artifacts_dir in result.takeover_refs
    assert "同一个任务目录" in result.runner_instruction


def test_recovery_strategy_resumes_user_stopped_run_from_same_checkpoint(tmp_path: Path) -> None:
    manager, task = _saved_task(tmp_path, status="CANCELLED")
    task.failure_type = "cancelled"
    task.runner_attempts = 99
    task.attributes = {
        "cancel_subagents": {
            "reason": "conversation_user_stop",
            "previous_status": "RUNNING",
        }
    }
    manager.save(task)

    result = build_subagent_recovery_strategy(
        SubagentRecoveryStrategyRequest(task=manager.load(task.id))
    )

    assert result.recommended_action == "recover_from_checkpoint"
    assert result.recovery_mode == "rerun_from_checkpoint"
    assert result.no_progress_fuse is False
    assert task.agent_run_checkpoint_json in result.recovery_refs


def test_recovery_strategy_suggests_leadership_recovery_for_failed_coordinator(tmp_path: Path) -> None:
    manager = SubAgentManager(tmp_path)
    coordinator = manager.create_run(
        goal="协调示例网站实现",
        thought="拆给 leaf。",
        plan=["派工"],
        role="coordinator",
    )
    manager.hierarchy.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=coordinator.id,
            apply=True,
            child_specs=[HierarchyChildSpec(goal="写条目列表", role="worker", agent_name="小小傻妞-catalog")],
        )
    )
    coordinator = manager.load(coordinator.id)
    coordinator.status = "TIMEOUT"
    coordinator.failure_type = "runner_timeout"
    manager.save(coordinator)
    coordinator = manager.load(coordinator.id)

    result = build_subagent_recovery_strategy(SubagentRecoveryStrategyRequest(task=coordinator))

    assert result.recommended_action == "takeover"
    assert result.recovery_mode == "leadership_recovery"
    assert result.leadership_recovery is True
    assert result.child_run_ids == coordinator.child_ids
    assert "subagents-leadership-recovery-plan" in result.runner_instruction


def test_recovery_mode_helpers_accept_only_current_structured_modes() -> None:
    assert is_rerun_mode(RecoveryMode.RERUN_FROM_CHECKPOINT) is True
    assert is_takeover_mode(RecoveryMode.TAKEOVER_FROM_CHECKPOINT) is True
    assert action_for_recovery_mode(RecoveryMode.RERUN_FROM_CHECKPOINT) == "recover_from_checkpoint"
    assert (
        recovery_mode_from_protocol_value("rerun_from_checkpoint")
        is RecoveryMode.RERUN_FROM_CHECKPOINT
    )
    assert recovery_mode_from_protocol_value("rerun_from_old_alias") is RecoveryMode.MANUAL_REVIEW_MISSING_REFS
