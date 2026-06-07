
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
    weights = {
        "failed": 100,
        "timeout": 95,
        "channel_error": 90,
        "blocked": 80,
        "channel_broken": 75,
        "missing_work_order_files": 70,
        "done_without_evidence": 60,
        "done_without_verification": 55,
        "channel_degraded": 52,
        "open_capability_gap": 50,
        "open_capability_request": 40,
        "taken_over": 30,
    }
    return max((weights.get(item, 1) for item in flags), default=0)
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
    kind_weight = {
        "missing_work_order_files": 90,
        "fake_done_risk": 85,
        "run_timeout": 80,
        "no_progress_fuse": 78,
        "coordinator_needs_leadership_recovery": 77,
        "heartbeat_stale": 70,
        "channel_broken": 68,
        "status_failed": 65,
        "status_timeout": 65,
        "status_channel_error": 60,
        "parent_timeout_with_unfinished_children": 59,
        "coordinator_heartbeat_stale": 58,
        "status_blocked": 50,
        "channel_probe_missing": 48,
        "unverified_done": 45,
        "channel_degraded": 42,
        "open_capability_request": 40,
        "open_capability_gap": 20,
    }.get(issue.kind, 1)
    return severity_weight + kind_weight
def _severity_weight(severity: str) -> int:
    return {"P0": 1000, "P1": 500, "P2": 100}.get(severity, 0)
def _action_for_issue(issue: DueCheckIssue) -> tuple[str, int, str]:
    kind = issue.kind
    if kind in {"channel_broken", "channel_probe_missing", "status_channel_error"}:
        return "probe_or_repair_channel", 980, "CHANNEL_ERROR"
    if kind == "channel_degraded":
        return "inspect_channel_probe", 780, ""
    if kind == "missing_work_order_files":
        return "repair_work_order", 960, "BLOCKED"
    if kind == "fake_done_risk":
        return "reopen_for_evidence", 940, "BLOCKED"
    if kind == "unverified_done":
        return "reopen_for_evidence", 760, "BLOCKED"
    if kind in {"run_timeout", "heartbeat_stale", "status_timeout"}:
        return "takeover_or_reassign", 900, "TIMEOUT"
    if kind == "no_progress_fuse":
        return "stop_no_progress_and_escalate", 990, ""
    if kind == "coordinator_needs_leadership_recovery":
        return "recover_coordinator_leadership", 930, ""
    if kind == "parent_timeout_with_unfinished_children":
        return "recover_child_after_parent_timeout", 830, ""
    if kind == "coordinator_heartbeat_stale":
        return "recover_coordinator_leadership", 820, ""
    if kind == "status_failed":
        return "inspect_failure", 860, ""
    if kind == "status_blocked":
        return "classify_blocker", 740, ""
    if kind == "open_capability_request":
        return "route_capability_request", 700, ""
    if kind == "open_capability_gap":
        return "triage_capability_gap", 420, ""
    return issue.suggested_action or "inspect_manually", 100, ""
def _commands_for_action(action: str, run_id: str) -> list[str]:
    cli = "my-agent"
    probe_actions = {
        "probe_or_repair_channel",
        "inspect_channel_probe",
        "repair_work_order",
        "takeover_or_reassign",
    }
    if action == "stop_no_progress_and_escalate":
        return [f"{cli} subagent {run_id}", f"{cli} subagents-recovery-tree {run_id} --hide-healthy"]
    if action in probe_actions:
        return [f"{cli} subagents-probe {run_id}", f"{cli} subagent {run_id}"]
    if action in {"recover_coordinator_leadership", "recover_child_after_parent_timeout"}:
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
        task.goal,
        request.needed_capability,
        request.problem,
        request.expected_output,
        scoped_type,
        " ".join(request.tried),
        " ".join(request.evidence),
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
    return {
        "id": card.id,
        "kind": card.kind,
        "name": card.name,
        "description": card.description[:240],
        "risk_level": card.risk_level,
        "source": card.source,
        "path": card.path,
        "score": f"{hit.score:.2f}",
        "reasons": "；".join(hit.reasons[:4]),
    }
def _status_from_structured_output(parsed: SubAgentParsedOutput) -> str:
    status = parsed.status.upper().strip()
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
