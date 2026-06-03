
from __future__ import annotations

"""Board, due-check, action, and capability route report models."""

from dataclasses import dataclass, field


@dataclass
class SubAgentBoardItem:
    """One subagent status row for dashboards and startup recovery."""

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
    # Current-layout workspace refs; old work-order paths are intentionally omitted.
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
    """Subagent dashboard snapshot for humans and startup recovery."""

    generated_at: float
    summary: dict[str, int]
    hot_list: list[SubAgentBoardItem]
    recent: list[SubAgentBoardItem]
    items: list[SubAgentBoardItem]


@dataclass
class DueCheckIssue:
    """One due-check issue for a stale, blocked, or risky subagent run."""

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
    """Due-check summary plus issue rows."""

    generated_at: float
    summary: dict[str, int]
    issues: list[DueCheckIssue]


@dataclass
class ActionPlanItem:
    """One proposed recovery/action item derived from due-check findings."""

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
    """Action-plan report for dry-run review or explicit apply."""

    generated_at: float
    summary: dict[str, int]
    actions: list[ActionPlanItem]


@dataclass
class ActionApplyRecord:
    """Audit row for one applied or previewed action-plan item."""

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
    """Action-apply report for recovery audit and dashboard display."""

    generated_at: float
    dry_run: bool
    summary: dict[str, int]
    records: list[ActionApplyRecord]


@dataclass
class CapabilityRouteRecord:
    """One capability-request routing decision."""

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
    """Capability routing summary plus selected grants."""

    generated_at: float
    dry_run: bool
    summary: dict[str, int]
    records: list[CapabilityRouteRecord]
