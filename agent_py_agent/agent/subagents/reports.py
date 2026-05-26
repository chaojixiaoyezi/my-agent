# LLM: Subagent orchestration module; keep task workspace, manager facade, and report contracts stable.
# 模块用途: 支撑主代理派发、跟踪、验收、汇总子代理任务。

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
    PatchApplyRecord,
    PatchApplyReport,
    PatchReviewRecord,
    PatchReviewReport,
)

__all__ = [
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
