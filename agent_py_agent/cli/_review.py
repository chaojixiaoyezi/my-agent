

from __future__ import annotations

import json

from ..agent.subagents.patch import PatchApplyOptions, PatchReviewOptions
from ._review_tests import cmd_subagents_tests as _cmd_subagents_tests
from .common import make_agent
from .models import SubagentsPatchOptions


def cmd_subagents_tests(args) -> int:
    return _cmd_subagents_tests(args, make_agent_fn=make_agent)


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
    options = _subagents_patch_options(args, agent=agent)

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


def _subagents_patch_options(args, *, agent=None) -> SubagentsPatchOptions:
    return SubagentsPatchOptions(
        action=getattr(args, "patch_action", None) or "review_dry_run",
        run_ids=getattr(args, "run_id", None) or None,
        reviewer=getattr(args, "reviewer", None),
        note=getattr(args, "note", None) or "",
        limit=_subagent_config_int(agent, args, "limit", "subagent_cli_default_limit"),
    )


def _subagent_config_int(agent, args, arg_name: str, config_name: str) -> int:
    value = getattr(args, arg_name, None)
    if value is not None:
        return int(value)
    return int(getattr(getattr(agent, "config", None), config_name, 0) or 0)


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
