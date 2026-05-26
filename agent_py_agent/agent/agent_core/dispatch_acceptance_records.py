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
from ..subagents.parent_acceptance_followup_control import ParentAcceptanceFollowUpControlOptions
from ..subagents.rendering import render_acceptance_review_markdown
from ..subagents.reports import AcceptanceReviewRecord, AcceptanceReviewReport
from ..subagents.services.dispatch_params import DispatchRecordParams
from ..subagents.services.indexing_params import IndexReportParams
from .dispatch_acceptance_alignment import (
    align_acceptance_record_with_parent_tests,
    parent_tests_require_rescue,
)
from .dispatch_acceptance_refresh import (
    DispatchAcceptanceRefreshRequest,
    refresh_acceptance_after_parent_tests,
)
from .dispatch_acceptance_stored_record import stored_acceptance_record
from .dispatch_record_params import AcceptanceRecordParams
from .dispatch_test_failure_summary import acceptance_test_failure_payload


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
        refreshed = _apply_acceptance_followup_if_ready(params, refreshed, policy_summary)
        refreshed = _align_acceptance_record_with_parent_tests(params, refreshed, policy_summary)
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
        execute_tests=params.execute_acceptance_tests,
    )
    run_ids = _acceptance_run_ids(params)
    if params.apply and not params.execute_acceptance_tests:
        return params.agent.subagents.write_acceptance_review_report(run_ids, options=options)
    if params.execute_acceptance_tests:
        return params.agent.subagents.write_acceptance_review_report(run_ids, options=options)
    return params.agent.subagents.review_acceptances(run_ids, options=options)


# LLM: _acceptance_run_ids scopes finalize reviews to the dispatch target when provided.
# 函数用途: 显式 run_ids/root/parent 调度只验收本轮范围内等待验收的任务，避免旧工作区任务污染报告。
def _acceptance_run_ids(params: AcceptanceRecordParams) -> list[str] | None:
    if params.include_run_ids:
        return [str(item) for item in params.include_run_ids if str(item or "").strip()]
    if not (params.root_id or params.parent_run_id or params.exclude_run_ids):
        return None
    excluded = {str(item) for item in (params.exclude_run_ids or []) if str(item or "").strip()}
    selected: list[str] = []
    for task in params.agent.subagents.list_runs():
        if task.id in excluded:
            continue
        if params.parent_run_id and task.parent_id != params.parent_run_id:
            continue
        if params.root_id and task.root_id != params.root_id:
            continue
        if task.status == "AWAITING_ACCEPTANCE" or task.verification_status == "NEEDS_ACCEPTANCE":
            selected.append(task.id)
    return selected


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
        dry_run=all(record.dry_run for record in records),
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


# LLM: _align_acceptance_record_with_parent_tests makes dispatch facts single-source after real tests fail.
# 函数用途: 显式父级验收 tests 失败时，把 acceptance record、aggregate 和单 run 审计都改成拒绝修复口径。
def _align_acceptance_record_with_parent_tests(
    params: AcceptanceRecordParams,
    record: AcceptanceReviewRecord,
    policy_summary: dict[str, object],
) -> AcceptanceReviewRecord:
    if not parent_tests_require_rescue(policy_summary):
        return record
    aligned = align_acceptance_record_with_parent_tests(record, policy_summary)
    if params.apply and params.execute_acceptance_tests:
        _write_single_acceptance_record_files(params.agent, aligned)
    return aligned


# LLM: _write_single_acceptance_record_files mirrors the manager writer after post-test normalization.
# 函数用途: 单 run acceptance_review.json/Markdown 也使用测试失败后的拒绝口径，避免和 dispatch 报告不一致。
def _write_single_acceptance_record_files(agent: Any, record: AcceptanceReviewRecord) -> None:
    writer = getattr(agent.subagents, "_write_acceptance_record_files", None)
    indexer = getattr(agent.subagents, "_index_acceptance_review", None)
    if callable(writer):
        writer(record)
    if callable(indexer):
        indexer(record)


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
    payload = _parent_acceptance_policy_payload(task, policy, execution)
    if options and options.execute_tests:
        payload.update(_existing_test_followup_payload(task))
    return payload


# LLM: _apply_acceptance_followup_if_ready mutates only when runner-context dispatch requested apply.
# 函数用途: 父节点 dispatch 已明确 apply 且 tests 通过时，执行受控 follow-up apply 并返回落盘后的验收记录。
def _apply_acceptance_followup_if_ready(
    params: AcceptanceRecordParams,
    record: AcceptanceReviewRecord,
    policy_summary: dict[str, object],
) -> AcceptanceReviewRecord:
    if not _should_apply_acceptance_followup(params, record, policy_summary):
        return record
    result = params.agent.subagents.apply_parent_acceptance_followup(
        record.run_id,
        options=ParentAcceptanceFollowUpControlOptions(
            apply=True,
            reviewer=params.reviewer,
            note=params.note,
        ),
    )
    policy_summary.update(_followup_control_summary(result))
    return stored_acceptance_record(params.agent, record.run_id) or record


# LLM: _should_apply_acceptance_followup keeps top-level dispatch and failed tests non-mutating.
# 函数用途: 只允许显式 auto_apply、测试已执行且 0 失败、follow-up 指向 apply_acceptance 的场景落状态。
def _should_apply_acceptance_followup(
    params: AcceptanceRecordParams,
    record: AcceptanceReviewRecord,
    policy_summary: dict[str, object],
) -> bool:
    return bool(
        params.apply
        and params.execute_acceptance_tests
        and params.auto_apply_acceptance_followup
        and record.ok
        and policy_summary.get("parent_acceptance_auto_execution_executed") is True
        and policy_summary.get("parent_acceptance_auto_execution_test_failed") == 0
        and policy_summary.get("parent_acceptance_followup_status") == "ready_for_manual_apply"
        and policy_summary.get("parent_acceptance_followup_action") == "apply_acceptance"
    )


# LLM: _followup_control_summary replaces ready-for-manual fields with the applied control outcome.
# 函数用途: dispatch record 输出 follow-up 的最终控制结果，便于 coordinator 看到 child 已闭环。
def _followup_control_summary(result: Any) -> dict[str, object]:
    return {
        "parent_acceptance_followup_status": str(getattr(result, "status", "") or ""),
        "parent_acceptance_followup_action": str(getattr(result, "action", "") or ""),
        "parent_acceptance_followup_command": str(getattr(result, "recommended_command", "") or ""),
        "parent_acceptance_followup_reason": str(getattr(result, "message", "") or ""),
    }


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
        **acceptance_test_failure_payload(execution.test_execution_ref),
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


# LLM: _existing_test_followup_payload surfaces tests run by the explicit acceptance review path.
# 函数用途: 当 dispatch 已通过 execute_tests 写出 test_execution/follow-up 时，记录中仍显示 tests_executed。
def _existing_test_followup_payload(task: Any) -> dict[str, object]:
    test_ref = Path(task.reports_dir) / "test_execution.json"
    followup_ref = Path(task.reports_dir) / "parent_acceptance_auto_followup.json"
    if not test_ref.exists():
        return {}
    test_payload = json.loads(test_ref.read_text(encoding="utf-8"))
    followup = _read_followup_payload(followup_ref)
    return {
        "parent_acceptance_auto_execution_status": "tests_executed",
        "parent_acceptance_auto_execution_allowed": True,
        "parent_acceptance_auto_execution_executed": True,
        "parent_acceptance_auto_execution_guard_status": "manual_confirmed",
        "parent_acceptance_auto_execution_blocked_by": [],
        "parent_acceptance_auto_execution_test_ref": str(test_ref),
        "parent_acceptance_auto_execution_test_total": int(test_payload.get("total_tests") or 0),
        "parent_acceptance_auto_execution_test_failed": int(test_payload.get("failed") or 0),
        **acceptance_test_failure_payload(test_ref),
        "parent_acceptance_followup_ref": str(followup_ref) if followup_ref.exists() else "",
        "parent_acceptance_followup_status": str(followup.get("status") or ""),
        "parent_acceptance_followup_action": str(followup.get("action") or ""),
        "parent_acceptance_followup_command": str(followup.get("command") or ""),
        "parent_acceptance_followup_reason": str(followup.get("reason") or ""),
    }


# LLM: _read_followup_payload tolerates missing or malformed follow-up audit files.
# 函数用途: 读取 parent_acceptance_auto_followup.json 的 followup 子结构；失败返回空字典。
def _read_followup_payload(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    followup = payload.get("followup") if isinstance(payload, dict) else {}
    return followup if isinstance(followup, dict) else {}
