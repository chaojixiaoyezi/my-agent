
from __future__ import annotations

import json

from ..agent.subagents.acceptance_review_service import AcceptanceReviewOptions
from ..agent.subagents.patch import PatchApplyOptions, PatchReviewOptions
from .common import make_agent
from .models import SubagentsAcceptanceOptions, SubagentsPatchOptions


def cmd_subagents_acceptance(args) -> int:

    agent = make_agent(args)
    options = _subagents_acceptance_options(args)
    report = agent.subagents.write_acceptance_review_report(
        run_ids=options.run_ids,
        options=AcceptanceReviewOptions(
            apply=options.apply,
            reviewer=options.reviewer or "parent",
            note=options.note,
            limit=options.limit,
        ),
    )
    mode = "apply" if options.apply else "dry-run"
    print("SUBAGENT ACCEPTANCE")
    print(f"mode={mode} total_records={report.summary.get('total', 0)}")
    print("summary=" + json.dumps(report.summary, ensure_ascii=False, sort_keys=True))
    if not report.records:
        print("暂时没有等待验收的 subagent。")
    for record in report.records:
        status = "OK" if record.ok else "FAIL"
        print(
            f"- [{status}] {record.run_id} decision={record.decision} "
            f"applied={record.applied} {record.before_status}/{record.before_verification_status}"
            f"->{record.after_status}/{record.after_verification_status} :: {record.message}"
        )
    print(f"\n已写入: {agent.subagents.workspace / 'subagent_acceptance_report.json'}")
    print(f"已写入: {agent.subagents.workspace / 'SUBAGENT_ACCEPTANCE.md'}")
    if options.apply:
        print(f"审计日志: {agent.subagents.workspace / 'subagent_acceptance_log.jsonl'}")
        print(f"审计日志: {agent.subagents.workspace / 'ACCEPTANCE_REVIEW_LOG.md'}")
    return 0


def _subagents_acceptance_options(args) -> SubagentsAcceptanceOptions:
    # LLM: Keep argparse at the boundary; downstream helpers get a typed CLI bundle.
    return SubagentsAcceptanceOptions(
        run_ids=getattr(args, "run_id", None) or None,
        apply=bool(getattr(args, "apply", False)),
        reviewer=getattr(args, "reviewer", None),
        note=getattr(args, "note", None) or "",
        limit=int(getattr(args, "limit", 0) or 0),
    )


def _print_patch_report(report, mode: str, workspace, include_audit: bool = False) -> None:
    print(f"SUBAGENT PATCH {mode.upper()}")
    print(f"mode={mode} total_records={report.summary.get('total', 0)}")
    print("summary=" + json.dumps(report.summary, ensure_ascii=False, sort_keys=True))
    if not report.records:
        print("暂时没有 patch 需要 apply。" if "apply" in mode else "暂时没有 patch 需要审核。")
        return
    for record in report.records:
        status = "OK" if record.ok else "FAIL"
        print(
            f"- [{status}] {record.run_id} decision={record.decision} "
            f"patches={record.patch_count} applied={record.applied_count} "
            f"blocked={record.blocked_count} rollback={record.rollback_performed} :: {record.message}"
        )
    print(f"\n已写入: {workspace / f'subagent_patch_{mode}_report.json'}")
    print(f"已写入: {workspace / f'SUBAGENT_PATCH_{mode.upper()}.md'}")
    if include_audit:
        print(f"审计日志: {workspace / f'subagent_patch_{mode}_log.jsonl'}")
        print(f"审计日志: {workspace / f'PATCH_{mode.upper()}_LOG.md'}")


def cmd_subagents_patches(args) -> int:

    agent = make_agent(args)
    workspace = agent.subagents.workspace
    options = _subagents_patch_options(args)

    if options.action == "apply_dry_run":
        report = agent.subagents.write_patch_apply_report(
            run_ids=options.run_ids,
            options=PatchApplyOptions(
                apply=False,
                applier=options.reviewer or "parent",
                note=options.note,
                limit=options.limit,
            ),
        )
        _print_patch_report(report, "apply-dry-run", workspace, include_audit=False)
        return 0

    if options.action == "apply":
        report = agent.subagents.write_patch_apply_report(
            run_ids=options.run_ids,
            options=PatchApplyOptions(
                apply=True,
                applier=options.reviewer or "parent",
                note=options.note,
                limit=options.limit,
            ),
        )
        _print_patch_report(report, "apply", workspace, include_audit=True)
        return 0

    report = agent.subagents.write_patch_review_report(
        run_ids=options.run_ids,
        options=PatchReviewOptions(
            apply=options.action == "review_apply",
            reviewer=options.reviewer or "parent",
            note=options.note,
            limit=options.limit,
        ),
    )
    mode = "review-apply" if options.action == "review_apply" else "review-dry-run"
    _print_review_report(report, mode, workspace, options.action == "review_apply")
    return 0


def _subagents_patch_options(args) -> SubagentsPatchOptions:
    # LLM: Normalize legacy parser constants before branching into review/apply modes.
    return SubagentsPatchOptions(
        action=getattr(args, "patch_action", None) or "review_dry_run",
        run_ids=getattr(args, "run_id", None) or None,
        reviewer=getattr(args, "reviewer", None),
        note=getattr(args, "note", None) or "",
        limit=int(getattr(args, "limit", 0) or 0),
    )


def _print_review_report(report, mode: str, workspace, include_audit: bool) -> None:
    print("SUBAGENT PATCH REVIEW")
    print(f"mode={mode} total_records={report.summary.get('total', 0)}")
    print("summary=" + json.dumps(report.summary, ensure_ascii=False, sort_keys=True))
    if not report.records:
        print("暂时没有 patch 需要审核。")
        return
    for record in report.records:
        status = "OK" if record.ok else "FAIL"
        print(
            f"- [{status}] {record.run_id} decision={record.decision} "
            f"patches={record.patch_count} approved={record.approved_count} "
            f"blocked={record.blocked_count} applied={record.applied} :: {record.message}"
        )
    print(f"\n已写入: {workspace / 'subagent_patch_review_report.json'}")
    print(f"已写入: {workspace / 'SUBAGENT_PATCH_REVIEW.md'}")
    if include_audit:
        print(f"审计日志: {workspace / 'subagent_patch_review_log.jsonl'}")
        print(f"审计日志: {workspace / 'PATCH_REVIEW_LOG.md'}")
