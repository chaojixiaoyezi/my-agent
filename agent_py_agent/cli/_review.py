# LLM: CLI surface module; keep argparse/Typer wiring, stdout text, and service-call boundaries stable.
# 模块用途: 提供命令行入口或辅助函数，把用户命令转换成 agent 服务调用。


from __future__ import annotations

import json

from ..agent.subagents.acceptance_review_service import AcceptanceReviewOptions
from ..agent.subagents.patch import PatchApplyOptions, PatchReviewOptions
from ._acceptance_plan import cmd_subagents_acceptance_plan
from ._review_tests import cmd_subagents_tests as _cmd_subagents_tests
from .common import make_agent
from .models import SubagentsAcceptanceOptions, SubagentsPatchOptions


# LLM: cmd_subagents_acceptance 属于CLI 命令层；改行为前先对齐调用方和快照/单测。
# 函数用途: CLI 子命令入口，连接 argparse 参数、服务调用和最终退出码。
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
            execute_tests=_acceptance_execute_tests(agent, options),
            test_timeout_seconds=_acceptance_test_timeout(agent, options),
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


# LLM: _subagents_acceptance_options 属于CLI 命令层；改行为前先对齐调用方和快照/单测。
# 函数用途: 生成结构化字段，保持 CLI 输出、报告和测试读取口径一致。
def _subagents_acceptance_options(args) -> SubagentsAcceptanceOptions:
    return SubagentsAcceptanceOptions(
        run_ids=getattr(args, "run_id", None) or None,
        apply=bool(getattr(args, "apply", False)),
        reviewer=getattr(args, "reviewer", None),
        note=getattr(args, "note", None) or "",
        limit=int(getattr(args, "limit", 0) or 0),
        execute_tests=getattr(args, "execute_tests", None),
        test_timeout=getattr(args, "test_timeout", None),
    )


# LLM: _acceptance_execute_tests applies explicit CLI choice before config defaults.
# 函数用途: 解析真实测试执行开关，保持默认验收路径保守且可由命令行覆盖。
def _acceptance_execute_tests(agent, options: SubagentsAcceptanceOptions) -> bool:
    if options.execute_tests is not None:
        return bool(options.execute_tests)
    return bool(getattr(agent.config, "acceptance_execute_tests", False))


# LLM: _acceptance_test_timeout applies CLI override before the configured timeout.
# 函数用途: 解析真实测试执行超时，供 acceptance 和 test report 使用同一数值。
def _acceptance_test_timeout(agent, options: SubagentsAcceptanceOptions) -> float:
    if options.test_timeout is not None:
        return float(options.test_timeout)
    return float(getattr(agent.config, "acceptance_test_timeout_seconds", 120) or 120)


# LLM: cmd_subagents_tests keeps the historical _review.make_agent patch point for tests.
# 函数用途: 兼容旧导入路径，把 subagents-tests 实现转发到拆分后的 _review_tests 模块。
def cmd_subagents_tests(args) -> int:
    return _cmd_subagents_tests(args, make_agent_fn=make_agent)


# LLM: _print_patch_report 属于CLI 命令层；改行为前先对齐调用方和快照/单测。
# 函数用途: 整理 CLI 或报告展示文本，输出文案变化会影响快照断言。
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


# LLM: cmd_subagents_patches 属于CLI 命令层；改行为前先对齐调用方和快照/单测。
# 函数用途: CLI 子命令入口，连接 argparse 参数、服务调用和最终退出码。
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


# LLM: _subagents_patch_options 属于CLI 命令层；改行为前先对齐调用方和快照/单测。
# 函数用途: 生成结构化字段，保持 CLI 输出、报告和测试读取口径一致。
def _subagents_patch_options(args) -> SubagentsPatchOptions:
    return SubagentsPatchOptions(
        action=getattr(args, "patch_action", None) or "review_dry_run",
        run_ids=getattr(args, "run_id", None) or None,
        reviewer=getattr(args, "reviewer", None),
        note=getattr(args, "note", None) or "",
        limit=int(getattr(args, "limit", 0) or 0),
    )


# LLM: _print_review_report 属于CLI 命令层；改行为前先对齐调用方和快照/单测。
# 函数用途: 整理 CLI 或报告展示文本，输出文案变化会影响快照断言。
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
