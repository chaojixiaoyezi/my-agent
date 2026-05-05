"""Review commands: acceptance review, patch review/apply."""

from __future__ import annotations

import json

from .common import make_agent


def cmd_subagents_acceptance(args) -> int:
    """验收等待验收的 subagent，默认 dry-run。"""

    agent = make_agent(args)
    report = agent.subagents.write_acceptance_review_report(
        run_ids=args.run_id or None,
        apply=args.apply,
        reviewer=args.reviewer,
        note=args.note or "",
        limit=args.limit,
    )
    mode = "apply" if args.apply else "dry-run"
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
    if args.apply:
        print(f"审计日志: {agent.subagents.workspace / 'subagent_acceptance_log.jsonl'}")
        print(f"审计日志: {agent.subagents.workspace / 'ACCEPTANCE_REVIEW_LOG.md'}")
    return 0


def cmd_subagents_patches(args) -> int:
    """审核或 apply runner 输出里的 patch 记录。"""

    agent = make_agent(args)
    if args.patch_action == "apply_dry_run":
        report = agent.subagents.write_patch_apply_report(
            run_ids=args.run_id or None,
            apply=False,
            applier=args.reviewer,
            note=args.note or "",
            limit=args.limit,
        )
        mode = "apply-dry-run"
        print("SUBAGENT PATCH APPLY")
        print(f"mode={mode} total_records={report.summary.get('total', 0)}")
        print("summary=" + json.dumps(report.summary, ensure_ascii=False, sort_keys=True))
        if not report.records:
            print("暂时没有 patch 需要 apply。")
        for record in report.records:
            status = "OK" if record.ok else "FAIL"
            print(
                f"- [{status}] {record.run_id} decision={record.decision} "
                f"patches={record.patch_count} applied={record.applied_count} "
                f"blocked={record.blocked_count} rollback={record.rollback_performed} :: {record.message}"
            )
        print(f"\n已写入: {agent.subagents.workspace / 'subagent_patch_apply_report.json'}")
        print(f"已写入: {agent.subagents.workspace / 'SUBAGENT_PATCH_APPLY.md'}")
        return 0

    if args.patch_action == "apply":
        report = agent.subagents.write_patch_apply_report(
            run_ids=args.run_id or None,
            apply=True,
            applier=args.reviewer,
            note=args.note or "",
            limit=args.limit,
        )
        mode = "apply"
        print("SUBAGENT PATCH APPLY")
        print(f"mode={mode} total_records={report.summary.get('total', 0)}")
        print("summary=" + json.dumps(report.summary, ensure_ascii=False, sort_keys=True))
        if not report.records:
            print("暂时没有 patch 需要 apply。")
        for record in report.records:
            status = "OK" if record.ok else "FAIL"
            print(
                f"- [{status}] {record.run_id} decision={record.decision} "
                f"patches={record.patch_count} applied={record.applied_count} "
                f"blocked={record.blocked_count} rollback={record.rollback_performed} :: {record.message}"
            )
        print(f"\n已写入: {agent.subagents.workspace / 'subagent_patch_apply_report.json'}")
        print(f"已写入: {agent.subagents.workspace / 'SUBAGENT_PATCH_APPLY.md'}")
        print(f"审计日志: {agent.subagents.workspace / 'subagent_patch_apply_log.jsonl'}")
        print(f"审计日志: {agent.subagents.workspace / 'PATCH_APPLY_LOG.md'}")
        return 0

    report = agent.subagents.write_patch_review_report(
        run_ids=args.run_id or None,
        apply=args.patch_action == "review_apply",
        reviewer=args.reviewer,
        note=args.note or "",
        limit=args.limit,
    )
    mode = "review-apply" if args.patch_action == "review_apply" else "review-dry-run"
    print("SUBAGENT PATCH REVIEW")
    print(f"mode={mode} total_records={report.summary.get('total', 0)}")
    print("summary=" + json.dumps(report.summary, ensure_ascii=False, sort_keys=True))
    if not report.records:
        print("暂时没有 patch 需要审核。")
    for record in report.records:
        status = "OK" if record.ok else "FAIL"
        print(
            f"- [{status}] {record.run_id} decision={record.decision} "
            f"patches={record.patch_count} approved={record.approved_count} "
            f"blocked={record.blocked_count} applied={record.applied} :: {record.message}"
        )
    print(f"\n已写入: {agent.subagents.workspace / 'subagent_patch_review_report.json'}")
    print(f"已写入: {agent.subagents.workspace / 'SUBAGENT_PATCH_REVIEW.md'}")
    if args.patch_action == "review_apply":
        print(f"审计日志: {agent.subagents.workspace / 'subagent_patch_review_log.jsonl'}")
        print(f"审计日志: {agent.subagents.workspace / 'PATCH_REVIEW_LOG.md'}")
    return 0
