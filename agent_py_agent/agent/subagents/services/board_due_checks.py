# LLM: Subagent orchestration module; keep task workspace, manager facade, and report contracts stable.
# 模块用途: 支撑主代理派发、跟踪、验收、汇总子代理任务。

from __future__ import annotations

"""due-check issue builders for subagent board service.

给人看的解释：
这里把单任务巡检拆出 board.py，用一个上下文对象承载重复参数，避免每个检查函数都有长参数列表。
"""

import json
from pathlib import Path

from .board_due_models import (
    DueInspectionContext,
    DueIssueSpec,
    InspectTaskDueRequest,
    _single_issue,
)
from .board_due_timeout_checks import (
    check_coordinator_heartbeat_issues,
    check_heartbeat_timeout_issues,
    check_run_timeout_issues,
)
from .board_parent_timeout import check_parent_timeout_child_issues
from .recovery_strategy import SubagentRecoveryStrategyRequest, build_subagent_recovery_strategy


# LLM: artifact repair runs are one-shot deterministic recovery attempts; failed attempts must hand off.
# 函数用途: 判断当前 task 是否已经是 artifact_integrity 修复任务，避免 repair child 递归套娃。
def _is_artifact_integrity_repair_task(task) -> bool:
    attrs = getattr(task, "attributes", {}) or {}
    return isinstance(attrs, dict) and attrs.get("repair_kind") == "artifact_integrity"


# LLM: _is_parent_acceptance_repair_task is part of this module's structured runtime path; keep callers and tests aligned before changing it.
# 函数用途: 完成本模块中的转换、校验或状态整理，供相邻流程继续使用。
def _is_parent_acceptance_repair_task(task) -> bool:
    attrs = getattr(task, "attributes", {}) or {}
    return isinstance(attrs, dict) and attrs.get("repair_kind") == "parent_acceptance"


# LLM: _check_work_order_issues 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 函数用途: 校验workorderissues需要的输入和状态，不满足时把错误明确反馈给调用方；关键副作用: 主要返回判断或抛出明确异常，调用方依赖布尔语义稳定。
def _check_work_order_issues(ctx: DueInspectionContext, validation):
    """Check for missing work order files."""
    if validation.ok:
        return []
    return [
        _single_issue(
            ctx,
            DueIssueSpec(
                "P0",
                "missing_work_order_files",
                f"工单目录缺少 {len(validation.missing)} 个关键路径，后续接管和验收不可靠。",
                "repair_work_order",
            ),
        )
    ]


# LLM: _check_status_issues 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 函数用途: 校验状态issues需要的输入和状态，不满足时把错误明确反馈给调用方；关键副作用: 主要返回判断或抛出明确异常，调用方依赖布尔语义稳定。
def _check_status_issues(ctx: DueInspectionContext, manager):
    """Check for failed/timeout/channel error/blocked status issues."""
    task = ctx.task
    if task.status not in {"FAILED", "TIMEOUT", "CHANNEL_ERROR", "BLOCKED"}:
        return []
    failure_type = str(getattr(task, "failure_type", "") or "").lower()
    if task.status == "BLOCKED" and failure_type == "provider_timeout":
        return [
            _single_issue(
                ctx,
                DueIssueSpec(
                    "P1",
                    "status_provider_timeout",
                    "任务因模型接口超时阻塞；应基于已有 task refs 接管或重试，而不是只做人工分类。",
                    "takeover_or_reassign",
                ),
            )
        ]
    if task.status == "BLOCKED" and failure_type == "artifact_integrity_failed":
        if _is_artifact_integrity_repair_task(task):
            return [
                _single_issue(
                    ctx,
                    DueIssueSpec(
                        "P1",
                        "artifact_repair_failed",
                        "artifact repair 任务仍未修好；应创建 takeover run 继承 refs 继续，不能再创建同类 repair child。",
                        "takeover_or_reassign",
                    ),
                )
            ]
        if repair := _verified_repair_child(manager, task, "artifact_integrity"):
            return [
                _single_issue(
                    ctx,
                    DueIssueSpec(
                        "P1",
                        "artifact_integrity_repair_completed",
                        "产物修复子任务已通过验收；应把父任务从 BLOCKED 收口为已完成，避免重复创建修复子任务。",
                        "close_parent_from_verified_repair_child",
                        related_refs=[repair.task_dir, repair.output_json],
                    ),
                )
            ]
        return [
            _single_issue(
                ctx,
                DueIssueSpec(
                    "P1",
                    "status_artifact_integrity_failed",
                    "任务产物结构检查失败；应创建 scoped repair worker 读取 failure refs 修复产物。",
                    "create_repair_child_from_artifact_integrity_refs",
                ),
            )
        ]
    severity = {"FAILED": "P0", "TIMEOUT": "P0", "CHANNEL_ERROR": "P0", "BLOCKED": "P1"}[task.status]
    action = {
        "FAILED": "inspect_failure_and_reassign_or_takeover",
        "TIMEOUT": "shrink_scope_or_takeover",
        "CHANNEL_ERROR": "probe_channel_before_reassign",
        "BLOCKED": "classify_blocker_and_route_capability",
    }[task.status]
    return [
        _single_issue(
            ctx,
            DueIssueSpec(
                severity,
                f"status_{task.status.lower()}",
                f"任务状态为 {task.status}，需要父代理确认原因，不能当作完成。",
                action,
            ),
        )
    ]


# LLM: _verified_repair_child is part of this module's structured runtime path; keep callers and tests aligned before changing it.
# 函数用途: 完成本模块中的转换、校验或状态整理，供相邻流程继续使用。
def _verified_repair_child(manager, task, repair_kind: str):
    target_refs = _normalized_ref_set(getattr(task, "artifact_refs", []) or [])
    for candidate in manager.list_runs():
        attrs = getattr(candidate, "attributes", {}) or {}
        if attrs.get("repair_kind") != repair_kind:
            continue
        if attrs.get("repair_source_run_id") != task.id:
            continue
        if candidate.status != "DONE" or candidate.verification_status != "VERIFIED":
            continue
        candidate_refs = _normalized_ref_set(attrs.get("target_artifact_refs", []) or [])
        if target_refs and candidate_refs and target_refs != candidate_refs:
            continue
        return candidate
    return None


# LLM: _normalized_ref_set is part of this module's structured runtime path; keep callers and tests aligned before changing it.
# 函数用途: 完成本模块中的转换、校验或状态整理，供相邻流程继续使用。
def _normalized_ref_set(values: object) -> set[str]:
    refs: set[str] = set()
    for item in values if isinstance(values, list) else []:
        text = str(item or "").strip()
        if text:
            refs.add(str(Path(text).resolve(strict=False)))
    return refs


# LLM: _check_no_progress_fuse_issues lifts recovery-strategy fuse decisions into due-check reports.
# 函数用途: 当同一个 run 连续恢复无进展达到阈值时，明确生成 no-progress fuse，而不是继续当普通失败重试。
def _check_no_progress_fuse_issues(ctx: DueInspectionContext, attempt_limit: int):
    """Check whether repeated recovery attempts must stop automatic retry."""
    if attempt_limit <= 0:
        return []
    strategy = build_subagent_recovery_strategy(
        SubagentRecoveryStrategyRequest(
            task=ctx.task,
            now=0.0,
            no_progress_attempt_limit=attempt_limit,
        )
    )
    if not strategy.no_progress_fuse:
        return []
    refs = [strategy.packet_ref, *strategy.fallback_refs, *strategy.takeover_refs]
    return [
        _single_issue(
            ctx,
            DueIssueSpec(
                "P0",
                "no_progress_fuse",
                (
                    f"任务已连续尝试 {ctx.task.runner_attempts} 次，达到无进展熔断阈值 {attempt_limit}；"
                    "停止自动重试和扩容，改为汇总 refs 后等待父级/用户决策。"
                ),
                "stop_no_progress_and_escalate",
                related_refs=list(dict.fromkeys(ref for ref in refs if ref)),
            ),
        )
    ]


# LLM: _check_leadership_recovery_issues lifts dead coordinator decisions above generic timeout handling.
# 函数用途: 带 child_ids 的 coordinator/leader 失联时，优先生成领导权恢复问题，避免普通 takeover 新建空 run。
def _check_leadership_recovery_issues(ctx: DueInspectionContext, attempt_limit: int):
    """Check whether a dead coordinator should hand off its child subtree."""
    strategy = build_subagent_recovery_strategy(
        SubagentRecoveryStrategyRequest(
            task=ctx.task,
            now=0.0,
            no_progress_attempt_limit=attempt_limit,
        )
    )
    if not strategy.leadership_recovery:
        return []
    child_refs = [f"child_run:{run_id}" for run_id in strategy.child_run_ids]
    refs = [*child_refs, *strategy.fallback_refs, *strategy.takeover_refs]
    return [
        _single_issue(
            ctx,
            DueIssueSpec(
                "P0",
                "coordinator_needs_leadership_recovery",
                (
                    f"协调节点状态为 {ctx.task.status}，旗下仍有 {len(strategy.child_run_ids)} 个子任务；"
                    "必须先把子树交给新 leader，不能按普通 worker 新建 takeover run。"
                ),
                "recover_coordinator_leadership",
                related_refs=list(dict.fromkeys(ref for ref in refs if ref)),
            ),
        )
    ]


# LLM: _check_channel_broken_issues 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 函数用途: 校验通道brokenissues需要的输入和状态，不满足时把错误明确反馈给调用方；关键副作用: 主要返回判断或抛出明确异常，调用方依赖布尔语义稳定。
def _check_channel_broken_issues(ctx: DueInspectionContext):
    """Check for channel broken issues."""
    if ctx.task.channel_status != "BROKEN":
        return []
    return [
        _single_issue(
            ctx,
            DueIssueSpec(
                "P0",
                "channel_broken",
                "最近一次通道检查为 BROKEN，优先修复 runtime / workdir / JSON 现场。",
                "run_channel_probe_and_fix_runtime",
            ),
        )
    ]


# LLM: _check_channel_degraded_issues 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 函数用途: 校验通道degradedissues需要的输入和状态，不满足时把错误明确反馈给调用方；关键副作用: 主要返回判断或抛出明确异常，调用方依赖布尔语义稳定。
def _check_channel_degraded_issues(ctx: DueInspectionContext):
    """Check for channel degraded issues."""
    if ctx.task.channel_status != "DEGRADED":
        return []
    return [
        _single_issue(
            ctx,
            DueIssueSpec(
                "P1",
                "channel_degraded",
                "最近一次通道检查为 DEGRADED，建议先修复弱项再继续派工。",
                "inspect_channel_probe_evidence",
            ),
        )
    ]


# LLM: _check_probe_missing_issues 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 函数用途: 校验probemissingissues需要的输入和状态，不满足时把错误明确反馈给调用方；关键副作用: 主要返回判断或抛出明确异常，调用方依赖布尔语义稳定。
def _check_probe_missing_issues(ctx: DueInspectionContext):
    """Check for missing channel probe evidence."""
    if ctx.task.status != "CHANNEL_ERROR" or ctx.task.last_probe_at:
        return []
    return [
        _single_issue(
            ctx,
            DueIssueSpec(
                "P0",
                "channel_probe_missing",
                "任务状态为 CHANNEL_ERROR，但还没有 probe 证据。",
                "run_channel_probe",
            ),
        )
    ]


# LLM: _check_done_evidence_issues 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 函数用途: 校验done证据issues需要的输入和状态，不满足时把错误明确反馈给调用方；关键副作用: 主要返回判断或抛出明确异常，调用方依赖布尔语义稳定。
def _check_done_evidence_issues(ctx: DueInspectionContext, min_evidence):
    """Check for DONE task with insufficient evidence."""
    task = ctx.task
    if task.status != "DONE" or min_evidence <= 0 or len(task.evidence) >= min_evidence:
        return []
    return [
        _single_issue(
            ctx,
            DueIssueSpec(
                "P0",
                "fake_done_risk",
                f"DONE 任务只有 {len(task.evidence)} 条证据，少于配置要求的 {min_evidence} 条。",
                "require_evidence_or_reopen",
            ),
        )
    ]


# LLM: _check_done_verification_issues 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 函数用途: 校验doneverificationissues需要的输入和状态，不满足时把错误明确反馈给调用方；关键副作用: 主要返回判断或抛出明确异常，调用方依赖布尔语义稳定。
def _check_done_verification_issues(ctx: DueInspectionContext):
    """Check for DONE task without verification."""
    if ctx.task.status != "DONE" or ctx.task.verification_status == "VERIFIED":
        return []
    return [
        _single_issue(
            ctx,
            DueIssueSpec(
                "P1",
                "unverified_done",
                "任务已标记 DONE，但 verification_status 还不是 VERIFIED。",
                "run_acceptance_or_assign_reviewer",
            ),
        )
    ]


# LLM: _check_parent_acceptance_repair_issues_with_manager promotes failed parent tests into deterministic repair work.
# 函数用途: 父级验收已 REJECT 时生成修复动作；如果修复子任务已通过，则先收口父任务。
def _check_parent_acceptance_repair_issues_with_manager(ctx: DueInspectionContext, manager):
    if not _parent_acceptance_rejected(ctx.task):
        return []
    if _is_parent_acceptance_repair_task(ctx.task):
        return [
            _single_issue(
                ctx,
                DueIssueSpec(
                    "P1",
                    "parent_acceptance_repair_failed",
                    "parent acceptance repair 任务仍未修好；应创建 takeover run 继承 refs 继续，不能再创建同类 repair child。",
                    "takeover_or_reassign",
                ),
            )
        ]
    if repair := _verified_repair_child(manager, ctx.task, "parent_acceptance"):
        return [
            _single_issue(
                ctx,
                DueIssueSpec(
                    "P1",
                    "parent_acceptance_repair_completed",
                    "父级验收修复子任务已通过验收；应把父任务收口为已完成，避免重复创建验收修复子任务。",
                    "close_parent_from_verified_repair_child",
                    related_refs=[repair.task_dir, repair.output_json],
                ),
            )
        ]
    return _parent_acceptance_repair_issue_specs(ctx)


# LLM: _parent_acceptance_repair_issue_specs is part of this module's structured runtime path; keep callers and tests aligned before changing it.
# 函数用途: 完成本模块中的转换、校验或状态整理，供相邻流程继续使用。
def _parent_acceptance_repair_issue_specs(ctx: DueInspectionContext):
    return [
        _single_issue(
            ctx,
            DueIssueSpec(
                "P1",
                "parent_acceptance_test_failed",
                "父级真实验收已拒绝该任务；应创建 scoped repair worker 读取 acceptance/test refs 修复被点名问题。",
                "create_repair_child_from_parent_acceptance_refs",
            ),
        )
    ]


# LLM: _parent_acceptance_rejected stays local to due-check to avoid importing agent_core during service init.
# 函数用途: 只读取 acceptance_review.json 的机器决策字段，避免从自然语言状态推断。
def _parent_acceptance_rejected(task) -> bool:
    path = Path(getattr(task, "reports_dir", "") or "") / "acceptance_review.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(payload, dict) and str(payload.get("decision") or "").upper() == "REJECT"


# LLM: _check_capability_request_issues 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 函数用途: 校验能力请求issues需要的输入和状态，不满足时把错误明确反馈给调用方；关键副作用: 可能触发网络输入输出或消费流式响应，需保留错误传播语义。
def _check_capability_request_issues(ctx: DueInspectionContext):
    """Check for open capability request issues."""
    if not ctx.open_request_count:
        return []
    return [
        _single_issue(
            ctx,
            DueIssueSpec(
                "P1",
                "open_capability_request",
                f"存在 {ctx.open_request_count} 条未处理能力请求。",
                "route_capability_request",
            ),
        )
    ]


# LLM: _check_capability_gap_issues 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 函数用途: 校验能力缺口issues需要的输入和状态，不满足时把错误明确反馈给调用方；关键副作用: 主要返回判断或抛出明确异常，调用方依赖布尔语义稳定。
def _check_capability_gap_issues(ctx: DueInspectionContext):
    """Check for open capability gap issues."""
    if not ctx.open_gap_count:
        return []
    return [
        _single_issue(
            ctx,
            DueIssueSpec(
                "P2",
                "open_capability_gap",
                f"存在 {ctx.open_gap_count} 条未关闭能力缺口。",
                "triage_gap_for_learning_or_tooling",
            ),
        )
    ]


# LLM: inspect_single_task_due stays bundle-first so new predicates do not grow the public signature.
# 函数用途: 处理单个任务的到期巡检，串起所有 predicate 并返回 refs-only issue 列表。
def inspect_single_task_due(request: InspectTaskDueRequest):
    """Inspect a single task for due issues. Returns a list of issues."""
    task = request.task
    open_request_count = sum(1 for item in task.capability_requests if item.status == "OPEN")
    open_gap_count = sum(1 for item in task.capability_gaps if item.status == "OPEN")
    ctx = DueInspectionContext(
        task=task,
        task_index=request.task_index,
        risk_flags=request.risk_flags_builder(task, open_request_count, open_gap_count),
        open_request_count=open_request_count,
        open_gap_count=open_gap_count,
        age_seconds=max(0.0, request.settings.now - (task.created_at or request.settings.now)),
        stale_seconds=max(0.0, request.settings.now - (task.heartbeat_at or task.updated_at or request.settings.now)),
    )
    validation = request.manager.validate_work_order(task.id)
    issues = []
    issues.extend(_check_work_order_issues(ctx, validation))
    issues.extend(_check_parent_acceptance_repair_issues_with_manager(ctx, request.manager))
    no_progress_issues = _check_no_progress_fuse_issues(ctx, request.settings.no_progress_attempt_limit)
    leadership_issues = (
        []
        if no_progress_issues
        else _check_leadership_recovery_issues(ctx, request.settings.no_progress_attempt_limit)
    )
    issues.extend(no_progress_issues or leadership_issues or _check_status_issues(ctx, request.manager))
    issues.extend(check_parent_timeout_child_issues(ctx))
    issues.extend(_check_channel_broken_issues(ctx))
    issues.extend(_check_channel_degraded_issues(ctx))
    issues.extend(_check_probe_missing_issues(ctx))
    issues.extend(_check_done_evidence_issues(ctx, request.settings.min_evidence))
    issues.extend(_check_done_verification_issues(ctx))
    issues.extend(_check_capability_request_issues(ctx))
    issues.extend(_check_capability_gap_issues(ctx))
    issues.extend(check_coordinator_heartbeat_issues(ctx, request.settings.heartbeat_timeout))
    issues.extend(check_heartbeat_timeout_issues(ctx, request.settings.heartbeat_timeout))
    issues.extend(check_run_timeout_issues(ctx, request.settings.run_timeout))
    return issues
