
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
from .model_records import (
    ChannelProbeCheck,
    ChannelProbeReport,
    ChannelProbeResult,
    TakeoverRecord,
    WorkOrderValidation,
)
from .model_runtime import (
    SubAgentExecutionContext,
    SubAgentParsedOutput,
    SubAgentRunnerResult,
)
from .model_task import (
    EvidencePacket,
    FailureHandoff,
    Finding,
    InheritanceManifest,
    LearningCandidate,
    RuntimeIdentity,
    SecuritySignal,
    StatusReport,
    SubAgentTask,
)
from .quality_models import ContextManifest, QualityContract


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


SUBAGENT_TASK_STATUSES = frozenset(status.value for status in TaskStatus)
SUBAGENT_VERIFICATION_STATUSES = frozenset(status.value for status in VerificationStatus)
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
    TaskStatus.ABANDONED.value,
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
    text = str(value or "").strip().upper()
    if text in SUBAGENT_TASK_STATUSES:
        return text
    raise ValueError("subagent_status_invalid")


def normalize_verification_status(value: object) -> str:
    """Return a current verification status or fail closed for unknown raw text."""
    text = str(value or "").strip().upper()
    if text in SUBAGENT_VERIFICATION_STATUSES:
        return text
    raise ValueError("subagent_verification_status_invalid")


def task_status_in(value: object, statuses: frozenset[str] | set[str]) -> bool:
    try:
        return normalize_task_status(value) in statuses
    except ValueError:
        return False


def task_has_status(task: object, status: TaskStatus) -> bool:
    return task_status_in(getattr(task, "status", ""), {status.value})


def task_is_done_verified(task: object) -> bool:
    try:
        status = normalize_task_status(getattr(task, "status", ""))
        verification = normalize_verification_status(getattr(task, "verification_status", ""))
    except ValueError:
        return False
    return status == TaskStatus.DONE.value and verification == VerificationStatus.VERIFIED.value


def task_has_failure_status(task: object) -> bool:
    return task_status_in(getattr(task, "status", ""), SUBAGENT_FAILURE_STATUSES)


def task_has_failed_result_status(task: object) -> bool:
    return task_status_in(getattr(task, "status", ""), SUBAGENT_FAILED_RESULT_STATUSES)


def task_has_ended_status(task: object) -> bool:
    return task_status_in(getattr(task, "status", ""), SUBAGENT_ENDED_STATUSES)


def task_is_dispatch_ineligible(task: object) -> bool:
    return task_status_in(getattr(task, "status", ""), DISPATCH_INELIGIBLE_STATUSES)


def task_is_handled_after_parent_timeout(task: object) -> bool:
    if task_is_done_verified(task):
        return True
    return task_status_in(getattr(task, "status", ""), SUBAGENT_HANDLED_TERMINAL_STATUSES)


def task_needs_continuation(task: object, *, closed_statuses: frozenset[str] | set[str]) -> bool:
    if task_status_in(getattr(task, "status", ""), closed_statuses):
        return False
    return not task_is_done_verified(task)


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
    "LearningCandidate",
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
    "task_is_done_verified",
    "task_is_handled_after_parent_timeout",
    "task_needs_continuation",
    "task_status_in",
]
