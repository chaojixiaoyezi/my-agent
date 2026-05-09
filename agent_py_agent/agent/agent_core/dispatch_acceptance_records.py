# LLM: Dispatch acceptance record builder owns parent-test execution summaries and refreshed review records.
# 模块用途: 构建 dispatch 的 acceptance 记录，显式 tests 后刷新验收结论，避免主 dispatch_service 膨胀。

from __future__ import annotations

import json
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ..subagents.acceptance_review_service import AcceptanceReviewOptions
from ..subagents.parent_acceptance_auto_execution import ParentAcceptanceAutoExecutionOptions
from ..subagents.rendering import render_acceptance_review_markdown
from ..subagents.reports import AcceptanceReviewRecord, AcceptanceReviewReport
from ..subagents.services.dispatch_params import DispatchRecordParams
from ..subagents.services.indexing_params import IndexReportParams
from .dispatch_acceptance_refresh import (
    DispatchAcceptanceRefreshRequest,
    refresh_acceptance_after_parent_tests,
)
from .dispatch_record_params import AcceptanceRecordParams


# LLM: make_acceptance_records builds dispatch-visible acceptance records after optional parent tests.
# 函数用途: 构建验收阶段调度记录；显式执行 tests 后会复算 dry-run acceptance，不直接 apply 任务。
def make_acceptance_records(params: AcceptanceRecordParams):
    agent = params.agent
    records = []
    refreshed_records = []
    acceptance_report = _acceptance_report(params)
    for item in acceptance_report.records:
        policy_summary = _parent_acceptance_policy_summary(
            agent,
            item.run_id,
            options=_auto_execution_options(params.execute_acceptance_tests),
        )
        refreshed = refresh_acceptance_after_parent_tests(
            DispatchAcceptanceRefreshRequest(agent, item, params, policy_summary)
        )
        refreshed_records.append(refreshed)
        records.append(agent.subagents.make_dispatch_record(
            params=_acceptance_dispatch_record_params(refreshed, policy_summary)
        ))
    _write_refreshed_report_if_needed(params, refreshed_records)
    return records


# LLM: _acceptance_report preserves existing apply semantics before optional test execution summaries.
# 函数用途: 生成初始 acceptance report；execute_acceptance_tests 场景不 apply，只等待 follow-up gate。
def _acceptance_report(params: AcceptanceRecordParams):
    options = AcceptanceReviewOptions(
        apply=params.apply and not params.execute_acceptance_tests,
        reviewer=params.reviewer,
        note=params.note,
        limit=params.limit,
    )
    if params.apply and not params.execute_acceptance_tests:
        return params.agent.subagents.write_acceptance_review_report(options=options)
    return params.agent.subagents.review_acceptances(options=options)


# LLM: _write_refreshed_report_if_needed keeps aggregate acceptance files aligned after explicit tests.
# 函数用途: dispatch --apply 且显式跑 tests 后，用测试后的 dry-run 记录重写 aggregate 验收报告。
def _write_refreshed_report_if_needed(
    params: AcceptanceRecordParams,
    records: list[AcceptanceReviewRecord],
) -> None:
    if not (params.apply and params.execute_acceptance_tests and records):
        return
    report = AcceptanceReviewReport(
        generated_at=time.time(),
        dry_run=True,
        summary=_acceptance_report_summary(records),
        records=records,
    )
    _write_acceptance_report_files(params.agent, report)


# LLM: _write_acceptance_report_files mirrors the manager report files without mutating task state.
# 函数用途: 写测试后 aggregate acceptance JSON/Markdown 和索引；不追加 apply log，不修改 run 状态。
def _write_acceptance_report_files(agent: Any, report: AcceptanceReviewReport) -> None:
    workspace = agent.subagents.workspace
    (workspace / "subagent_acceptance_report.json").write_text(
        json.dumps(asdict(report), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (workspace / "SUBAGENT_ACCEPTANCE.md").write_text(
        render_acceptance_review_markdown(report),
        encoding="utf-8",
    )
    indexer = getattr(agent.subagents, "_index_report", None)
    if callable(indexer):
        indexer(IndexReportParams(
            "subagent_acceptance_report",
            "latest",
            "Subagent acceptance report",
            report,
            "subagent_acceptance_report_written",
        ))


# LLM: _acceptance_report_summary matches manager acceptance summaries for refreshed dry-run reports.
# 函数用途: 根据测试后 records 重新统计 ACCEPT/REJECT、ok/failed 和 dry_run/applied 计数。
def _acceptance_report_summary(records: list[AcceptanceReviewRecord]) -> dict[str, int]:
    summary: dict[str, int] = {"total": len(records)}
    for record in records:
        summary[record.decision] = summary.get(record.decision, 0) + 1
        status_key = "ok" if record.ok else "failed"
        mode_key = "dry_run" if record.dry_run else "applied"
        summary[status_key] = summary.get(status_key, 0) + 1
        summary[mode_key] = summary.get(mode_key, 0) + 1
    return summary


# LLM: _acceptance_dispatch_record_params maps a review record plus policy refs into a dispatch record bundle.
# 函数用途: 将刷新后的验收结果和 parent acceptance auto 摘要合并成 DispatchRecordParams。
def _acceptance_dispatch_record_params(item: Any, policy_summary: dict[str, object]) -> DispatchRecordParams:
    return DispatchRecordParams(
        step="acceptance",
        action=item.decision.lower(),
        run_id=item.run_id,
        dry_run=item.dry_run,
        applied=item.applied,
        ok=item.ok,
        message=item.message,
        before_status=item.before_status,
        after_status=item.after_status,
        before_verification_status=item.before_verification_status,
        after_verification_status=item.after_verification_status,
        evidence_paths=item.evidence_paths,
        **policy_summary,
    )


# LLM: _parent_acceptance_policy_summary attaches policy/execution refs and runs tests only with explicit options.
# 函数用途: 为 acceptance 调度记录生成自动验收策略和执行摘要；默认只写审计，显式 options 才跑 tests。
def _parent_acceptance_policy_summary(
    agent: Any,
    run_id: str,
    *,
    options: ParentAcceptanceAutoExecutionOptions | None = None,
) -> dict[str, object]:
    if not run_id:
        return {}
    task = agent.subagents.load(run_id)
    policy = agent.subagents.plan_parent_acceptance_auto_policy(run_id)
    execution = agent.subagents.plan_parent_acceptance_auto_execution(run_id, options=options)
    return _parent_acceptance_policy_payload(task, policy, execution)


# LLM: _parent_acceptance_policy_payload keeps the summary field list out of the orchestration function.
# 函数用途: 把 policy 和 execution 结果压缩成 dispatch record 的 refs-only 字段。
def _parent_acceptance_policy_payload(task: Any, policy: Any, execution: Any) -> dict[str, object]:
    policy_ref = Path(task.reports_dir) / "parent_acceptance_auto_policy.json"
    execution_ref = Path(task.reports_dir) / "parent_acceptance_auto_execution.json"
    return {
        "parent_acceptance_policy_ref": str(policy_ref),
        "parent_acceptance_policy_decision": policy.decision,
        "parent_acceptance_policy_action": policy.action,
        "parent_acceptance_policy_would_execute": bool(policy.would_execute),
        "parent_acceptance_policy_executed": bool(policy.executed),
        "parent_acceptance_policy_execution_mode": policy.execution_mode,
        "parent_acceptance_policy_automatic_execution_allowed": bool(policy.automatic_execution_allowed),
        "parent_acceptance_policy_recommended_command": policy.recommended_command,
        "parent_acceptance_policy_preflight_status": policy.preflight_status,
        "parent_acceptance_policy_ready_for_automatic_execution": bool(policy.ready_for_automatic_execution),
        "parent_acceptance_policy_preflight_blockers": list(policy.preflight_blockers),
        "parent_acceptance_auto_execution_ref": str(execution_ref),
        "parent_acceptance_auto_execution_status": execution.status,
        "parent_acceptance_auto_execution_allowed": bool(execution.execution_allowed),
        "parent_acceptance_auto_execution_executed": bool(execution.executed),
        "parent_acceptance_auto_execution_guard_status": execution.guard_status,
        "parent_acceptance_auto_execution_blocked_by": list(execution.blocked_by),
        "parent_acceptance_auto_execution_test_ref": execution.test_execution_ref,
        "parent_acceptance_auto_execution_test_total": execution.test_total,
        "parent_acceptance_auto_execution_test_failed": execution.test_failed,
        "parent_acceptance_followup_ref": execution.followup_ref,
        "parent_acceptance_followup_status": execution.followup_status,
        "parent_acceptance_followup_action": execution.followup_action,
        "parent_acceptance_followup_command": execution.followup_command,
        "parent_acceptance_followup_reason": execution.followup_reason,
    }


# LLM: _auto_execution_options converts the dispatch flag into the guarded parent acceptance options bundle.
# 函数用途: 只在 dispatch/watch 显式确认时创建 execute_tests 选项包；默认保持 dry-run facade。
def _auto_execution_options(execute_tests: bool) -> ParentAcceptanceAutoExecutionOptions | None:
    if not execute_tests:
        return None
    return ParentAcceptanceAutoExecutionOptions(execute_tests=True)
