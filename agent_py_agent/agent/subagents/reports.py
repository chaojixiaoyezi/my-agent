from __future__ import annotations

"""LLM contract: subagent report, review, dispatch, and planner dataclasses.

Human version:
这里放“父代理看什么报告、怎么验收、怎么调度”的数据结构。
它们和 SubAgentTask 分开，是为了让运行状态和报告输出各自变化。
"""

from dataclasses import dataclass, field

@dataclass
class SubAgentBoardItem:
    """子代理看板里的一行机器事实。"""

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
    task_dir: str
    output_json: str


@dataclass
class SubAgentBoard:
    """子代理看板，兼顾机器读取和人类扫视。"""

    generated_at: float
    summary: dict[str, int]
    hot_list: list[SubAgentBoardItem]
    recent: list[SubAgentBoardItem]
    items: list[SubAgentBoardItem]


@dataclass
class DueCheckIssue:
    """父代理巡检发现的一条待处理问题。"""

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
    created_at: float = 0.0


@dataclass
class DueCheckReport:
    """父代理 due-check 报告。

    这份报告是后续自动接管、重派、缩小目标和能力路由的机器输入。
    """

    generated_at: float
    summary: dict[str, int]
    issues: list[DueCheckIssue]


@dataclass
class ActionPlanItem:
    """由 due-check 转出来的一条 dry-run 动作。"""

    id: str
    run_id: str
    severity: str
    priority: int
    action: str
    reason: str
    source_issue_kinds: list[str]
    suggested_commands: list[str] = field(default_factory=list)
    would_change_status_to: str = ""
    requires_confirmation: bool = True
    dry_run: bool = True
    owner: str = ""
    final_owner: str = ""
    task_dir: str = ""
    created_at: float = 0.0


@dataclass
class ActionPlanReport:
    """父代理动作计划报告。

    当前只用于 dry-run，不直接修改任何子代理运行状态。
    """

    generated_at: float
    summary: dict[str, int]
    actions: list[ActionPlanItem]


@dataclass
class ActionApplyRecord:
    """一次 action apply 的审计记录。"""

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
    evidence_paths: list[str] = field(default_factory=list)
    created_at: float = 0.0


@dataclass
class ActionApplyReport:
    """action apply 报告。

    dry-run 时只说明会做什么；apply 时才会真的修改子代理运行记录。
    """

    generated_at: float
    dry_run: bool
    summary: dict[str, int]
    records: list[ActionApplyRecord]


@dataclass
class CapabilityRouteRecord:
    """一次 capability request 路由记录。"""

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
    grant_id: str = ""
    gap_id: str = ""
    message: str = ""
    created_at: float = 0.0


@dataclass
class CapabilityRouteReport:
    """能力请求路由报告。"""

    generated_at: float
    dry_run: bool
    summary: dict[str, int]
    records: list[CapabilityRouteRecord]


@dataclass
class AcceptanceReviewFinding:
    """一次验收检查中的单项结论。"""

    name: str
    ok: bool
    severity: str
    message: str
    evidence_path: str = ""
    created_at: float = 0.0


@dataclass
class AcceptanceReviewRecord:
    """单个子代理运行的验收记录。"""

    id: str
    run_id: str
    dry_run: bool
    applied: bool
    ok: bool
    decision: str
    message: str
    before_status: str
    after_status: str
    before_verification_status: str
    after_verification_status: str
    reviewer: str = ""
    note: str = ""
    evidence_count: int = 0
    test_count: int = 0
    artifact_count: int = 0
    findings: list[AcceptanceReviewFinding] = field(default_factory=list)
    evidence_paths: list[str] = field(default_factory=list)
    created_at: float = 0.0


@dataclass
class AcceptanceReviewReport:
    """父代理验收报告。"""

    generated_at: float
    dry_run: bool
    summary: dict[str, int]
    records: list[AcceptanceReviewRecord]


@dataclass
class PatchReviewRecord:
    """runner patch 输出的审核记录。"""

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
    created_at: float = 0.0


@dataclass
class PatchReviewReport:
    """批量 patch 审核报告。"""

    generated_at: float
    dry_run: bool
    summary: dict[str, int]
    records: list[PatchReviewRecord]


@dataclass
class DispatchRecord:
    """父代理调度器的一步审计记录。"""

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
    created_at: float = 0.0


@dataclass
class DispatchReport:
    """父代理调度器报告。"""

    generated_at: float
    dry_run: bool
    summary: dict[str, int]
    records: list[DispatchRecord]


@dataclass
class DispatchWatchRecord:
    """父代理 watch 模式的一轮循环记录。"""

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
    """父代理 watch 模式报告。"""

    generated_at: float
    dry_run: bool
    summary: dict[str, int]
    records: list[DispatchWatchRecord]


@dataclass
class ParentPlannerParsedOutput:
    """父代理 planner 的结构化模型输出。"""

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
    """父代理 planner 的一次审计记录。"""

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
    created_at: float = 0.0


@dataclass
class ParentPlannerReport:
    """父代理 planner 报告。"""

    generated_at: float
    dry_run: bool
    summary: dict[str, int]
    records: list[ParentPlannerRecord]


