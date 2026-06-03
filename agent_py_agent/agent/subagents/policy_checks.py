
from __future__ import annotations

"""individual policy check functions for risk weighting, status mapping, and due-check dispatch.

这些函数各自是一条规则——某个 issue 应该排多前面、某个 runner 状态应该映射成什么任务状态、
某个能力请求应该怎么检索。它们不读写文件，也不依赖运行时状态。
"""

import time
from dataclasses import dataclass
from pathlib import Path

from ..capabilities import CapabilitySearchHit
from ..capability_config import CapabilityConfig
from .capability_status import is_pending_capability_status
from .models import CapabilityGrant, CapabilityRequest, SubAgentParsedOutput, SubAgentTask
from .reports import ActionPlanItem, DueCheckIssue, SubAgentBoardItem


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
    """Params bundle for _make_due_issue."""
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
    """Params bundle for deciding the runner follow-up action."""

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


def _severity_weight(severity: str) -> int:

    return {"P0": 1000, "P1": 500, "P2": 100}.get(severity, 0)


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
    if action in _probe_command_actions():
        return [f"{cli} subagents-probe {run_id}", f"{cli} subagent {run_id}"]
    if action in _recovery_tree_command_actions():
        return [f"{cli} subagents-recovery-tree {run_id} --hide-healthy", f"{cli} subagent {run_id}"]
    return [f"{cli} subagent {run_id}"]


def _probe_command_actions() -> set[str]:
    return {
        "probe_or_repair_channel",
        "inspect_channel_probe",
        "repair_work_order",
        "takeover_or_reassign",
    }


def _recovery_tree_command_actions() -> set[str]:
    return {
        "recover_coordinator_leadership",
        "recover_child_after_parent_timeout",
        "stop_no_progress_and_escalate",
    }


def _status_from_structured_output(parsed: SubAgentParsedOutput) -> str:

    status = parsed.status.upper().strip()
    if parsed.capability_requests or parsed.blocked_reason:
        return "BLOCKED"
    if is_pending_capability_status(status):
        return "BLOCKED"
    if status in {"BLOCKED", "FAILED", "CHANNEL_ERROR", "TIMEOUT"}:
        return status
    if status in {"DONE", "COMPLETED", "COMPLETE", "SUCCESS"}:
        return "DONE"
    return "DONE"


def _verification_from_runner_status(status: str) -> str:

    if status.upper() in {"DONE", "COMPLETED", "COMPLETE", "SUCCESS"}:
        return "VERIFIED"
    return "UNVERIFIED"


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


def _is_active(status: str) -> bool:

    return status.upper() not in {
        "DONE",
        "FAILED",
        "BLOCKED",
        "TIMEOUT",
        "CHANNEL_ERROR",
        "TAKEN_OVER",
    }


def _default_forbidden_write_roots() -> list[str]:

    home = Path.home()
    return [
        str(home),
        str(home / "Desktop"),
        str(home / "Downloads"),
        str(home / ".ssh"),
    ]
