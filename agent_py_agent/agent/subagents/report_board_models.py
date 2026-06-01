# LLM: Subagent orchestration module; keep task workspace, manager facade, and report contracts stable.
# 模块用途: 支撑主代理派发、跟踪、验收、汇总子代理任务。

from __future__ import annotations

"""Board, due-check, action, and capability route report models."""

from dataclasses import dataclass, field


# LLM: SubAgentBoardItem is an operator-facing row; model tools should prefer inspect_agent_tree.
# 类用途: 保存看板行的当前状态、进度、产物引用和时间信息；不携带旧 work-order 路径。
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
    # LLM: target_tokens expose concrete artifact ownership without reading artifact bodies.
    target_tokens: list[str] = field(default_factory=list)
    # LLM: artifact refs make completed work directly recoverable from board rows without path guessing.
    artifact_refs: list[str] = field(default_factory=list)
    # LLM: artifact registry refs are the machine ledger records behind artifact_refs.
    artifact_registry_refs: list[dict[str, object]] = field(default_factory=list)
    # LLM: evidence refs keep verification/supporting facts visible without expanding artifact bodies.
    evidence_refs: list[str] = field(default_factory=list)
    # LLM: board rows expose task-tree evidence and child state without reading logs.
    evidence_packet_count: int = 0
    finding_count: int = 0
    child_status_counts: dict[str, int] = field(default_factory=dict)
    progress: float = 0.0
    current_step: str = ""
    latest_summary: str = ""
    blocker_count: int = 0
    # LLM: timing fields are observability only; they help parents judge staleness without dispatching.
    running_seconds: float = 0.0
    seconds_since_progress: float = 0.0


# LLM: SubAgentBoard is a compact dashboard snapshot, not the task tree authority.
# 类用途: 保存看板摘要、风险行和最近行；真实层级状态以 kernel/inspect_agent_tree 为准。
@dataclass
class SubAgentBoard:
    """Subagent dashboard snapshot for humans and startup recovery."""

    generated_at: float
    summary: dict[str, int]
    hot_list: list[SubAgentBoardItem]
    recent: list[SubAgentBoardItem]
    items: list[SubAgentBoardItem]


# LLM: DueCheckIssue is one stale/blocking signal surfaced to humans and recovery tools.
# 类用途: 保存一个子代理看板问题的状态、建议动作和相关 refs。
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
    # LLM: related_refs keeps due-check recovery hints machine-readable without parsing messages.
    related_refs: list[str] = field(default_factory=list)
    created_at: float = 0.0


# LLM: DueCheckReport groups due-check issues without mutating subagent state.
# 类用途: 保存一次 due-check 扫描的摘要和问题列表。
@dataclass
class DueCheckReport:
    """Due-check summary plus issue rows."""

    generated_at: float
    summary: dict[str, int]
    issues: list[DueCheckIssue]


# LLM: ActionPlanItem is a proposed action, not permission to execute it.
# 类用途: 描述一个可审核的恢复/推进动作及其理由、范围和确认要求。
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
    # LLM: rescue metadata keeps escalation visible before any mutating action runs.
    rescue_trigger: str = ""
    rescue_strategy: str = ""
    escalation_target: str = ""
    rescue_context_refs: list[str] = field(default_factory=list)
    # LLM: rescue packet records refs-only retry/escalation policy; it does not authorize auto execution.
    rescue_packet: dict[str, object] = field(default_factory=dict)
    requires_confirmation: bool = True
    dry_run: bool = True
    owner: str = ""
    final_owner: str = ""
    task_dir: str = ""
    created_at: float = 0.0


# LLM: ActionPlanReport packages proposed actions for review before any apply step.
# 类用途: 保存一次 action planning 的摘要和候选动作。
@dataclass
class ActionPlanReport:
    """Action-plan report for dry-run review or explicit apply."""

    generated_at: float
    summary: dict[str, int]
    actions: list[ActionPlanItem]


# LLM: ActionApplyRecord is the audit row for one previewed or applied action.
# 类用途: 记录单个恢复动作是否 dry-run、是否实际应用以及状态变化。
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
    # LLM: apply records preserve the rescue packet that justified the action.
    rescue_packet: dict[str, object] = field(default_factory=dict)
    evidence_paths: list[str] = field(default_factory=list)
    created_at: float = 0.0


# LLM: ActionApplyReport is the aggregate audit result for action application.
# 类用途: 保存一次 apply/dry-run 的总体摘要和每条动作记录。
@dataclass
class ActionApplyReport:
    """Action-apply report for recovery audit and dashboard display."""

    generated_at: float
    dry_run: bool
    summary: dict[str, int]
    records: list[ActionApplyRecord]


# LLM: CapabilityRouteRecord records one capability request routing decision.
# 类用途: 保存能力请求匹配、授予、缺口和 dry-run 边界信息。
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
    # LLM: route scope fields stay refs-only so dry-run reports can show boundaries without executing tools.
    request_scope: dict[str, object] = field(default_factory=dict)
    grant_scope: dict[str, object] = field(default_factory=dict)
    grant_id: str = ""
    gap_id: str = ""
    message: str = ""
    created_at: float = 0.0


# LLM: CapabilityRouteReport aggregates capability routing decisions for the board.
# 类用途: 保存一次能力路由扫描的摘要和多条路由记录。
@dataclass
class CapabilityRouteReport:
    """Capability routing summary plus selected grants."""

    generated_at: float
    dry_run: bool
    summary: dict[str, int]
    records: list[CapabilityRouteRecord]
