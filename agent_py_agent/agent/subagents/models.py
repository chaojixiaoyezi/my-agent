
"""Public dataclass exports for subagent state.

Concrete model families live in narrower modules; this file is the current
public state model API.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .execution import TestExecutionRecord, TestExecutor
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
    """Subagent lifecycle status values."""

    PLANNING = "PLANNING"
    RUNNING = "RUNNING"
    BLOCKED = "BLOCKED"
    PAUSED = "PAUSED"
    ABANDONED = "ABANDONED"
    # 状态用途: 旧 run 已由 replacement/takeover run 接管，保留审计但不再进入普通 dispatch 候选。
    TAKEN_OVER = "TAKEN_OVER"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


DISPATCH_INELIGIBLE_STATUSES = frozenset({
    TaskStatus.PAUSED.value,
    TaskStatus.ABANDONED.value,
    TaskStatus.TAKEN_OVER.value,
    TaskStatus.COMPLETED.value,
    TaskStatus.FAILED.value,
})


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
    "VerificationEvidence",
    "WorkOrderValidation",
]
