from __future__ import annotations

"""Subagent report dataclasses.

These models are consumed as one report surface by dispatch, board, patch, and
policy services. Keeping them here avoids a facade that only re-exported four
small model files.
"""

from dataclasses import dataclass, field


@dataclass
class ParentPlannerParsedOutput:
    found: bool
    ok: bool
    decision: str = ""
    summary: str = ""
    should_dispatch: bool = True
    runner_instruction: str = ""
    suggested_max_runners: int = 0
    actions: list[dict[str, object]] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    parse_error: str = ""
    raw_json: dict[str, object] = field(default_factory=dict)


@dataclass
class ParentPlannerRecord:
    id: str
    dry_run: bool
    triggered: bool
    ok: bool
    decision: str
    message: str
    gate_summary: dict[str, int] = field(default_factory=dict)
    backend: str = ""
    tool_rounds: int = 0
    parse_error: str = ""
    summary: str = ""
    actions: list[dict[str, object]] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    runner_instruction: str = ""
    suggested_max_runners: int = 0
    prompt_path: str = ""
    response_path: str = ""
    evidence_paths: list[str] = field(default_factory=list)
    runtime_error: dict[str, object] = field(default_factory=dict)
    created_at: float = 0.0


@dataclass
class ParentPlannerReport:
    generated_at: float
    dry_run: bool
    summary: dict[str, int]
    records: list[ParentPlannerRecord]


@dataclass
class SubAgentBoardItem:
    id: str
    root_id: str
    parent_id: str
    depth: int
    status: str
    verification_status: str
    channel_status: str
    owner: str
    supervisor: str
    final_owner: str
    goal: str
    updated_at: float
    heartbeat_at: float
    evidence_count: int
    open_request_count: int
    open_gap_count: int
    child_count: int
    takeover_by: str
    locked_file_count: int
    risk_flags: list[str]
    task_root: str
    final_report_ref: str
    task_work_dir: str = ""
    task_output_dir: str = ""
    agent_work_dir: str = ""
    checkpoint_ref: str = ""
    summary_ref: str = ""
    latest_tool_progress_ref: str = ""
    agent_name: str = ""
    role: str = ""
    target_tokens: list[str] = field(default_factory=list)
    artifact_refs: list[str] = field(default_factory=list)
    artifact_registry_refs: list[dict[str, object]] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    evidence_packet_count: int = 0
    finding_count: int = 0
    child_status_counts: dict[str, int] = field(default_factory=dict)
    child_status_load_errors: list[dict[str, object]] = field(default_factory=list)
    progress: float = 0.0
    current_step: str = ""
    latest_summary: str = ""
    blocker_count: int = 0
    running_seconds: float = 0.0
    seconds_since_progress: float = 0.0


@dataclass
class SubAgentBoard:
    generated_at: float
    summary: dict[str, int]
    hot_list: list[SubAgentBoardItem]
    recent: list[SubAgentBoardItem]
    items: list[SubAgentBoardItem]


@dataclass
class DueCheckIssue:
    run_id: str
    severity: str
    kind: str
    message: str
    suggested_action: str
    status: str = ""
    owner: str = ""
    supervisor: str = ""
    final_owner: str = ""
    goal: str = ""
    task_dir: str = ""
    risk_flags: list[str] = field(default_factory=list)
    evidence_count: int = 0
    open_request_count: int = 0
    open_gap_count: int = 0
    age_seconds: float = 0.0
    stale_seconds: float = 0.0
    related_refs: list[str] = field(default_factory=list)
    created_at: float = 0.0


@dataclass
class DueCheckReport:
    generated_at: float
    summary: dict[str, int]
    issues: list[DueCheckIssue]


@dataclass
class ActionPlanItem:
    id: str
    run_id: str
    severity: str
    priority: int
    action: str
    reason: str
    source_issue_kinds: list[str]
    suggested_commands: list[str] = field(default_factory=list)
    would_change_status_to: str = ""
    rescue_trigger: str = ""
    rescue_strategy: str = ""
    escalation_target: str = ""
    rescue_context_refs: list[str] = field(default_factory=list)
    rescue_packet: dict[str, object] = field(default_factory=dict)
    requires_confirmation: bool = True
    dry_run: bool = True
    owner: str = ""
    final_owner: str = ""
    task_dir: str = ""
    created_at: float = 0.0


@dataclass
class ActionPlanReport:
    generated_at: float
    summary: dict[str, int]
    actions: list[ActionPlanItem]


@dataclass
class ActionApplyRecord:
    id: str
    action_id: str
    run_id: str
    action: str
    dry_run: bool
    applied: bool
    ok: bool
    message: str
    before_status: str = ""
    after_status: str = ""
    before_channel_status: str = ""
    after_channel_status: str = ""
    rescue_trigger: str = ""
    rescue_strategy: str = ""
    escalation_target: str = ""
    rescue_context_refs: list[str] = field(default_factory=list)
    rescue_packet: dict[str, object] = field(default_factory=dict)
    evidence_paths: list[str] = field(default_factory=list)
    created_at: float = 0.0


@dataclass
class ActionApplyReport:
    generated_at: float
    dry_run: bool
    summary: dict[str, int]
    records: list[ActionApplyRecord]


@dataclass
class CapabilityRouteRecord:
    id: str
    run_id: str
    request_id: str
    status: str
    dry_run: bool
    query: str
    candidate_count: int
    granted_skills: list[str] = field(default_factory=list)
    granted_tools: list[str] = field(default_factory=list)
    selected_cards: list[dict[str, str]] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    request_scope: dict[str, object] = field(default_factory=dict)
    grant_scope: dict[str, object] = field(default_factory=dict)
    grant_id: str = ""
    gap_id: str = ""
    message: str = ""
    created_at: float = 0.0


@dataclass
class CapabilityRouteReport:
    generated_at: float
    dry_run: bool
    summary: dict[str, int]
    records: list[CapabilityRouteRecord]


@dataclass
class DispatchRecord:
    id: str
    step: str
    action: str
    run_id: str
    dry_run: bool
    applied: bool
    ok: bool
    message: str
    before_status: str = ""
    after_status: str = ""
    before_verification_status: str = ""
    after_verification_status: str = ""
    evidence_paths: list[str] = field(default_factory=list)
    runner_summary: str = ""
    runner_created_child_count: int = 0
    runner_created_child_ids: list[str] = field(default_factory=list)
    runner_created_roles: list[str] = field(default_factory=list)
    runner_child_status_counts: dict[str, int] = field(default_factory=dict)
    runner_unfinished_child_ids: list[str] = field(default_factory=list)
    runner_child_load_errors: list[dict[str, object]] = field(default_factory=list)
    collaboration_candidate_load_errors: list[dict[str, object]] = field(default_factory=list)
    runner_partial_success: bool = False
    runner_instruction: str = ""
    suggested_max_runners: int = 0
    created_at: float = 0.0


@dataclass
class DispatchReport:
    generated_at: float
    dry_run: bool
    summary: dict[str, int]
    records: list[DispatchRecord]


@dataclass
class DispatchWatchRecord:
    id: str
    cycle: int
    dry_run: bool
    ok: bool
    message: str
    dispatch_record_count: int
    dispatch_summary: dict[str, int] = field(default_factory=dict)
    started_at: float = 0.0
    ended_at: float = 0.0
    evidence_paths: list[str] = field(default_factory=list)


@dataclass
class DispatchWatchReport:
    generated_at: float
    dry_run: bool
    summary: dict[str, int]
    records: list[DispatchWatchRecord]


@dataclass
class PatchReviewRecord:
    id: str
    run_id: str
    dry_run: bool
    applied: bool
    ok: bool
    decision: str
    message: str
    patch_count: int
    approved_count: int = 0
    blocked_count: int = 0
    reviewer: str = ""
    note: str = ""
    evidence_paths: list[str] = field(default_factory=list)
    patches: list[dict[str, object]] = field(default_factory=list)
    load_errors: list[dict[str, object]] = field(default_factory=list)
    created_at: float = 0.0


@dataclass
class PatchReviewReport:
    generated_at: float
    dry_run: bool
    summary: dict[str, int]
    records: list[PatchReviewRecord]


@dataclass
class PatchApplyRecord:
    id: str
    run_id: str
    dry_run: bool
    applied: bool
    ok: bool
    decision: str
    message: str
    patch_count: int
    applied_count: int = 0
    blocked_count: int = 0
    rollback_performed: bool = False
    applier: str = ""
    note: str = ""
    evidence_paths: list[str] = field(default_factory=list)
    test_commands: list[str] = field(default_factory=list)
    test_results: list[dict[str, object]] = field(default_factory=list)
    patches: list[dict[str, object]] = field(default_factory=list)
    load_errors: list[dict[str, object]] = field(default_factory=list)
    owner_policy: dict[str, object] = field(default_factory=dict)
    batch_validation: dict[str, object] = field(default_factory=dict)
    failure_recovery: dict[str, object] = field(default_factory=dict)
    created_at: float = 0.0


@dataclass
class PatchApplyReport:
    generated_at: float
    dry_run: bool
    summary: dict[str, int]
    records: list[PatchApplyRecord]


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
