from __future__ import annotations

"""LLM contract: compatibility exports for subagent report dataclasses."""

from .planner_reports import (
    ParentPlannerParsedOutput,
    ParentPlannerRecord,
    ParentPlannerReport,
)
from .report_board_models import (
    ActionApplyRecord,
    ActionApplyReport,
    ActionPlanItem,
    ActionPlanReport,
    CapabilityRouteRecord,
    CapabilityRouteReport,
    DueCheckIssue,
    DueCheckReport,
    SubAgentBoard,
    SubAgentBoardItem,
)
from .report_dispatch_models import (
    DispatchRecord,
    DispatchReport,
    DispatchWatchRecord,
    DispatchWatchReport,
)
from .report_review_models import (
    AcceptanceReviewFinding,
    AcceptanceReviewRecord,
    AcceptanceReviewReport,
    PatchApplyRecord,
    PatchApplyReport,
    PatchReviewRecord,
    PatchReviewReport,
)

__all__ = [
    "AcceptanceReviewFinding",
    "AcceptanceReviewRecord",
    "AcceptanceReviewReport",
    "ActionApplyRecord",
    "ActionApplyReport",
    "ActionPlanItem",
    "ActionPlanReport",
    "CapabilityRouteRecord",
    "CapabilityRouteReport",
    "DispatchRecord",
    "DispatchReport",
    "DispatchWatchRecord",
    "DispatchWatchReport",
    "DueCheckIssue",
    "DueCheckReport",
    "ParentPlannerParsedOutput",
    "ParentPlannerRecord",
    "ParentPlannerReport",
    "PatchApplyRecord",
    "PatchApplyReport",
    "PatchReviewRecord",
    "PatchReviewReport",
    "SubAgentBoard",
    "SubAgentBoardItem",
]
