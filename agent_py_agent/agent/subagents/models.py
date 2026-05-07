from __future__ import annotations

"""LLM contract: public dataclass exports for subagent state.

This module is intentionally kept as the compatibility surface. Concrete model
families live in narrower modules so new state can grow without turning this
file back into a catch-all.
LLM: keep external imports pointed here while moving concrete dataclasses out.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

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

# LLM: expose task-tree control-plane dataclasses through the stable facade.
from .model_task import EvidencePacket, Finding, LearningCandidate, StatusReport, SubAgentTask
from .quality_models import ContextManifest, QualityContract


class TaskStatus(str, Enum):
    """Subagent lifecycle status values."""

    PLANNING = "PLANNING"
    RUNNING = "RUNNING"
    BLOCKED = "BLOCKED"
    PAUSED = "PAUSED"
    ABANDONED = "ABANDONED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


DISPATCH_INELIGIBLE_STATUSES = frozenset({
    TaskStatus.PAUSED.value,
    TaskStatus.ABANDONED.value,
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


# LLM: manager-facing option bundles live in the public facade so CLI/core callers share one shape.
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
    "Finding",
    "LearningCandidate",
    "QualityContract",
    "SubAgentCard",
    "SubAgentCapabilityRouteOptions",
    "SubAgentChannelProbeOptions",
    "SubAgentDueCheckOptions",
    "SubAgentExecutionContext",
    "SubAgentParsedOutput",
    "SubAgentRunnerResult",
    "SubAgentTask",
    "StatusReport",
    "TakeoverRecord",
    "TaskStatus",
    "VerificationEvidence",
    "WorkOrderValidation",
]
