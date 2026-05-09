# LLM: CLI surface module; keep argparse/Typer wiring, stdout text, and service-call boundaries stable.
# 模块用途: 提供命令行入口或辅助函数，把用户命令转换成 agent 服务调用。


from __future__ import annotations

import json
from pathlib import Path

from ..agent.subagents.acceptance_review_service import AcceptanceReviewOptions
from ..agent.subagents.execution_executor import TestExecutor
from ..agent.subagents.execution_report import (
    TestExecutionReportOptions,
    load_test_execution_report,
    write_test_execution_report,
)
from ..agent.subagents.execution_test_items import TestItemPreparationRequest, prepare_test_items
from ..agent.subagents.patch import PatchApplyOptions, PatchReviewOptions
from ._acceptance_plan import cmd_subagents_acceptance_plan
from .common import make_agent
from .models import SubagentsAcceptanceOptions, SubagentsPatchOptions, SubagentsTestsOptions


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


# LLM: cmd_subagents_tests is refs-first; it prints stored reports unless users explicitly re-run.
# 函数用途: 查看或显式重跑单个 subagent 的真实测试执行记录，不自动展开大 artifact 正文。
def cmd_subagents_tests(args) -> int:

    agent = make_agent(args)
    options = _subagents_tests_options(args)
    task = agent.subagents.load(options.run_id)
    report_path = Path(task.reports_dir) / "test_execution.json"

    if options.re_run:
        _request_acceptance_test_execution(agent, options)
        if _needs_direct_subagents_tests_report(report_path, task):
            _write_subagents_tests_report(agent, task, options)

    if not report_path.exists():
        print("SUBAGENT TESTS")
        print(f"run_id={options.run_id} re_run={options.re_run}")
        print("暂时没有 test_execution.json；如需真实执行请加 --re-run。")
        return 1

    report = load_test_execution_report(report_path)
    _print_subagents_tests_report(options, report)
    return _subagents_tests_exit_code(report)


# LLM: _subagents_tests_options normalizes argparse names used by tests and CLI wiring.
# 函数用途: 收拢 subagents-tests 参数，给查看和重跑流程使用同一份结构。
def _subagents_tests_options(args) -> SubagentsTestsOptions:
    return SubagentsTestsOptions(
        run_id=str(getattr(args, "run_id", "") or ""),
        re_run=bool(getattr(args, "re_run", False)),
        timeout=float(getattr(args, "timeout", 120.0) or 120.0),
    )


# LLM: _request_acceptance_test_execution keeps this command aligned with acceptance opt-in semantics.
# 函数用途: 调用验收服务的显式真实测试开关；真实服务会写报告，测试替身则可只记录调用。
def _request_acceptance_test_execution(agent, options: SubagentsTestsOptions) -> None:
    agent.subagents.write_acceptance_review_report(
        run_ids=[options.run_id],
        options=AcceptanceReviewOptions(
            execute_tests=True,
            test_timeout_seconds=options.timeout,
        ),
    )


# LLM: _write_subagents_tests_report is a fallback for direct CLI re-run when acceptance did not write a report.
# 函数用途: 从 output.json 读取 tests 并执行，写入 test_execution.json/md。
def _write_subagents_tests_report(agent, task, options: SubagentsTestsOptions):
    workspace_root = _subagents_tests_workspace(agent, task)
    output = _read_task_output(task)
    tests = [item for item in output.get("tests", []) if isinstance(item, dict)]
    tests = prepare_test_items(
        TestItemPreparationRequest(tests=tests, output=output, workspace_root=workspace_root)
    )
    executor = TestExecutor(workspace_root, timeout_seconds=options.timeout)
    records = [executor.execute(test) for test in tests]
    return write_test_execution_report(
        task.reports_dir,
        records,
        options=TestExecutionReportOptions(
            workspace_root=workspace_root,
            timeout_seconds=options.timeout,
        ),
    )


# LLM: _needs_direct_subagents_tests_report prevents empty acceptance side effects from masking declared tests.
# 函数用途: 判断是否需要直接按 output.json 执行 tests；空报告不能覆盖真实声明的测试项。
def _needs_direct_subagents_tests_report(report_path: Path, task) -> bool:
    if not report_path.exists():
        return True
    report = load_test_execution_report(report_path)
    return report.total_tests == 0 and _output_declares_tests(task)


# LLM: _output_declares_tests reads only structured output metadata to decide whether a zero-test report is suspicious.
# 函数用途: 判断 output.json 是否声明了 tests；用于避免“报告存在但没执行任何测试”被当成成功。
def _output_declares_tests(task) -> bool:
    return bool([item for item in _read_task_output(task).get("tests", []) if isinstance(item, dict)])


# LLM: _subagents_tests_exit_code makes the CLI fail when no tests ran or any test failed.
# 函数用途: 根据 test_execution 汇总决定命令退出码，防止空报告或失败报告误报成功。
def _subagents_tests_exit_code(report) -> int:
    if report.total_tests <= 0 or report.failed > 0:
        return 1
    return 0


# LLM: _subagents_tests_workspace prefers the manager project root and falls back to output.json's folder.
# 函数用途: 推断测试执行目录，保证真实命令和文件检查被限制在可解释的 workspace 内。
def _subagents_tests_workspace(agent, task) -> Path:
    workspace_root = getattr(agent.subagents, "workspace_root", None)
    if workspace_root:
        return Path(workspace_root)
    output_json = Path(getattr(task, "output_json", "") or ".").resolve()
    return output_json.parent


# LLM: _read_task_output only reads the structured runner output, not any referenced large artifact body.
# 函数用途: 读取 output.json 里的 tests 数组；失败时返回空结构，由报告保留空执行事实。
def _read_task_output(task) -> dict[str, object]:
    raw_path = getattr(task, "output_json", "") or ""
    if not raw_path:
        return {}
    output_json = Path(raw_path)
    if not output_json.is_file():
        return {}
    payload = json.loads(output_json.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {}


# LLM: _print_subagents_tests_report is intentionally compact so terminal output stays inspectable.
# 函数用途: 打印测试执行摘要和每条记录的结果，不打印 stdout/stderr 全文。
def _print_subagents_tests_report(options: SubagentsTestsOptions, report) -> None:
    print("SUBAGENT TESTS")
    print(f"run_id={options.run_id} re_run={options.re_run}")
    print(
        f"total={report.total_tests} executed={report.executed} "
        f"passed={report.passed} failed={report.failed}"
    )
    for record in report.records:
        status = "PASS" if record.passed else "FAIL"
        print(
            f"- [{status}] {record.test_name} method={record.validation_method} "
            f"exit_code={record.exit_code}"
        )
    print(f"\nJSON: {report.json_path}")
    print(f"Markdown: {report.markdown_path}")


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
