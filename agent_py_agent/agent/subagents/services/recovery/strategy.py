
# LLM: 恢复资格只读取当前 TaskStatus、refs 和显式停止事实；未知状态不能触发重跑或接管。
# 模块用途: 为失败或停滞子代理生成结构化恢复方案，实际控制仍由管理器执行并记账。
from __future__ import annotations

from typing import Any

"""Refs-first recovery strategy for failed or stalled subagent runs.

Recovery eligibility reads the current TaskStatus protocol only; unknown raw
status text remains audit data and never drives rerun, takeover, or closeout.
"""

from dataclasses import dataclass, field
from pathlib import Path

from ....capability.config import CapabilityConfig
from ...models import (
    PROVIDER_SUPPLY_FAILURE_TYPES,
    SUBAGENT_DEAD_FAILURE_TYPES,
    SUBAGENT_DEAD_STATUSES,
    SUBAGENT_FAILURE_STATUSES,
    SUBAGENT_RECOVERY_CLOSED_STATUSES,
    SUBAGENT_RETRYABLE_FAILURE_STATUSES,
    SUBAGENT_TASK_STATUSES,
    SubAgentTask,
    known_failure_type,
    task_status_in,
)
from ...protocol import build_task_address, build_task_envelope
from ...recovery_eligibility import user_stopped_run_is_resumable
from ...role_templates import role_template_snapshot_for_task
from .modes import RecoveryMode, action_for_recovery_mode


# LLM: 生产调用注入同一有效能力配置阈值；独立纯策略调用的默认值只来自CapabilityConfig，0明确关闭。
# 类用途: 携带任务、同树信息与当前无进展阈值，不建立第二份配置权威。
@dataclass(frozen=True)
class SubagentRecoveryStrategyRequest:
    task: SubAgentTask
    all_tasks: list[SubAgentTask] = field(default_factory=list)
    no_progress_attempt_limit: int = field(default_factory=lambda: CapabilityConfig().subagent_no_progress_attempt_limit)


# LLM: 只调用可信组合根绑定的原能力配置读口，不从owner/workspace猜路径；坏配置阻止启动，不静默切回默认4。
# 函数用途: 为真实启动、恢复编排和树报告读取同一有效阈值，保留0关闭语义。
def manager_no_progress_attempt_limit(manager: Any) -> int:
    provider = getattr(manager, "recovery_capability_config", None)
    config = provider() if callable(provider) else CapabilityConfig()
    if not isinstance(config, CapabilityConfig):
        raise ValueError("恢复能力配置读取失败，不能按默认阈值自动启动。")
    return config.subagent_no_progress_attempt_limit


# LLM: 恢复回执区分已证实无进展与观察未知次数；只描述事实，不凭未知观察清零或触发熔断。
# 类用途: 返回同run恢复建议及宿主观察状态。
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
    no_progress_unknown_count: int = 0
    blocked_by: list[str] = field(default_factory=list)
    runner_instruction: str = ""

    # LLM: 将恢复事实投影成回执，未知观察次数可见但不冒充已证实的无进展。
    # 函数用途: 输出恢复策略与观察完整性。
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
            "no_progress_unknown_count": self.no_progress_unknown_count,
            "blocked_by": list(self.blocked_by),
            "runner_instruction": self.runner_instruction,
        }


# LLM: 阈值由调用方传入且0关闭；坏账首次读取就人工复核并保留blocked_by，未知进展观察不等同坏恢复历史，不写文件。
# 函数用途: 为规范任务生成与派发门一致的安全恢复建议及明确阻断原因。
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
        no_progress_unknown_count=recovery_count(task, "no_progress_unknown_count") or 0,
        blocked_by=["recovery_history_unknown"] if recovery_mode == RecoveryMode.MANUAL_REVIEW_UNKNOWN_HISTORY else [],
        runner_instruction=_runner_instruction(task, recovery_refs, recovery_mode),
    )


# LLM: 坏账指令只要求人工复核，不能因refs存在而建议接续；已证失败累计与未知观察各守原语义。
# 函数用途: 为准确恢复模式提供最小现场说明，不把人工阻断说成自动执行。
def _runner_instruction(
    task: SubAgentTask,
    recovery_refs: list[str],
    recovery_mode: RecoveryMode,
) -> str:
    if recovery_mode == RecoveryMode.MANUAL_REVIEW_UNKNOWN_HISTORY:
        return "恢复计数历史未知或损坏：停止自动续派或接管，请先人工复核原 canonical 记录与 refs；不要按零计数重开额度。"
    if recovery_mode == RecoveryMode.NO_PROGRESS_LIMIT_REACHED:
        return "自上次已证实字节推进以来的无进展失败累计达到阈值：不要继续自动重试或扩容；请汇总 refs 等待父级/用户决策。"
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


# LLM: 沿唯一纯计数校验首先阻断unknown历史；有效refs、失联角色或关闭阈值均不能把坏账变成自动重跑/接管。
# 函数用途: 根据计数完整性和原恢复事实选择准确模式。
def _recovery_mode(
    task: SubAgentTask,
    recovery_refs: list[str],
    no_progress_fuse: bool,
) -> RecoveryMode:
    if recovery_count(task, "recovery_history_unknown_count"):
        return RecoveryMode.MANUAL_REVIEW_UNKNOWN_HISTORY
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


RECOVERY_COUNTER_NAMES = ("failure_retry_count", "provider_retry_count", "no_progress_count", "recovery_history_unknown_count")
_ABSENT_COUNTER = object()


# LLM: 唯一只读计数校验；仅全部旧字段缺失且状态/原因/总次数有效时按total-1保守迁移；部分缺失、布尔、矛盾即unknown。
# 函数用途: 在首次读取与所有候选门共用坏账拒绝和干净旧记录兼容。
def recovery_counters(task: SubAgentTask) -> dict[str, int]:
    values = vars(task)
    extra = values.get("_extra_fields", {})
    extra = extra if isinstance(extra, dict) else {}
    names = (*RECOVERY_COUNTER_NAMES, "no_progress_unknown_count")
    raw = {name: values.get(name, extra.get(name, _ABSENT_COUNTER)) for name in names}
    present = any(value is not _ABSENT_COUNTER for value in raw.values())
    attempts = values.get("runner_attempts", None if present else 0)
    valid_attempts = type(attempts) is int and attempts >= 0
    counters = {name: value if type(value) is int and value >= 0 else 0 for name, value in raw.items()}
    invalid = not valid_attempts
    if not present:
        failure = values.get("failure_type", "")
        invalid |= not task_status_in(values.get("status", ""), SUBAGENT_TASK_STATUSES)
        invalid |= bool(failure) and not bool(known_failure_type(failure))
        if not invalid:
            key = "provider_retry_count" if failure in PROVIDER_SUPPLY_FAILURE_TYPES else "failure_retry_count"
            counters[key] = max(0, attempts - 1)
    else:
        invalid |= any(type(raw[name]) is not int or raw[name] < 0 for name in RECOVERY_COUNTER_NAMES)
        optional = raw["no_progress_unknown_count"]
        invalid |= optional is not _ABSENT_COUNTER and (type(optional) is not int or optional < 0)
        if valid_attempts:
            invalid |= counters["failure_retry_count"] + counters["provider_retry_count"] > attempts
    if invalid:
        counters["recovery_history_unknown_count"] = max(1, counters["recovery_history_unknown_count"])
    return counters


# LLM: 读取前核全组一致性，不能用False/or零或某个合法字段掩盖另一个坏字段；不写入任务。
# 函数用途: 返回统一验证后的恢复计数。
def recovery_count(task: SubAgentTask, name: str) -> int | None:
    return recovery_counters(task).get(name)


# LLM: 正常yield/child wait/compact不累计；坏恢复历史只走人工复核，不冒充已证熔断；共用失败状态且0关闭。
# 函数用途: 依据宿主无进展事实判断是否熔断。
def _no_progress_fuse(task: SubAgentTask, attempt_limit: int) -> bool:
    if attempt_limit <= 0 or recovery_count(task, "recovery_history_unknown_count"):
        return False
    return (
        (recovery_count(task, "no_progress_count") or 0) >= attempt_limit
        and task_status_in(task_status(task), SUBAGENT_RETRYABLE_FAILURE_STATUSES)
    )


__all__ = [
    "SubagentRecoveryStrategy",
    "SubagentRecoveryStrategyRequest",
    "build_subagent_recovery_strategy",
]
