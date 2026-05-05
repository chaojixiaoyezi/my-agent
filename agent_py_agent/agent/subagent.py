from __future__ import annotations

"""LLM contract: compatibility facade for the focused subagents package.

Human version:
历史代码一直从 `agent.subagent` 导入各种类和函数。真正实现已经拆到
`agent.subagents.*`，这里保留旧入口，避免一次重构打断所有调用方。
"""

from .subagents.manager import SubAgentManager
from .subagents.manager_runner_results import RecordRunnerResultParams
from .subagents.models import (
    CapabilityGap,
    CapabilityGrant,
    CapabilityRequest,
    ChannelProbeCheck,
    ChannelProbeReport,
    ChannelProbeResult,
    ContextManifest,
    LearningCandidate,
    QualityContract,
    SubAgentCard,
    SubAgentExecutionContext,
    SubAgentParsedOutput,
    SubAgentRunnerResult,
    SubAgentTask,
    TakeoverRecord,
    VerificationEvidence,
    WorkOrderValidation,
)
from .subagents.parsing import parse_parent_planner_output, parse_subagent_runner_output
from .subagents.policies import filter_board_items
from .subagents.rendering import (
    render_acceptance_record_markdown,
    render_acceptance_review_markdown,
    render_action_apply_markdown,
    render_action_plan_markdown,
    render_board_markdown,
    render_capability_route_markdown,
    render_dispatch_markdown,
    render_dispatch_watch_markdown,
    render_due_check_markdown,
    render_parent_planner_markdown,
    render_patch_review_markdown,
    render_patch_review_record_markdown,
)
from .subagents.reports import (
    AcceptanceReviewFinding,
    AcceptanceReviewRecord,
    AcceptanceReviewReport,
    ActionApplyRecord,
    ActionApplyReport,
    ActionPlanItem,
    ActionPlanReport,
    CapabilityRouteRecord,
    CapabilityRouteReport,
    DispatchRecord,
    DispatchReport,
    DispatchWatchRecord,
    DispatchWatchReport,
    DueCheckIssue,
    DueCheckReport,
    ParentPlannerParsedOutput,
    ParentPlannerRecord,
    ParentPlannerReport,
    PatchApplyRecord,
    PatchApplyReport,
    PatchReviewRecord,
    PatchReviewReport,
    SubAgentBoard,
    SubAgentBoardItem,
)
from .subagents.runner_rendering import (
    render_channel_probe_markdown,
    render_execution_context_markdown,
    render_runner_result_markdown,
    render_single_channel_probe_markdown,
)
