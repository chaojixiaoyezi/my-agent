# LLM: CLI implementation for subagents-tests; keep real test execution refs-first.
# 模块用途: 查看或显式重跑 subagent 测试报告，和 patch review CLI 拆开维护。

from __future__ import annotations

import json
from pathlib import Path

from ..agent.subagents.execution_executor import TestExecutor
from ..agent.subagents.execution_report import (
    TestExecutionReportOptions,
    load_test_execution_report,
    write_test_execution_report,
)
from ..agent.subagents.execution_test_items import TestItemPreparationRequest, prepare_test_items
from ..agent.subagents.test_failure_classification import (
    TestFailureClassificationRequest,
    classify_test_execution_report,
    write_test_failure_classification,
)
from .common import make_agent
from .models import SubagentsTestsOptions


# LLM: cmd_subagents_tests is refs-first; it prints stored reports unless users explicitly re-run.
# 函数用途: 查看或显式重跑单个 subagent 的真实测试执行记录，不自动展开大 artifact 正文。
def cmd_subagents_tests(args, make_agent_fn=make_agent) -> int:
    agent = make_agent_fn(args)
    options = _subagents_tests_options(args)
    task = agent.subagents.load(options.run_id)
    report_path = Path(task.reports_dir) / "test_execution.json"

    if options.re_run:
        _write_subagents_tests_report(agent, task, options)

    if not report_path.exists():
        print("SUBAGENT TESTS")
        print(f"run_id={options.run_id} re_run={options.re_run}")
        print("暂时没有 test_execution.json；如需真实执行请加 --re-run。")
        return 1

    report = load_test_execution_report(report_path)
    classification = _write_subagents_test_classification(task, report)
    _print_subagents_tests_report(options, report, classification)
    return _subagents_tests_exit_code(report)


# LLM: _subagents_tests_options normalizes argparse names used by tests and CLI wiring.
# 函数用途: 收拢 subagents-tests 参数，给查看和重跑流程使用同一份结构。
def _subagents_tests_options(args) -> SubagentsTestsOptions:
    return SubagentsTestsOptions(
        run_id=str(getattr(args, "run_id", "") or ""),
        re_run=bool(getattr(args, "re_run", False)),
        timeout=float(getattr(args, "timeout", 120.0) or 120.0),
    )


# LLM: _write_subagents_tests_report re-runs declared subagent tests directly.
# 函数用途: 从 output.json 读取 tests 并执行，写入 test_execution.json/md。
def _write_subagents_tests_report(agent, task, options: SubagentsTestsOptions):
    workspace_root = _subagents_tests_workspace(agent, task)
    output = _read_task_output(task)
    tests = _subagents_tests_items(task, output)
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


# LLM: _subagents_tests_items returns worker-declared tests only.
# 函数用途: 构造真实执行测试项；不再合并结果检查包。
def _subagents_tests_items(task, output: dict[str, object]) -> list[dict]:
    return [item for item in output.get("tests", []) if isinstance(item, dict)]


# LLM: _write_subagents_test_classification keeps test reports paired with compact repair-routing facts.
# 函数用途: 根据当前 test_execution 写分类报告；只读 output.json 元数据，不复制大 stdout/stderr。
def _write_subagents_test_classification(task, report):
    request = TestFailureClassificationRequest(report=report, output=_read_task_output(task))
    classification = classify_test_execution_report(request)
    write_test_failure_classification(task.reports_dir, request)
    return classification


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
def _print_subagents_tests_report(options: SubagentsTestsOptions, report, classification) -> None:
    print("SUBAGENT TESTS")
    print(f"run_id={options.run_id} re_run={options.re_run}")
    print(
        f"total={report.total_tests} executed={report.executed} "
        f"passed={report.passed} failed={report.failed}"
    )
    print(
        f"classification={classification.primary_category} "
        f"recommended_action={classification.recommended_action}"
    )
    for record in report.records:
        status = "PASS" if record.passed else "FAIL"
        print(
            f"- [{status}] {record.test_name} method={record.validation_method} "
            f"exit_code={record.exit_code}"
        )
    print(f"\nJSON: {report.json_path}")
    print(f"Markdown: {report.markdown_path}")
    print(f"Classification: {Path(report.json_path).with_name('test_failure_classification.json')}")
