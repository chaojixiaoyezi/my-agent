
"""Public dataclass exports for subagent state.

Concrete model families live in narrower modules; this file is the current
public state model API.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .execution.executor import TestExecutor
from .execution.records import TestExecutionRecord
from .model_capabilities import (
    CapabilityGap,
    CapabilityGrant,
    CapabilityRequest,
    VerificationEvidence,
)
from .model_runtime import (
    SubAgentExecutionContext,
    SubAgentParsedOutput,
    SubAgentRunnerResult,
)
from .model_task import (
    ChannelProbeCheck,
    ChannelProbeReport,
    ChannelProbeResult,
    ContextManifest,
    EvidencePacket,
    FailureHandoff,
    Finding,
    InheritanceManifest,
    QualityContract,
    RuntimeIdentity,
    SecuritySignal,
    StatusReport,
    SubAgentTask,
    TakeoverRecord,
    WorkOrderValidation,
)


class TaskStatus(str, Enum):
    """Current subagent lifecycle protocol statuses."""

    PLANNING = "PLANNING"
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    BLOCKED = "BLOCKED"
    PAUSED = "PAUSED"
    ABANDONED = "ABANDONED"
    CANCELLED = "CANCELLED"
    # 状态用途: 旧 run 已由 replacement/takeover run 接管，保留审计但不再进入普通 dispatch 候选。
    TAKEN_OVER = "TAKEN_OVER"
    DONE = "DONE"
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"
    CHANNEL_ERROR = "CHANNEL_ERROR"


class VerificationStatus(str, Enum):
    """Current verification lifecycle statuses for subagent results."""

    UNVERIFIED = "UNVERIFIED"
    VERIFIED = "VERIFIED"
    FAILED = "FAILED"


class FailureType(str, Enum):
    """Current structured runner failure types used by subagent orchestration."""

    API_ERROR = "api_error"
    BACKGROUND_DISPATCH_STARTUP = "background_dispatch_startup"
    CANCELLED = "cancelled"
    CAPABILITY_REQUEST = "capability_request"
    CHANNEL = "channel"
    CHANNEL_BROKEN = "channel_broken"
    CHANNEL_ERROR = "channel_error"
    # The runner made durable progress but explicitly reports that its declared
    # deliverables are not finished yet.  This is a continuation state, not an
    # external blocker and not permission to create a replacement run.
    INCOMPLETE_DELIVERABLES = "incomplete_deliverables"
    MISSING_CAPABILITY = "missing_capability"
    MISSING_EVIDENCE = "missing_evidence"
    MODEL_ERROR = "model_error"
    NO_PROGRESS_FUSE = "no_progress_fuse"
    PERMISSION_BLOCKED = "permission_blocked"
    # Account/plan quota cannot be repaired by replaying the same provider
    # request.  Keep it distinct from transient overload so durable workers
    # stop consuming tokens until an operator explicitly restores supply.
    PROVIDER_QUOTA_EXHAUSTED = "provider_quota_exhausted"
    PROVIDER_TIMEOUT = "provider_timeout"
    RUNNER_CHANNEL_FAILED = "runner_channel_failed"
    RUNNER_ERROR = "runner_error"
    RUNNER_TIMEOUT = "runner_timeout"
    RUNNER_WORKER_ERROR = "runner_worker_error"
    STRUCTURED_OUTPUT_PARSE_ERROR = "structured_output_parse_error"
    STATUS_BLOCKED = "status_blocked"
    STATUS_FAILED = "status_failed"
    TAKEOVER_CHAIN_EXHAUSTED = "takeover_chain_exhausted"
    TOOL_ERROR = "tool_error"
    TOOL_FAILURE = "tool_failure"
    TOOL_OUTPUT_CONTEXT_OVERFLOW = "tool_output_context_overflow"
    TOOL_RESULT_MISSING = "tool_result_missing"
    TRANSIENT_ERROR = "transient_error"
    VERIFICATION_FAILED = "verification_failed"
    WRITE_PERMISSION_BLOCKED = "write_permission_blocked"


SUBAGENT_TASK_STATUSES = frozenset(status.value for status in TaskStatus)
SUBAGENT_VERIFICATION_STATUSES = frozenset(status.value for status in VerificationStatus)
SUBAGENT_FAILURE_TYPES = frozenset(item.value for item in FailureType)
RETRYABLE_RUNNER_FAILURE_TYPES = frozenset({
    FailureType.RUNNER_ERROR.value,
    FailureType.STRUCTURED_OUTPUT_PARSE_ERROR.value,
    FailureType.TOOL_RESULT_MISSING.value,
    FailureType.MODEL_ERROR.value,
    FailureType.API_ERROR.value,
    FailureType.TRANSIENT_ERROR.value,
    FailureType.PROVIDER_TIMEOUT.value,
    FailureType.RUNNER_TIMEOUT.value,
    # Backward compatibility for persisted BLOCKED results written before
    # incomplete work became a PENDING continuation state.
    FailureType.INCOMPLETE_DELIVERABLES.value,
})
PROVIDER_SUPPLY_FAILURE_TYPES = frozenset({
    FailureType.TRANSIENT_ERROR.value,
    FailureType.PROVIDER_TIMEOUT.value,
})
CAPABILITY_GRANTED_BLOCKER_FAILURE_TYPES = frozenset({
    FailureType.CAPABILITY_REQUEST.value,
    FailureType.PERMISSION_BLOCKED.value,
    FailureType.MISSING_CAPABILITY.value,
    FailureType.WRITE_PERMISSION_BLOCKED.value,
})
SUBAGENT_DEAD_FAILURE_TYPES = frozenset({
    FailureType.RUNNER_TIMEOUT.value,
    FailureType.CHANNEL_ERROR.value,
    FailureType.RUNNER_CHANNEL_FAILED.value,
    FailureType.CHANNEL_BROKEN.value,
})
DISPATCH_INELIGIBLE_STATUSES = frozenset({
    TaskStatus.PAUSED.value,
    TaskStatus.ABANDONED.value,
    TaskStatus.CANCELLED.value,
    TaskStatus.TAKEN_OVER.value,
    TaskStatus.DONE.value,
    TaskStatus.FAILED.value,
    TaskStatus.TIMEOUT.value,
    TaskStatus.CHANNEL_ERROR.value,
})
SUBAGENT_FAILURE_STATUSES = frozenset({
    TaskStatus.BLOCKED.value,
    TaskStatus.FAILED.value,
    TaskStatus.CHANNEL_ERROR.value,
    TaskStatus.TIMEOUT.value,
})
SUBAGENT_WAKE_STATUSES = frozenset({
    TaskStatus.DONE.value,
    *SUBAGENT_FAILURE_STATUSES,
})
SUBAGENT_BLOCKED_STATUSES = frozenset({
    TaskStatus.BLOCKED.value,
})
SUBAGENT_FAILED_RESULT_STATUSES = frozenset({
    TaskStatus.FAILED.value,
    TaskStatus.CHANNEL_ERROR.value,
    TaskStatus.TIMEOUT.value,
})
SUBAGENT_DEAD_STATUSES = frozenset({
    TaskStatus.CHANNEL_ERROR.value,
    TaskStatus.TIMEOUT.value,
})
SUBAGENT_ENDED_STATUSES = frozenset({
    TaskStatus.DONE.value,
    TaskStatus.FAILED.value,
    TaskStatus.BLOCKED.value,
    TaskStatus.CHANNEL_ERROR.value,
    TaskStatus.TIMEOUT.value,
    TaskStatus.CANCELLED.value,
    TaskStatus.ABANDONED.value,
    TaskStatus.TAKEN_OVER.value,
})
SUBAGENT_HANDLED_TERMINAL_STATUSES = frozenset({
    # "主代理已处理的终态":ABANDONED(崩溃调和)、CANCELLED(主代理主动取消收尾)、TAKEN_OVER(被接管)。
    # CANCELLED 在此 → 自动进 RECOVERY_CLOSED(=DONE|HANDLED):被取消的子代理不再被 recovery 捡、
    # compaction 不续传、不能当协作目标、takeover 跳过——与旧 ABANDONED 行为等价,只是状态名不再误导。
    TaskStatus.ABANDONED.value,
    TaskStatus.CANCELLED.value,
    TaskStatus.TAKEN_OVER.value,
})
SUBAGENT_RESOLVED_TERMINAL_STATUSES = frozenset({
    TaskStatus.CANCELLED.value,
    *SUBAGENT_HANDLED_TERMINAL_STATUSES,
})
SUBAGENT_RECOVERY_CLOSED_STATUSES = frozenset({
    TaskStatus.DONE.value,
    *SUBAGENT_HANDLED_TERMINAL_STATUSES,
})
SUBAGENT_REUSABLE_STATUSES = frozenset({
    TaskStatus.PLANNING.value,
    TaskStatus.PENDING.value,
    TaskStatus.RUNNING.value,
    TaskStatus.DONE.value,
    TaskStatus.BLOCKED.value,
    TaskStatus.PAUSED.value,
})
SUBAGENT_DISPATCH_READY_STATUSES = frozenset({
    TaskStatus.PLANNING.value,
    TaskStatus.PENDING.value,
})


def normalize_task_status(value: object) -> str:
    """Return a current protocol status or fail closed for unknown raw text."""
    text = str(value or "").strip()
    if text in SUBAGENT_TASK_STATUSES:
        return text
    raise ValueError("subagent_status_invalid")


def normalize_verification_status(value: object) -> str:
    """Return a current verification status or fail closed for unknown raw text."""
    text = str(value or "").strip()
    if text in SUBAGENT_VERIFICATION_STATUSES:
        return text
    raise ValueError("subagent_verification_status_invalid")


def normalize_failure_type(value: object) -> str:
    """Return the lowercase raw failure type; callers decide whether it is known."""
    return str(value or "").strip().lower()


def known_failure_type(value: object) -> str:
    """Return a current structured failure type, or empty for unknown raw text."""
    text = normalize_failure_type(value)
    return text if text in SUBAGENT_FAILURE_TYPES else ""


_TASK_STATUS_FAILURE_TYPES = {
    TaskStatus.BLOCKED.value: FailureType.STATUS_BLOCKED.value,
    TaskStatus.FAILED.value: FailureType.STATUS_FAILED.value,
    TaskStatus.TIMEOUT.value: FailureType.RUNNER_TIMEOUT.value,
    TaskStatus.CHANNEL_ERROR.value: FailureType.CHANNEL_ERROR.value,
    TaskStatus.CANCELLED.value: FailureType.CANCELLED.value,
}


_TASK_STATUS_REASON_CODES = {
    TaskStatus.PLANNING.value: "planning",
    TaskStatus.PENDING.value: "pending",
    TaskStatus.RUNNING.value: "running",
    TaskStatus.BLOCKED.value: "blocked",
    TaskStatus.PAUSED.value: "paused",
    TaskStatus.ABANDONED.value: "abandoned",
    TaskStatus.CANCELLED.value: "cancelled",
    TaskStatus.TAKEN_OVER.value: "taken_over",
    TaskStatus.DONE.value: "done",
    TaskStatus.FAILED.value: "failed",
    TaskStatus.TIMEOUT.value: "timeout",
    TaskStatus.CHANNEL_ERROR.value: "channel_error",
}


def failure_type_from_task_status(value: object) -> str:
    """Map a current task status to an explicit failure type without guessing."""
    try:
        status = normalize_task_status(value)
    except ValueError:
        return ""
    return _TASK_STATUS_FAILURE_TYPES.get(status, "")


def task_status_reason_code(value: object) -> str:
    """Return a stable reason code for a current task status."""
    try:
        status = normalize_task_status(value)
    except ValueError:
        return ""
    return _TASK_STATUS_REASON_CODES.get(status, "")


def task_status_in(value: object, statuses: frozenset[str] | set[str]) -> bool:
    try:
        return normalize_task_status(value) in statuses
    except ValueError:
        return False


def task_has_status(task: object, status: TaskStatus) -> bool:
    return task_status_in(getattr(task, "status", ""), {status.value})


# LLM: 子代理完成权只读 host-owned TaskStatus.DONE，verification_status 仅作历史展示。
# 函数用途: 判断子代理是否已正常结束且不再需要续派。
def task_is_completed(task: object) -> bool:
    try:
        status = normalize_task_status(getattr(task, "status", ""))
    except ValueError:
        return False
    return status == TaskStatus.DONE.value


def task_has_failure_status(task: object) -> bool:
    return task_status_in(getattr(task, "status", ""), SUBAGENT_FAILURE_STATUSES)


def task_has_failed_result_status(task: object) -> bool:
    return task_status_in(getattr(task, "status", ""), SUBAGENT_FAILED_RESULT_STATUSES)


def task_has_ended_status(task: object) -> bool:
    return task_status_in(getattr(task, "status", ""), SUBAGENT_ENDED_STATUSES)


def task_is_dispatch_ineligible(task: object) -> bool:
    return task_status_in(getattr(task, "status", ""), DISPATCH_INELIGIBLE_STATUSES)


def task_is_handled_after_parent_timeout(task: object) -> bool:
    if task_is_completed(task):
        return True
    return task_status_in(getattr(task, "status", ""), SUBAGENT_HANDLED_TERMINAL_STATUSES)


def task_needs_continuation(task: object, *, closed_statuses: frozenset[str] | set[str]) -> bool:
    if task_status_in(getattr(task, "status", ""), closed_statuses):
        return False
    return not task_is_completed(task)


@dataclass
class SubAgentCard:
    """Role/capability card describing what a subagent is allowed to do."""

    name: str
    description: str
    role: str = "general"
    default_model: str = "inherit"
    allowed_skills: list[str] = field(default_factory=list)
    allowed_tools: list[str] = field(default_factory=list)
    can_write: bool = False
    can_spawn_children: bool = False
    can_request_capability: bool = True
    max_depth: int = 0
    result_contract: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SubAgentCapabilityRouteOptions:
    """Bundle for capability-request routing options."""

    apply: bool = False
    run_ids: list[str] | None = None
    limit: int = 0


@dataclass(frozen=True)
class SubAgentChannelProbeOptions:
    """Bundle for channel probe selection options."""

    run_ids: list[str] | None = None
    limit: int = 0


@dataclass(frozen=True)
class SubAgentDueCheckOptions:
    """Bundle for due-check report options."""

    config: Any | None = None
    write_report: bool = False
    root_id: str = ""
    include_run_ids: list[str] = field(default_factory=list)
    exclude_run_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SubAgentPlanActionsOptions:
    """Bundle for dry-run action-plan options."""

    config: Any | None = None
    write_report: bool = False
    root_id: str = ""
    include_run_ids: list[str] = field(default_factory=list)
    exclude_run_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SubAgentLeadershipRecoveryPlanOptions:
    """Bundle for dry-run batch coordinator leadership recovery planning."""

    config: Any | None = None
    write_report: bool = False
    root_id: str = ""
    leader_ids: list[str] = field(default_factory=list)
    max_children_per_leader: int = 3


@dataclass(frozen=True)
class SubAgentLeadershipRecoveryApplyOptions:
    """Bundle for controlled subset coordinator leadership recovery apply."""

    root_id: str = ""
    coordinator_id: str = ""
    leader_id: str = ""
    child_ids: list[str] = field(default_factory=list)
    apply: bool = False
    max_children_per_leader: int = 0


@dataclass(frozen=True)
class SubAgentBoardOptions:
    """Bundle for board rendering and recent-list selection."""

    recent_limit: int = 20
    include_child_status_counts: bool = True
    status: str = ""
    owner: str = ""
    root_id: str = ""


__all__ = [
    "CapabilityGap",
    "CapabilityGrant",
    "CapabilityRequest",
    "ChannelProbeCheck",
    "ChannelProbeReport",
    "ChannelProbeResult",
    "ContextManifest",
    "DISPATCH_INELIGIBLE_STATUSES",
    "EvidencePacket",
    "FailureHandoff",
    "Finding",
    "InheritanceManifest",
    "QualityContract",
    "RuntimeIdentity",
    "SecuritySignal",
    "SubAgentCard",
    "SubAgentBoardOptions",
    "SubAgentCapabilityRouteOptions",
    "SubAgentChannelProbeOptions",
    "SubAgentDueCheckOptions",
    "SubAgentLeadershipRecoveryApplyOptions",
    "SubAgentLeadershipRecoveryPlanOptions",
    "SubAgentPlanActionsOptions",
    "SubAgentExecutionContext",
    "SubAgentParsedOutput",
    "SubAgentRunnerResult",
    "SubAgentTask",
    "StatusReport",
    "TakeoverRecord",
    "TestExecutor",
    "TestExecutionRecord",
    "TaskStatus",
    "SUBAGENT_BLOCKED_STATUSES",
    "SUBAGENT_DEAD_STATUSES",
    "SUBAGENT_DISPATCH_READY_STATUSES",
    "SUBAGENT_ENDED_STATUSES",
    "SUBAGENT_FAILED_RESULT_STATUSES",
    "SUBAGENT_FAILURE_STATUSES",
    "SUBAGENT_HANDLED_TERMINAL_STATUSES",
    "SUBAGENT_RECOVERY_CLOSED_STATUSES",
    "SUBAGENT_RESOLVED_TERMINAL_STATUSES",
    "SUBAGENT_REUSABLE_STATUSES",
    "SUBAGENT_TASK_STATUSES",
    "SUBAGENT_VERIFICATION_STATUSES",
    "VerificationEvidence",
    "VerificationStatus",
    "WorkOrderValidation",
    "normalize_task_status",
    "normalize_verification_status",
    "task_has_ended_status",
    "task_has_failure_status",
    "task_has_status",
    "task_is_dispatch_ineligible",
    "task_is_completed",
    "task_is_handled_after_parent_timeout",
    "task_needs_continuation",
    "task_status_in",
]
