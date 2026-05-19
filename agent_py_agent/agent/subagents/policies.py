# LLM: Subagent orchestration module; keep task workspace, manager facade, and report contracts stable.
# 模块用途: 支撑主代理派发、跟踪、验收、汇总子代理任务。

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from ..capabilities import CapabilitySearchHit
from ..capability_config import CapabilityConfig
from .capability_status import is_pending_capability_status
from .models import CapabilityGrant, CapabilityRequest, SubAgentParsedOutput, SubAgentTask
from .reports import ActionPlanItem, DueCheckIssue, SubAgentBoardItem


# LLM: filter_board_items 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 处理filter看板条目相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、执行器结果、验收和报告展示上的返回值和副作用边界稳定。
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
# LLM: _risk_weight 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 处理riskweight相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、执行器结果、验收和报告展示上的返回值和副作用边界稳定。
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
# LLM: MakeDueIssueParams 属于子代理任务管理的类边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 类用途: 集中保存make到期issue参数字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
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
    # LLM: related_refs carries structured child/run refs from due-check into rescue packets.
    related_refs: list[str] | None = None


# LLM: RunnerNextActionParams 属于子代理任务管理的类边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 类用途: 集中保存执行器next动作参数字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass(frozen=True)
class RunnerNextActionParams:
    dry_run: bool = False
    ok: bool = False
    status: str = ""
    capability_request_count: int = 0
    next_actions: list[str] | None = None


# LLM: _make_due_issue 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 构建到期issue所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 主要返回派生结构或文本，需保持字段名、顺序和空值处理稳定。
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
# LLM: _issue_weight 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 处理issueweight相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、执行器结果、验收和报告展示上的返回值和副作用边界稳定。
def _issue_weight(issue: DueCheckIssue) -> int:
    severity_weight = _severity_weight(issue.severity)
    kind_weight = {
        "missing_work_order_files": 90,
        "fake_done_risk": 85,
        "run_timeout": 80,
        "status_provider_timeout": 80,
        "status_artifact_integrity_failed": 79,
        "artifact_repair_failed": 79,
        "artifact_integrity_repair_completed": 78,
        "parent_acceptance_test_failed": 79,
        "parent_acceptance_repair_failed": 79,
        "parent_acceptance_repair_completed": 78,
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
# LLM: _severity_weight 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 处理severityweight相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、执行器结果、验收和报告展示上的返回值和副作用边界稳定。
def _severity_weight(severity: str) -> int:
    return {"P0": 1000, "P1": 500, "P2": 100}.get(severity, 0)
# LLM: _action_for_issue 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 处理动作issue相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、执行器结果、验收和报告展示上的返回值和副作用边界稳定。
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
        return "run_acceptance", 760, ""
    if kind in {"run_timeout", "heartbeat_stale", "status_timeout", "status_provider_timeout"}:
        return "takeover_or_reassign", 900, "TIMEOUT"
    if kind == "artifact_repair_failed":
        return "takeover_or_reassign", 900, "BLOCKED"
    if kind == "status_artifact_integrity_failed":
        return "create_repair_child_from_artifact_integrity_refs", 890, "BLOCKED"
    if kind == "artifact_integrity_repair_completed":
        return "close_parent_from_verified_repair_child", 880, "DONE"
    if kind == "parent_acceptance_test_failed":
        return "create_repair_child_from_parent_acceptance_refs", 890, "BLOCKED"
    if kind == "parent_acceptance_repair_failed":
        return "takeover_or_reassign", 900, "BLOCKED"
    if kind == "parent_acceptance_repair_completed":
        return "close_parent_from_verified_repair_child", 880, "DONE"
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
# LLM: _commands_for_action 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 处理commands动作相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、执行器结果、验收和报告展示上的返回值和副作用边界稳定。
def _commands_for_action(action: str, run_id: str) -> list[str]:
    # LLM: 优先使用已安装命令行入口，兼顾 Windows 可用性和 macOS/Linux 文档清晰度。
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
# LLM: _filter_action_plan_items 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 处理filter动作计划条目相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、执行器结果、验收和报告展示上的返回值和副作用边界稳定。
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
# LLM: _capability_request_query 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 处理能力请求查询相关的数据流，连接当前职责的前后步骤；关键副作用: 可能触发网络输入输出或消费流式响应，需保留错误传播语义。
def _capability_request_query(task: SubAgentTask, request: CapabilityRequest) -> str:
    # LLM: Include scoped request terms so shell/MCP/tool needs can route without broad prompt context.
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
# LLM: _select_capability_hits 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 读取或查询能力hits需要的状态，返回调用方可继续处理的快照；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
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
# LLM: _capability_kind_limit_reached 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 处理能力kind限制reached相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、执行器结果、验收和报告展示上的返回值和副作用边界稳定。
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
# LLM: _capability_hit_is_confident 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 处理能力hitisconfident相关的数据流，连接当前职责的前后步骤；关键副作用: 主要返回判断或抛出明确异常，调用方依赖布尔语义稳定。
def _capability_hit_is_confident(hit: CapabilitySearchHit) -> bool:
    if hit.score >= 5.0:
        return True
    if hit.score < 4.0:
        return False
    return any(not str(reason or "").startswith("命中描述") for reason in getattr(hit, "reasons", []) or [])
# LLM: _route_card_payload 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 处理routecard载荷相关的数据流，连接当前职责的前后步骤；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
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
# LLM: _status_from_structured_output 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 处理来自状态structuredoutput相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、执行器结果、验收和报告展示上的返回值和副作用边界稳定。
def _status_from_structured_output(parsed: SubAgentParsedOutput) -> str:
    status = parsed.status.upper().strip()
    if parsed.capability_requests or parsed.blocked_reason:
        return "BLOCKED"
    if is_pending_capability_status(status):
        return "BLOCKED"
    if status in {"BLOCKED", "FAILED", "CHANNEL_ERROR", "TIMEOUT"}:
        return status
    if status in {"DONE", "COMPLETED", "COMPLETE", "SUCCESS", "AWAITING_ACCEPTANCE"}:
        return "AWAITING_ACCEPTANCE"
    return "AWAITING_ACCEPTANCE"


# LLM: _verification_from_runner_status 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 处理来自verification执行器状态相关的数据流，连接当前职责的前后步骤；关键副作用: 会影响任务状态、执行器结果、验收和报告展示，需保持重试、超时和状态迁移语义。
def _verification_from_runner_status(status: str) -> str:
    if status.upper() == "AWAITING_ACCEPTANCE":
        return "NEEDS_ACCEPTANCE"
    return "UNVERIFIED"
# LLM: _runner_next_action 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 推进执行器next动作的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响任务状态、执行器结果、验收和报告展示，需保持重试、超时和状态迁移语义。
def _runner_next_action(*, params: RunnerNextActionParams) -> str:
    if params.dry_run:
        return ""
    if params.capability_request_count:
        return "route_capability_request"
    if params.next_actions:
        return params.next_actions[0]
    if params.ok and params.status == "AWAITING_ACCEPTANCE":
        return "run_acceptance"
    if not params.ok:
        return "inspect_runner_failure"
    return ""
# LLM: _dedupe_granted_cards 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 处理dedupegrantedcards相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、执行器结果、验收和报告展示上的返回值和副作用边界稳定。
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
# LLM: _new_grant_cards 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 构建grantcards所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 需保持任务状态、执行器结果、验收和报告展示上的返回值和副作用边界稳定。
def _new_grant_cards(grant: CapabilityGrant, seen: set[str]) -> list[dict[str, str]]:
    cards: list[dict[str, str]] = []
    for card in grant.capability_cards:
        key = card.get("id") or f"{card.get('kind')}:{card.get('name')}"
        if not key or key in seen:
            continue
        seen.add(key)
        cards.append({str(item_key): str(item_value) for item_key, item_value in card.items()})
    return cards
# LLM: _execution_context_instructions 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 处理execution上下文instructions相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、执行器结果、验收和报告展示上的返回值和副作用边界稳定。
def _execution_context_instructions() -> list[str]:
    return [
        "只能使用本上下文列出的 allowed_skills、allowed_tools 和 granted_cards。",
        "不要读取或展开全局 skill/tool registry；缺能力时提交 capability_request。",
        "工具失败要记录 tried/evidence，并优先在已授权能力内换 fallback；无可用 fallback 时上抛。",
        "完成前必须写入可验收 evidence，不能只口头声明完成。",
        "写入只允许发生在 allowed_write_roots 内，禁止写 forbidden_write_roots 和 locked_files。",
        "如果通道损坏、工单文件缺失或任务边界不清，先标记 BLOCKED 并等待父代理处理。",
    ]
# LLM: _is_active 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 判断active条件是否成立，作为后续调度或分支决策的门禁；关键副作用: 主要返回判断或抛出明确异常，调用方依赖布尔语义稳定。
def _is_active(status: str) -> bool:
    return status.upper() not in {
        "DONE",
        "FAILED",
        "BLOCKED",
        "AWAITING_ACCEPTANCE",
        "TIMEOUT",
        "CHANNEL_ERROR",
        "TAKEN_OVER",
    }
# LLM: _default_forbidden_write_roots 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 处理defaultforbiddenwriteroots相关的数据流，连接当前职责的前后步骤；关键副作用: 会改动任务状态、执行器结果、验收和报告展示，调用方依赖写入顺序和文件格式。
def _default_forbidden_write_roots() -> list[str]:
    home = Path.home()
    return [
        str(home),
        str(home / "Desktop"),
        str(home / "Downloads"),
        str(home / ".openclaw"),
    ]
