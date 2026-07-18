
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from agent_py_agent.agent.capability import CapabilitySearchHit
from agent_py_agent.agent.capability.config import CapabilityConfig

from .model_capabilities import is_pending_capability_status
from .models import (
    SUBAGENT_ENDED_STATUSES,
    SUBAGENT_FAILURE_STATUSES,
    CapabilityGrant,
    CapabilityRequest,
    SubAgentParsedOutput,
    SubAgentTask,
    TaskStatus,
    VerificationStatus,
    task_status_in,
)
from .reports import ActionPlanItem, DueCheckIssue, SubAgentBoardItem

ISSUE_MISSING_WORK_ORDER_FILES = "missing_work_order_files"
ISSUE_FAKE_DONE_RISK = "fake_done_risk"
ISSUE_RUN_TIMEOUT = "run_timeout"
ISSUE_NO_PROGRESS_FUSE = "no_progress_fuse"
ISSUE_COORDINATOR_NEEDS_LEADERSHIP_RECOVERY = "coordinator_needs_leadership_recovery"
ISSUE_HEARTBEAT_STALE = "heartbeat_stale"
ISSUE_CHANNEL_BROKEN = "channel_broken"
ISSUE_STATUS_FAILED = "status_failed"
ISSUE_STATUS_TIMEOUT = "status_timeout"
ISSUE_STATUS_CHANNEL_ERROR = "status_channel_error"
ISSUE_PARENT_TIMEOUT_WITH_UNFINISHED_CHILDREN = "parent_timeout_with_unfinished_children"
ISSUE_COORDINATOR_HEARTBEAT_STALE = "coordinator_heartbeat_stale"
ISSUE_STATUS_BLOCKED = "status_blocked"
ISSUE_CHANNEL_PROBE_MISSING = "channel_probe_missing"
ISSUE_UNVERIFIED_DONE = "unverified_done"
ISSUE_CHANNEL_DEGRADED = "channel_degraded"
ISSUE_OPEN_CAPABILITY_REQUEST = "open_capability_request"
ISSUE_OPEN_CAPABILITY_GAP = "open_capability_gap"

ACTION_PROBE_OR_REPAIR_CHANNEL = "probe_or_repair_channel"
ACTION_INSPECT_CHANNEL_PROBE = "inspect_channel_probe"
ACTION_REPAIR_WORK_ORDER = "repair_work_order"
ACTION_REOPEN_FOR_EVIDENCE = "reopen_for_evidence"
ACTION_TAKEOVER_OR_REASSIGN = "takeover_or_reassign"
ACTION_STOP_NO_PROGRESS_AND_ESCALATE = "stop_no_progress_and_escalate"
ACTION_RECOVER_COORDINATOR_LEADERSHIP = "recover_coordinator_leadership"
ACTION_RECOVER_CHILD_AFTER_PARENT_TIMEOUT = "recover_child_after_parent_timeout"
ACTION_INSPECT_FAILURE = "inspect_failure"
ACTION_CLASSIFY_BLOCKER = "classify_blocker"
ACTION_ROUTE_CAPABILITY_REQUEST = "route_capability_request"
ACTION_TRIAGE_CAPABILITY_GAP = "triage_capability_gap"
ACTION_INSPECT_MANUALLY = "inspect_manually"

RISK_FLAG_FAILED = "failed"
RISK_FLAG_TIMEOUT = "timeout"
RISK_FLAG_CHANNEL_ERROR = "channel_error"
RISK_FLAG_BLOCKED = "blocked"
RISK_FLAG_CHANNEL_BROKEN = "channel_broken"
RISK_FLAG_MISSING_WORK_ORDER_FILES = ISSUE_MISSING_WORK_ORDER_FILES
RISK_FLAG_DONE_WITHOUT_EVIDENCE = "done_without_evidence"
RISK_FLAG_DONE_WITHOUT_VERIFICATION = "done_without_verification"
RISK_FLAG_CHANNEL_DEGRADED = "channel_degraded"
RISK_FLAG_OPEN_CAPABILITY_GAP = ISSUE_OPEN_CAPABILITY_GAP
RISK_FLAG_OPEN_CAPABILITY_REQUEST = ISSUE_OPEN_CAPABILITY_REQUEST
RISK_FLAG_TAKEN_OVER = "taken_over"

SEVERITY_WEIGHTS = {
    "P0": 1000,
    "P1": 500,
    "P2": 100,
}

RISK_FLAG_WEIGHTS = {
    RISK_FLAG_FAILED: 100,
    RISK_FLAG_TIMEOUT: 95,
    RISK_FLAG_CHANNEL_ERROR: 90,
    RISK_FLAG_BLOCKED: 80,
    RISK_FLAG_CHANNEL_BROKEN: 75,
    RISK_FLAG_MISSING_WORK_ORDER_FILES: 70,
    RISK_FLAG_DONE_WITHOUT_EVIDENCE: 60,
    RISK_FLAG_DONE_WITHOUT_VERIFICATION: 55,
    RISK_FLAG_CHANNEL_DEGRADED: 52,
    RISK_FLAG_OPEN_CAPABILITY_GAP: 50,
    RISK_FLAG_OPEN_CAPABILITY_REQUEST: 40,
    RISK_FLAG_TAKEN_OVER: 30,
}

ISSUE_WEIGHTS = {
    ISSUE_MISSING_WORK_ORDER_FILES: 90,
    ISSUE_FAKE_DONE_RISK: 85,
    ISSUE_RUN_TIMEOUT: 80,
    ISSUE_NO_PROGRESS_FUSE: 78,
    ISSUE_COORDINATOR_NEEDS_LEADERSHIP_RECOVERY: 77,
    ISSUE_HEARTBEAT_STALE: 70,
    ISSUE_CHANNEL_BROKEN: 68,
    ISSUE_STATUS_FAILED: 65,
    ISSUE_STATUS_TIMEOUT: 65,
    ISSUE_STATUS_CHANNEL_ERROR: 60,
    ISSUE_PARENT_TIMEOUT_WITH_UNFINISHED_CHILDREN: 59,
    ISSUE_COORDINATOR_HEARTBEAT_STALE: 58,
    ISSUE_STATUS_BLOCKED: 50,
    ISSUE_CHANNEL_PROBE_MISSING: 48,
    ISSUE_UNVERIFIED_DONE: 45,
    ISSUE_CHANNEL_DEGRADED: 42,
    ISSUE_OPEN_CAPABILITY_REQUEST: 40,
    ISSUE_OPEN_CAPABILITY_GAP: 20,
}

TIMEOUT_ISSUE_KINDS = frozenset({
    ISSUE_RUN_TIMEOUT,
    ISSUE_HEARTBEAT_STALE,
    ISSUE_STATUS_TIMEOUT,
})
PROBE_ACTIONS = frozenset({
    ACTION_PROBE_OR_REPAIR_CHANNEL,
    ACTION_INSPECT_CHANNEL_PROBE,
    ACTION_REPAIR_WORK_ORDER,
    ACTION_TAKEOVER_OR_REASSIGN,
})
RECOVERY_TREE_ACTIONS = frozenset({
    ACTION_RECOVER_COORDINATOR_LEADERSHIP,
    ACTION_RECOVER_CHILD_AFTER_PARENT_TIMEOUT,
})


@dataclass(frozen=True)
class DueActionPolicy:
    action: str
    priority: int
    would_change_status_to: str = ""


ACTION_POLICIES = {
    ISSUE_CHANNEL_BROKEN: DueActionPolicy(ACTION_PROBE_OR_REPAIR_CHANNEL, 980, "CHANNEL_ERROR"),
    ISSUE_CHANNEL_PROBE_MISSING: DueActionPolicy(ACTION_PROBE_OR_REPAIR_CHANNEL, 980, "CHANNEL_ERROR"),
    ISSUE_STATUS_CHANNEL_ERROR: DueActionPolicy(ACTION_PROBE_OR_REPAIR_CHANNEL, 980, "CHANNEL_ERROR"),
    ISSUE_CHANNEL_DEGRADED: DueActionPolicy(ACTION_INSPECT_CHANNEL_PROBE, 780),
    ISSUE_MISSING_WORK_ORDER_FILES: DueActionPolicy(ACTION_REPAIR_WORK_ORDER, 960, "BLOCKED"),
    ISSUE_FAKE_DONE_RISK: DueActionPolicy(ACTION_REOPEN_FOR_EVIDENCE, 940, "BLOCKED"),
    ISSUE_UNVERIFIED_DONE: DueActionPolicy(ACTION_REOPEN_FOR_EVIDENCE, 760, "BLOCKED"),
    ISSUE_RUN_TIMEOUT: DueActionPolicy(ACTION_TAKEOVER_OR_REASSIGN, 900, "TIMEOUT"),
    ISSUE_HEARTBEAT_STALE: DueActionPolicy(ACTION_TAKEOVER_OR_REASSIGN, 900, "TIMEOUT"),
    ISSUE_STATUS_TIMEOUT: DueActionPolicy(ACTION_TAKEOVER_OR_REASSIGN, 900, "TIMEOUT"),
    ISSUE_NO_PROGRESS_FUSE: DueActionPolicy(ACTION_STOP_NO_PROGRESS_AND_ESCALATE, 990),
    ISSUE_COORDINATOR_NEEDS_LEADERSHIP_RECOVERY: DueActionPolicy(ACTION_RECOVER_COORDINATOR_LEADERSHIP, 930),
    ISSUE_PARENT_TIMEOUT_WITH_UNFINISHED_CHILDREN: DueActionPolicy(ACTION_RECOVER_CHILD_AFTER_PARENT_TIMEOUT, 830),
    ISSUE_COORDINATOR_HEARTBEAT_STALE: DueActionPolicy(ACTION_RECOVER_COORDINATOR_LEADERSHIP, 820),
    ISSUE_STATUS_FAILED: DueActionPolicy(ACTION_INSPECT_FAILURE, 860),
    ISSUE_STATUS_BLOCKED: DueActionPolicy(ACTION_CLASSIFY_BLOCKER, 740),
    ISSUE_OPEN_CAPABILITY_REQUEST: DueActionPolicy(ACTION_ROUTE_CAPABILITY_REQUEST, 700),
    ISSUE_OPEN_CAPABILITY_GAP: DueActionPolicy(ACTION_TRIAGE_CAPABILITY_GAP, 420),
}


@dataclass(frozen=True)
class RescuePolicy:
    strategy: str
    target: str


RESCUE_POLICIES = {
    ISSUE_RUN_TIMEOUT: RescuePolicy("takeover_or_shrink_scope_before_retry", "parent"),
    ISSUE_HEARTBEAT_STALE: RescuePolicy("takeover_or_shrink_scope_before_retry", "parent"),
    ISSUE_STATUS_TIMEOUT: RescuePolicy("takeover_or_shrink_scope_before_retry", "parent"),
    ISSUE_PARENT_TIMEOUT_WITH_UNFINISHED_CHILDREN: RescuePolicy(
        "recover_unfinished_children_after_parent_timeout",
        "parent",
    ),
    ISSUE_STATUS_FAILED: RescuePolicy("inspect_failure_then_rescue_or_escalate", "parent"),
    ISSUE_STATUS_BLOCKED: RescuePolicy("inspect_failure_then_rescue_or_escalate", "parent"),
    ISSUE_CHANNEL_BROKEN: RescuePolicy("repair_channel_before_retry", "runtime_owner"),
    ISSUE_CHANNEL_PROBE_MISSING: RescuePolicy("repair_channel_before_retry", "runtime_owner"),
    ISSUE_STATUS_CHANNEL_ERROR: RescuePolicy("repair_channel_before_retry", "runtime_owner"),
    ISSUE_OPEN_CAPABILITY_REQUEST: RescuePolicy("route_capability_request_before_retry", "capability_router"),
    ISSUE_OPEN_CAPABILITY_GAP: RescuePolicy(
        "escalate_capability_gap_for_tooling_or_learning",
        "capability_owner",
    ),
    ISSUE_FAKE_DONE_RISK: RescuePolicy("reopen_and_request_missing_evidence", "parent"),
    ISSUE_MISSING_WORK_ORDER_FILES: RescuePolicy("repair_work_order_before_any_retry", "parent"),
}


def filter_board_items(
    items: list[SubAgentBoardItem],
    *,
    status: str = "",
    owner: str = "",
    root_id: str = "",
) -> list[SubAgentBoardItem]:
    result = items
    if status:
        normalized = status.upper()
        result = [item for item in result if item.status == normalized]
    if owner:
        result = [
            item
            for item in result
            if item.owner == owner or item.supervisor == owner or item.final_owner == owner
        ]
    if root_id:
        result = [item for item in result if item.root_id == root_id]
    return result
def _risk_weight(flags: list[str]) -> int:
    return max((RISK_FLAG_WEIGHTS.get(item, 1) for item in flags), default=0)
@dataclass(frozen=True)
class MakeDueIssueParams:
    task: SubAgentTask
    severity: str
    kind: str
    message: str
    suggested_action: str
    risk_flags: list[str]
    open_request_count: int
    open_gap_count: int
    age_seconds: float
    stale_seconds: float
    related_refs: list[str] | None = None


@dataclass(frozen=True)
class RunnerNextActionParams:
    dry_run: bool = False
    ok: bool = False
    status: str = ""
    capability_request_count: int = 0
    next_actions: list[str] | None = None


def _make_due_issue(*, params: MakeDueIssueParams) -> DueCheckIssue:
    return DueCheckIssue(
        run_id=params.task.id,
        severity=params.severity,
        kind=params.kind,
        message=params.message,
        suggested_action=params.suggested_action,
        status=params.task.status,
        owner=params.task.owner,
        supervisor=params.task.supervisor,
        final_owner=params.task.final_owner,
        goal=params.task.goal,
        task_dir=params.task.task_dir,
        risk_flags=params.risk_flags,
        evidence_count=len(params.task.evidence),
        open_request_count=params.open_request_count,
        open_gap_count=params.open_gap_count,
        age_seconds=params.age_seconds,
        stale_seconds=params.stale_seconds,
        related_refs=list(params.related_refs or []),
        created_at=time.time(),
    )
def _issue_weight(issue: DueCheckIssue) -> int:
    severity_weight = _severity_weight(issue.severity)
    kind_weight = ISSUE_WEIGHTS.get(issue.kind, 1)
    return severity_weight + kind_weight
def _severity_weight(severity: str) -> int:
    return SEVERITY_WEIGHTS.get(severity, 0)
def _action_for_issue(issue: DueCheckIssue) -> tuple[str, int, str]:
    if policy := ACTION_POLICIES.get(issue.kind):
        return policy.action, policy.priority, policy.would_change_status_to
    return issue.suggested_action or ACTION_INSPECT_MANUALLY, 100, ""
def _commands_for_action(action: str, run_id: str) -> list[str]:
    cli = "my-agent"
    if action == ACTION_STOP_NO_PROGRESS_AND_ESCALATE:
        return [f"{cli} subagent {run_id}", f"{cli} subagents-recovery-tree {run_id} --hide-healthy"]
    if action in PROBE_ACTIONS:
        return [f"{cli} subagents-probe {run_id}", f"{cli} subagent {run_id}"]
    if action in RECOVERY_TREE_ACTIONS:
        return [f"{cli} subagents-recovery-tree {run_id} --hide-healthy", f"{cli} subagent {run_id}"]
    return [f"{cli} subagent {run_id}"]
def _filter_action_plan_items(
    actions: list[ActionPlanItem],
    *,
    action_filter: str = "",
    run_id: str = "",
    limit: int = 0,
) -> list[ActionPlanItem]:
    result = actions
    if action_filter:
        result = [item for item in result if item.action == action_filter]
    if run_id:
        result = [item for item in result if item.run_id == run_id]
    if limit > 0:
        result = result[:limit]
    return result
def _capability_request_query(task: SubAgentTask, request: CapabilityRequest) -> str:
    scoped_type = "" if request.capability_type == "generic" else request.capability_type
    parts = [
        request.needed_capability,
        scoped_type,
        " ".join(request.requested_tools),
        " ".join(request.requested_skills),
        " ".join(request.requested_mcp_tools),
        " ".join(request.requested_commands),
        " ".join(f"{key}:{value}" for key, value in request.constraints.items()),
    ]
    return "\n".join(part for part in parts if part)
def _select_capability_hits(
    hits: list[CapabilitySearchHit],
    config: CapabilityConfig,
) -> list[CapabilitySearchHit]:
    selected: list[CapabilitySearchHit] = []
    counts = {"skill": 0, "tool": 0}
    for hit in hits:
        if not _capability_hit_is_confident(hit):
            continue
        if _capability_kind_limit_reached(hit, config, counts):
            continue
        selected.append(hit)
        if hit.card.kind in counts:
            counts[hit.card.kind] += 1
    return selected
def _capability_kind_limit_reached(
    hit: CapabilitySearchHit,
    config: CapabilityConfig,
    counts: dict[str, int],
) -> bool:
    if hit.card.kind == "skill":
        return bool(config.capability_grant_max_skills and counts["skill"] >= config.capability_grant_max_skills)
    if hit.card.kind == "tool":
        return bool(config.capability_grant_max_tools and counts["tool"] >= config.capability_grant_max_tools)
    return False
def _capability_hit_is_confident(hit: CapabilitySearchHit) -> bool:
    return hit.score >= 4.0
def _route_card_payload(hit: CapabilitySearchHit) -> dict[str, str]:
    card = hit.card
    payload = {
        "id": card.id,
        "kind": card.kind,
        "name": card.name,
        "description": card.description[:240],
        "risk_level": card.risk_level,
        "source": card.source,
        "path": "" if card.kind == "skill" else card.path,
        "score": f"{hit.score:.2f}",
        "reasons": "；".join(hit.reasons[:4]),
    }
    if card.kind == "skill":
        payload["stable_id"] = str(card.metadata.get("stable_id") or card.name)
        payload["content_sha256"] = str(card.metadata.get("content_sha256") or "")
    return payload
def _status_from_structured_output(parsed: SubAgentParsedOutput) -> str:
    status = parsed.status.strip()
    if parsed.capability_requests:
        return TaskStatus.BLOCKED.value
    if is_pending_capability_status(status):
        return TaskStatus.BLOCKED.value
    if task_status_in(status, SUBAGENT_FAILURE_STATUSES):
        return status
    if task_status_in(status, {TaskStatus.DONE.value}):
        return TaskStatus.DONE.value
    return TaskStatus.BLOCKED.value


def _verification_from_runner_status(status: str) -> str:
    if task_status_in(status, {TaskStatus.DONE.value}):
        return VerificationStatus.VERIFIED.value
    return VerificationStatus.UNVERIFIED.value
def _runner_next_action(*, params: RunnerNextActionParams) -> str:
    if params.dry_run:
        return ""
    if params.capability_request_count:
        return "route_capability_request"
    if params.next_actions:
        return params.next_actions[0]
    if not params.ok:
        return "inspect_runner_failure"
    return ""
def _dedupe_granted_cards(
    grants: list[CapabilityGrant],
    *,
    max_cards: int = 0,
) -> list[dict[str, str]]:
    cards: list[dict[str, str]] = []
    seen: set[str] = set()
    for grant in grants:
        cards.extend(_new_grant_cards(grant, seen))
        if max_cards > 0 and len(cards) >= max_cards:
            return cards[:max_cards]
    return cards
def _new_grant_cards(grant: CapabilityGrant, seen: set[str]) -> list[dict[str, str]]:
    cards: list[dict[str, str]] = []
    for card in grant.capability_cards:
        key = card.get("id") or f"{card.get('kind')}:{card.get('name')}"
        if not key or key in seen:
            continue
        seen.add(key)
        cards.append({str(item_key): str(item_value) for item_key, item_value in card.items()})
    return cards
def _execution_context_instructions() -> list[str]:
    return [
        "只能使用本上下文列出的 allowed_skills、allowed_tools 和 granted_cards。",
        "不要读取或展开全局 skill/tool registry；缺能力时提交 capability_request。",
        "工具失败要记录 tried/evidence，并优先在已授权能力内换替代方案；无可用替代方案时上抛。",
        "完成前必须写入可验收 evidence，不能只口头声明完成。",
        "写入只允许发生在 allowed_write_roots 内，禁止写 forbidden_write_roots 和 locked_files。",
        "如果通道损坏、工单文件缺失或任务边界不清，先标记 BLOCKED 并等待父代理处理。",
    ]
def _is_active(status: str) -> bool:
    return not task_status_in(status, SUBAGENT_ENDED_STATUSES)
def _default_forbidden_write_roots() -> list[str]:
    home = Path.home()
    return [
        str(home),
        str(home / "Desktop"),
        str(home / "Downloads"),
        str(home / ".ssh"),
    ]
