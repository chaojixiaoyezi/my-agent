from __future__ import annotations

"""Board, due-check, action, and capability route report models."""

from dataclasses import dataclass, field


@dataclass
class SubAgentBoardItem:
    """瀛愪唬鐞嗙湅鏉块噷鐨勪竴琛屾満鍣ㄤ簨瀹炪€?"""

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
    # LLM: board rows expose task-tree evidence and child state without reading logs.
    evidence_packet_count: int = 0
    finding_count: int = 0
    child_status_counts: dict[str, int] = field(default_factory=dict)
    progress: float = 0.0
    current_step: str = ""
    latest_summary: str = ""
    blocker_count: int = 0


@dataclass
class SubAgentBoard:
    """瀛愪唬鐞嗙湅鏉匡紝鍏奸【鏈哄櫒璇诲彇鍜屼汉绫绘壂瑙嗐€?"""

    generated_at: float
    summary: dict[str, int]
    hot_list: list[SubAgentBoardItem]
    recent: list[SubAgentBoardItem]
    items: list[SubAgentBoardItem]


@dataclass
class DueCheckIssue:
    """鐖朵唬鐞嗗贰妫€鍙戠幇鐨勪竴鏉″緟澶勭悊闂銆?"""

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
    """鐖朵唬鐞?due-check 鎶ュ憡銆?"""

    generated_at: float
    summary: dict[str, int]
    issues: list[DueCheckIssue]


@dataclass
class ActionPlanItem:
    """鐢?due-check 杞嚭鏉ョ殑涓€鏉?dry-run 鍔ㄤ綔銆?"""

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
    requires_confirmation: bool = True
    dry_run: bool = True
    owner: str = ""
    final_owner: str = ""
    task_dir: str = ""
    created_at: float = 0.0


@dataclass
class ActionPlanReport:
    """鐖朵唬鐞嗗姩浣滆鍒掓姤鍛娿€?"""

    generated_at: float
    summary: dict[str, int]
    actions: list[ActionPlanItem]


@dataclass
class ActionApplyRecord:
    """涓€娆?action apply 鐨勫璁¤褰曘€?"""

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
    evidence_paths: list[str] = field(default_factory=list)
    created_at: float = 0.0


@dataclass
class ActionApplyReport:
    """action apply 鎶ュ憡銆?"""

    generated_at: float
    dry_run: bool
    summary: dict[str, int]
    records: list[ActionApplyRecord]


@dataclass
class CapabilityRouteRecord:
    """涓€娆?capability request 璺敱璁板綍銆?"""

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
    """鑳藉姏璇锋眰璺敱鎶ュ憡銆?"""

    generated_at: float
    dry_run: bool
    summary: dict[str, int]
    records: list[CapabilityRouteRecord]
