from __future__ import annotations

"""LLM: individual policy check functions for risk weighting, status mapping, and due-check dispatch.

给人看的解释：
这些函数各自是一条规则——某个 issue 应该排多前面、某个 runner 状态应该映射成什么任务状态、
某个能力请求应该怎么检索。它们不读写文件，也不依赖运行时状态。
"""

import time
from dataclasses import dataclass
from pathlib import Path

from ..capabilities import CapabilitySearchHit
from ..capability_config import CapabilityConfig
from .models import CapabilityGrant, CapabilityRequest, SubAgentParsedOutput, SubAgentTask
from .reports import ActionPlanItem, DueCheckIssue, SubAgentBoardItem


def _risk_weight(flags: list[str]) -> int:
    """LLM: compute risk weight so severe risks appear first in Hot List.

    新手说明:
    让严重风险在 Hot List 里排前面。返回值越大越靠前。
    """

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


def _make_due_issue(*, params: MakeDueIssueParams) -> DueCheckIssue:
    """LLM: unified factory for due-check issues to keep fields consistent across branches.

    新手说明:
    统一创建 due-check 问题，避免不同分支字段不一致。
    """
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
        created_at=time.time(),
    )


def _severity_weight(severity: str) -> int:
    """LLM: map P0/P1/P2 severity to a numeric weight for sorting.

    新手说明:
    统一的 P0/P1/P2 权重。P0 最严重排最前。
    """

    return {"P0": 1000, "P1": 500, "P2": 100}.get(severity, 0)


def _issue_weight(issue: DueCheckIssue) -> int:
    """LLM: compute due-check sorting weight combining severity and kind.

    新手说明:
    due-check 排序权重。严重程度和问题类型各贡献一部分分数。
    """

    severity_weight = _severity_weight(issue.severity)
    kind_weight = {
        "missing_work_order_files": 90,
        "fake_done_risk": 85,
        "run_timeout": 80,
        "heartbeat_stale": 70,
        "channel_broken": 68,
        "status_failed": 65,
        "status_timeout": 65,
        "status_channel_error": 60,
        "status_blocked": 50,
        "channel_probe_missing": 48,
        "unverified_done": 45,
        "channel_degraded": 42,
        "open_capability_request": 40,
        "open_capability_gap": 20,
    }.get(issue.kind, 1)
    return severity_weight + kind_weight


def _action_for_issue(issue: DueCheckIssue) -> tuple[str, int, str]:
    """LLM: map a due-check issue to a dry-run action tuple.

    新手说明:
    把 due-check issue 映射为 dry-run 动作。返回 (动作名, 优先级, 状态变更)。
    """

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
        return "run_acceptance", 760, ""
    if kind in {"run_timeout", "heartbeat_stale", "status_timeout"}:
        return "takeover_or_reassign", 900, "TIMEOUT"
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
    """LLM: provide runnable CLI commands for a dry-run action.

    新手说明:
    给 dry-run 动作提供下一步可运行命令。
    """

    # LLM: prefer the installed console script; it avoids Windows python3 shim issues.
    cli = "my-agent"
    commands = {
        "probe_or_repair_channel": [
            f"{cli} subagents-probe {run_id}",
            f"{cli} subagent {run_id}",
        ],
        "inspect_channel_probe": [
            f"{cli} subagents-probe {run_id}",
            f"{cli} subagent {run_id}",
        ],
        "repair_work_order": [
            f"{cli} subagents-probe {run_id}",
            f"{cli} subagent {run_id}",
        ],
        "reopen_for_evidence": [
            f"{cli} subagent {run_id}",
        ],
        "run_acceptance": [
            f"{cli} subagent {run_id}",
        ],
        "takeover_or_reassign": [
            f"{cli} subagents-probe {run_id}",
            f"{cli} subagent {run_id}",
        ],
        "inspect_failure": [
            f"{cli} subagent {run_id}",
        ],
        "classify_blocker": [
            f"{cli} subagent {run_id}",
        ],
        "route_capability_request": [
            f"{cli} subagent {run_id}",
        ],
        "triage_capability_gap": [
            f"{cli} subagent {run_id}",
        ],
    }
    return commands.get(action, [f"{cli} subagent {run_id}"])


def _status_from_structured_output(parsed: SubAgentParsedOutput) -> str:
    """LLM: normalize model-reported status into runner-allowed task status.

    新手说明:
    把模型上报状态压成 runner 允许的任务状态。有 capability_requests 或 blocked_reason 就变 BLOCKED，
    完成类状态统一变成 AWAITING_ACCEPTANCE。
    """

    status = parsed.status.upper().strip()
    if parsed.capability_requests or parsed.blocked_reason:
        return "BLOCKED"
    if status in {"BLOCKED", "FAILED", "CHANNEL_ERROR", "TIMEOUT"}:
        return status
    if status in {"DONE", "COMPLETED", "COMPLETE", "SUCCESS", "AWAITING_ACCEPTANCE"}:
        return "AWAITING_ACCEPTANCE"
    return "AWAITING_ACCEPTANCE"


def _verification_from_runner_status(status: str) -> str:
    """LLM: derive verification status from runner status; runner cannot directly set VERIFIED.

    新手说明:
    runner 不能直接 VERIFIED，只能进入待验收或未验收。
    """

    if status.upper() == "AWAITING_ACCEPTANCE":
        return "NEEDS_ACCEPTANCE"
    return "UNVERIFIED"


def _runner_next_action(
    *,
    dry_run: bool,
    ok: bool,
    status: str,
    capability_request_count: int,
    next_actions: list[str] | None = None,
) -> str:
    """LLM: compute machine-readable next-action suggestion from runner result.

    新手说明:
    根据 runner 结果给机器读的下一步建议。dry_run 不建议动作，有 capability_request 就路由，
    有 next_actions 就取第一个，ok 且待验收就跑验收，否则检查失败。
    """

    if dry_run:
        return ""
    if capability_request_count:
        return "route_capability_request"
    if next_actions:
        return next_actions[0]
    if ok and status == "AWAITING_ACCEPTANCE":
        return "run_acceptance"
    if not ok:
        return "inspect_runner_failure"
    return ""


def _is_active(status: str) -> bool:
    """LLM: check whether a task should still have heartbeat and runtime limits.

    新手说明:
    判断任务是否仍应有心跳和运行时限。终态任务不算 active。
    """

    return status.upper() not in {
        "DONE",
        "FAILED",
        "BLOCKED",
        "AWAITING_ACCEPTANCE",
        "TIMEOUT",
        "CHANNEL_ERROR",
        "TAKEN_OVER",
    }


def _default_forbidden_write_roots() -> list[str]:
    """LLM: return default high-risk directories that subagents must not write to.

    新手说明:
    默认禁止子代理写入的高风险目录，比如用户主目录和桌面。
    """

    home = Path.home()
    return [
        str(home),
        str(home / "Desktop"),
        str(home / "Downloads"),
        str(home / ".openclaw"),
    ]
