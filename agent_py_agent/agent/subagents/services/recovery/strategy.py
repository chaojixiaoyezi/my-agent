
from __future__ import annotations

"""Refs-first recovery strategy for failed or stalled subagent runs.

Recovery eligibility reads the current TaskStatus protocol only; unknown raw
status text remains audit data and never drives rerun, takeover, or closeout.
"""

from dataclasses import dataclass, field
from pathlib import Path

from ...models import (
    SUBAGENT_DEAD_FAILURE_TYPES,
    SUBAGENT_DEAD_STATUSES,
    SUBAGENT_FAILURE_STATUSES,
    SUBAGENT_RECOVERY_CLOSED_STATUSES,
    SubAgentTask,
    known_failure_type,
    task_status_in,
)
from ...protocol import build_task_address, build_task_envelope
from ...recovery_eligibility import user_stopped_run_is_resumable
from ...role_templates import role_template_snapshot_for_task
from .modes import RecoveryMode, action_for_recovery_mode


@dataclass(frozen=True)
class SubagentRecoveryStrategyRequest:
    task: SubAgentTask
    all_tasks: list[SubAgentTask] = field(default_factory=list)
    no_progress_attempt_limit: int = 4


@dataclass(frozen=True)
class SubagentRecoveryStrategy:
    run_id: str
    status: str
    role: str
    recommended_action: str
    recovery_mode: RecoveryMode
    context_scope: str = "task_local"
    recovery_refs: list[str] = field(default_factory=list)
    takeover_refs: list[str] = field(default_factory=list)
    child_run_ids: list[str] = field(default_factory=list)
    address: dict[str, object] = field(default_factory=dict)
    task_envelope: dict[str, object] = field(default_factory=dict)
    leadership_recovery: bool = False
    no_progress_fuse: bool = False
    blocked_by: list[str] = field(default_factory=list)
    runner_instruction: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "role": self.role,
            "recommended_action": self.recommended_action,
            "recovery_mode": self.recovery_mode.value,
            "context_scope": self.context_scope,
            "recovery_refs": list(self.recovery_refs),
            "takeover_refs": list(self.takeover_refs),
            "child_run_ids": list(self.child_run_ids),
            "address": dict(self.address),
            "task_envelope": dict(self.task_envelope),
            "leadership_recovery": self.leadership_recovery,
            "no_progress_fuse": self.no_progress_fuse,
            "blocked_by": list(self.blocked_by),
            "runner_instruction": self.runner_instruction,
        }


def build_subagent_recovery_strategy(request: SubagentRecoveryStrategyRequest) -> SubagentRecoveryStrategy:
    task = request.task
    recovery_refs = _recovery_refs(task)
    no_progress_fuse = _no_progress_fuse(task, request.no_progress_attempt_limit)
    recovery_mode = _recovery_mode(task, recovery_refs, no_progress_fuse)
    action = action_for_recovery_mode(recovery_mode)
    return SubagentRecoveryStrategy(
        run_id=task_text(task, "id"),
        status=task_status(task),
        role=task_role(task),
        recommended_action=action,
        recovery_mode=recovery_mode,
        recovery_refs=recovery_refs,
        takeover_refs=_takeover_refs(task),
        child_run_ids=task_list(task, "child_ids"),
        address=build_task_address(task, all_tasks=request.all_tasks).to_dict(),
        task_envelope=build_task_envelope(task, all_tasks=request.all_tasks).to_dict(),
        leadership_recovery=recovery_mode == RecoveryMode.LEADERSHIP_RECOVERY,
        no_progress_fuse=no_progress_fuse,
        runner_instruction=_runner_instruction(task, recovery_refs, recovery_mode),
    )


def _runner_instruction(
    task: SubAgentTask,
    recovery_refs: list[str],
    recovery_mode: RecoveryMode,
) -> str:
    if recovery_mode == RecoveryMode.NO_PROGRESS_LIMIT_REACHED:
        return "连续恢复没有进展：不要继续自动重试，也不要继续扩容；请汇总 refs 后等待父级/用户决策。"
    if recovery_mode == RecoveryMode.LEADERSHIP_RECOVERY:
        return (
            "coordinator/lead 已失联或失败：请调用 subagents-leadership-recovery-plan 选择新 leader，"
            "再分批接管其 child_run_ids，不要重复重启失联 coordinator。"
        )
    if recovery_mode.is_takeover():
        return _takeover_instruction(task, recovery_refs)
    if recovery_refs:
        return _recovery_refs_instruction(task, recovery_refs)
    return "缺少可用恢复 refs：请先保存当前 run 的 checkpoint/state，再继续。"


def _recovery_refs_instruction(task: SubAgentTask, recovery_refs: list[str]) -> str:
    refs = ", ".join(recovery_refs[:4])
    return (
        f"恢复 run {task_text(task, 'id')}：从已有 checkpoint/state/summary refs 接续：{refs}。"
        "只读取最少必要的 task-local refs，不要从用户目标重新规划，也不要读取主代理长期记忆。"
    )


def _takeover_instruction(task: SubAgentTask, recovery_refs: list[str]) -> str:
    source = ", ".join(recovery_refs[:3])
    return (
        f"原 run {task_text(task, 'id')} 看起来已挂死：创建 takeover run 接管同一个任务目录 {task_text(task, 'task_dir')} "
        f"和同一批 artifacts refs。checkpoint/state 恢复入口：{source}。不要重写健康分支。"
    )


def task_text(task: Any, name: str) -> str:
    value = getattr(task, name, "")
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, int | float):
        return str(value)
    return ""


def task_status(task: Any) -> str:
    return task_text(task, "status")


def task_role(task: Any) -> str:
    return task_text(task, "role")


def task_int(task: Any, name: str) -> int:
    try:
        return max(0, int(getattr(task, name, 0) or 0))
    except (TypeError, ValueError):
        return 0


def task_list(task: Any, name: str) -> list[str]:
    value = getattr(task, name, [])
    if not isinstance(value, list | tuple | set):
        return []
    return [str(item) for item in value if str(item or "").strip()]


def _recovery_mode(
    task: SubAgentTask,
    recovery_refs: list[str],
    no_progress_fuse: bool,
) -> RecoveryMode:
    if no_progress_fuse:
        return RecoveryMode.NO_PROGRESS_LIMIT_REACHED
    if _needs_leadership_recovery(task):
        return RecoveryMode.LEADERSHIP_RECOVERY
    if _needs_takeover(task):
        return RecoveryMode.TAKEOVER_FROM_CHECKPOINT
    if recovery_refs and _is_recoverable(task):
        return RecoveryMode.RERUN_FROM_CHECKPOINT
    if _is_closed(task):
        return RecoveryMode.CLOSED
    return RecoveryMode.MANUAL_REVIEW_MISSING_REFS


def _recovery_refs(task: SubAgentTask) -> list[str]:
    values = [
        task_text(task, "agent_run_checkpoint_json"),
        task_text(task, "agent_run_summary_md"),
        task_text(task, "agent_run_task_md"),
        task_text(task, "failure_handoff_json"),
        task_text(task, "takeover_readiness_json"),
        task_text(task, "output_json"),
        task_text(task, "runner_result_json"),
    ]
    return _existing_refs(values)


def _takeover_refs(task: SubAgentTask) -> list[str]:
    return _existing_refs(
        [
            task_text(task, "task_dir"),
            task_text(task, "agent_run_workspace_dir"),
            task_text(task, "agent_run_artifacts_dir"),
            task_text(task, "task_workspace_artifacts_dir"),
            task_text(task, "task_workspace_shared_dir"),
        ]
    )


def _existing_refs(values: list[str]) -> list[str]:
    refs: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and Path(text).exists() and text not in refs:
            refs.append(text)
    return refs


def _needs_leadership_recovery(task: SubAgentTask) -> bool:
    return (
        bool(task_list(task, "child_ids"))
        and bool(role_template_snapshot_for_task(task).get("can_spawn_children"))
        and _is_dead(task)
    )


def _needs_takeover(task: SubAgentTask) -> bool:
    return _is_dead(task) and not _needs_leadership_recovery(task)


def _is_dead(task: SubAgentTask) -> bool:
    status = task_status(task)
    failure_type = known_failure_type(task_text(task, "failure_type"))
    return task_status_in(status, SUBAGENT_DEAD_STATUSES) or failure_type in SUBAGENT_DEAD_FAILURE_TYPES


def _is_recoverable(task: SubAgentTask) -> bool:
    return user_stopped_run_is_resumable(task) or task_status_in(
        task_status(task),
        SUBAGENT_FAILURE_STATUSES,
    )


def _is_closed(task: SubAgentTask) -> bool:
    return task_status_in(task_status(task), SUBAGENT_RECOVERY_CLOSED_STATUSES)


def _no_progress_fuse(task: SubAgentTask, attempt_limit: int) -> bool:
    if attempt_limit <= 0:
        return False
    return (
        task_int(task, "runner_attempts") >= attempt_limit
        and task_status_in(task_status(task), SUBAGENT_FAILURE_STATUSES)
    )


__all__ = [
    "SubagentRecoveryStrategy",
    "SubagentRecoveryStrategyRequest",
    "build_subagent_recovery_strategy",
]
