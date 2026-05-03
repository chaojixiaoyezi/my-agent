from __future__ import annotations

"""LLM contract: pure subagent policies for filtering, risk, routing, and status mapping.

Human version:
这些函数是规则，不应该读写文件。比如哪些任务算红灯、某个 issue 应该映射成什么动作、
runner 上报 DONE 时为什么只能进入待验收。
"""

import time
from pathlib import Path

from ..capabilities import CapabilitySearchHit
from ..capability_config import CapabilityConfig
from .models import CapabilityGrant, CapabilityRequest, SubAgentParsedOutput, SubAgentTask
from .reports import ActionPlanItem, DueCheckIssue, SubAgentBoardItem


def filter_board_items(
    items: list[SubAgentBoardItem],
    *,
    status: str = "",
    owner: str = "",
    root_id: str = "",
) -> list[SubAgentBoardItem]:
    """按 CLI 参数过滤看板行。"""

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
    """让严重风险在 Hot List 里排前面。"""

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


def _make_due_issue(
    task: SubAgentTask,
    *,
    severity: str,
    kind: str,
    message: str,
    suggested_action: str,
    risk_flags: list[str],
    open_request_count: int,
    open_gap_count: int,
    age_seconds: float,
    stale_seconds: float,
) -> DueCheckIssue:
    """统一创建 due-check 问题，避免不同分支字段不一致。"""

    return DueCheckIssue(
        run_id=task.id,
        severity=severity,
        kind=kind,
        message=message,
        suggested_action=suggested_action,
        status=task.status,
        owner=task.owner,
        supervisor=task.supervisor,
        final_owner=task.final_owner,
        goal=task.goal,
        task_dir=task.task_dir,
        risk_flags=risk_flags,
        evidence_count=len(task.evidence),
        open_request_count=open_request_count,
        open_gap_count=open_gap_count,
        age_seconds=age_seconds,
        stale_seconds=stale_seconds,
        created_at=time.time(),
    )


def _issue_weight(issue: DueCheckIssue) -> int:
    """due-check 排序权重。"""

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


def _severity_weight(severity: str) -> int:
    """统一的 P0/P1/P2 权重。"""

    return {"P0": 1000, "P1": 500, "P2": 100}.get(severity, 0)


def _action_for_issue(issue: DueCheckIssue) -> tuple[str, int, str]:
    """把 due-check issue 映射为 dry-run 动作。"""

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
    """给 dry-run 动作提供下一步可运行命令。"""

    commands = {
        "probe_or_repair_channel": [
            f"python3 -m agent_py_agent subagents-probe {run_id}",
            f"python3 -m agent_py_agent subagent {run_id}",
        ],
        "inspect_channel_probe": [
            f"python3 -m agent_py_agent subagents-probe {run_id}",
            f"python3 -m agent_py_agent subagent {run_id}",
        ],
        "repair_work_order": [
            f"python3 -m agent_py_agent subagents-probe {run_id}",
            f"python3 -m agent_py_agent subagent {run_id}",
        ],
        "reopen_for_evidence": [
            f"python3 -m agent_py_agent subagent {run_id}",
        ],
        "run_acceptance": [
            f"python3 -m agent_py_agent subagent {run_id}",
        ],
        "takeover_or_reassign": [
            f"python3 -m agent_py_agent subagents-probe {run_id}",
            f"python3 -m agent_py_agent subagent {run_id}",
        ],
        "inspect_failure": [
            f"python3 -m agent_py_agent subagent {run_id}",
        ],
        "classify_blocker": [
            f"python3 -m agent_py_agent subagent {run_id}",
        ],
        "route_capability_request": [
            f"python3 -m agent_py_agent subagent {run_id}",
        ],
        "triage_capability_gap": [
            f"python3 -m agent_py_agent subagent {run_id}",
        ],
    }
    return commands.get(action, [f"python3 -m agent_py_agent subagent {run_id}"])


def _filter_action_plan_items(
    actions: list[ActionPlanItem],
    *,
    action_filter: str = "",
    run_id: str = "",
    limit: int = 0,
) -> list[ActionPlanItem]:
    """按 CLI 参数过滤动作计划。"""

    result = actions
    if action_filter:
        result = [item for item in result if item.action == action_filter]
    if run_id:
        result = [item for item in result if item.run_id == run_id]
    if limit > 0:
        result = result[:limit]
    return result


def _capability_request_query(task: SubAgentTask, request: CapabilityRequest) -> str:
    """把子代理能力请求压成检索 query。"""

    parts = [
        task.goal,
        request.needed_capability,
        request.problem,
        request.expected_output,
        " ".join(request.tried),
        " ".join(request.evidence),
        " ".join(f"{key}:{value}" for key, value in request.constraints.items()),
    ]
    return "\n".join(part for part in parts if part)


def _select_capability_hits(
    hits: list[CapabilitySearchHit],
    config: CapabilityConfig,
) -> list[CapabilitySearchHit]:
    """按配置限制挑选要下发的 skill/tool card。"""

    selected: list[CapabilitySearchHit] = []
    skill_count = 0
    tool_count = 0
    for hit in hits:
        if not _capability_hit_is_confident(hit):
            continue
        if hit.card.kind == "skill":
            if config.capability_grant_max_skills and skill_count >= config.capability_grant_max_skills:
                continue
            selected.append(hit)
            skill_count += 1
            continue
        if hit.card.kind == "tool":
            if config.capability_grant_max_tools and tool_count >= config.capability_grant_max_tools:
                continue
            selected.append(hit)
            tool_count += 1
    return selected


def _capability_hit_is_confident(hit: CapabilitySearchHit) -> bool:
    """过滤掉只因泛词弱命中的能力卡。"""

    return hit.score >= 4.0


def _route_card_payload(hit: CapabilitySearchHit) -> dict[str, str]:
    """把能力命中结果压成 grant 里可审计的短卡。"""

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
    """把模型上报状态压成 runner 允许的任务状态。"""

    status = parsed.status.upper().strip()
    if parsed.capability_requests or parsed.blocked_reason:
        return "BLOCKED"
    if status in {"BLOCKED", "FAILED", "CHANNEL_ERROR", "TIMEOUT"}:
        return status
    if status in {"DONE", "COMPLETED", "COMPLETE", "SUCCESS", "AWAITING_ACCEPTANCE"}:
        return "AWAITING_ACCEPTANCE"
    return "AWAITING_ACCEPTANCE"


def _verification_from_runner_status(status: str) -> str:
    """runner 不能直接 VERIFIED，只能进入待验收或未验收。"""

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
    """根据 runner 结果给机器读的下一步建议。"""

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


def _dedupe_granted_cards(
    grants: list[CapabilityGrant],
    *,
    max_cards: int = 0,
) -> list[dict[str, str]]:
    """从 capability grants 中提取去重后的短卡。"""

    cards: list[dict[str, str]] = []
    seen: set[str] = set()
    for grant in grants:
        for card in grant.capability_cards:
            key = card.get("id") or f"{card.get('kind')}:{card.get('name')}"
            if not key or key in seen:
                continue
            seen.add(key)
            cards.append(
                {str(item_key): str(item_value) for item_key, item_value in card.items()}
            )
            if max_cards > 0 and len(cards) >= max_cards:
                return cards
    return cards


def _execution_context_instructions() -> list[str]:
    """生成子代理执行上下文里的硬规则。"""

    return [
        "只能使用本上下文列出的 allowed_skills、allowed_tools 和 granted_cards。",
        "不要读取或展开全局 skill/tool registry；缺能力时提交 capability_request。",
        "工具失败要记录 tried/evidence，并优先在已授权能力内换 fallback；无可用 fallback 时上抛。",
        "完成前必须写入可验收 evidence，不能只口头声明完成。",
        "写入只允许发生在 allowed_write_roots 内，禁止写 forbidden_write_roots 和 locked_files。",
        "如果通道损坏、工单文件缺失或任务边界不清，先标记 BLOCKED 并等待父代理处理。",
    ]


def _is_active(status: str) -> bool:
    """判断任务是否仍应有心跳和运行时限。"""

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
    """默认禁止子代理写入的高风险目录。"""

    home = Path.home()
    return [
        str(home),
        str(home / "Desktop"),
        str(home / "Downloads"),
        str(home / ".openclaw"),
    ]

