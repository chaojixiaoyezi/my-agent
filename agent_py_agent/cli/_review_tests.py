
from __future__ import annotations

from pathlib import Path

from ..agent.common.json_io import JsonObjectReadReport, read_json_object_report
from ..agent.subagents.execution import (
    TestExecutionReportOptions,
    TestExecutor,
    TestItemPreparationRequest,
    load_test_execution_report,
    prepare_test_items,
    write_test_execution_report,
)
from ..agent.subagents.test_failure_classification import (
    TestFailureClassificationRequest,
    classify_test_execution_report,
    write_test_failure_classification,
)
from .common import make_agent
from .models import SubagentsTestsOptions


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
    output_report = _read_task_output_report(task)
    classification = _write_subagents_test_classification(task, report, output_report.payload)
    _print_subagents_tests_report(options, report, classification, output_load_error=output_report.load_error)
    return _subagents_tests_exit_code(report)


def _subagents_tests_options(args) -> SubagentsTestsOptions:
    return SubagentsTestsOptions(
        run_id=str(getattr(args, "run_id", "") or ""),
        re_run=bool(getattr(args, "re_run", False)),
        timeout=float(getattr(args, "timeout", 120.0) or 120.0),
    )


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


def _subagents_tests_items(task, output: dict[str, object]) -> list[dict]:
    return [item for item in output.get("tests", []) if isinstance(item, dict)]


def _write_subagents_test_classification(task, report, output: dict[str, object] | None = None):
    request = TestFailureClassificationRequest(report=report, output=output if output is not None else _read_task_output(task))
    classification = classify_test_execution_report(request)
    write_test_failure_classification(task.reports_dir, request)
    return classification


def _output_declares_tests(task) -> bool:
    return bool([item for item in _read_task_output(task).get("tests", []) if isinstance(item, dict)])


def _subagents_tests_exit_code(report) -> int:
    if report.total_tests <= 0 or report.failed > 0:
        return 1
    return 0


def _subagents_tests_workspace(agent, task) -> Path:
    workspace_root = getattr(agent.subagents, "workspace_root", None)
    if workspace_root:
        return Path(workspace_root)
    output_json = Path(getattr(task, "output_json", "") or ".").resolve()
    return output_json.parent


def _read_task_output(task) -> dict[str, object]:
    return _read_task_output_report(task).payload


def _read_task_output_report(task) -> JsonObjectReadReport:
    raw_path = getattr(task, "output_json", "") or ""
    if not raw_path:
        return JsonObjectReadReport({})
    output_json = Path(raw_path)
    return read_json_object_report(output_json, context="cli.subagents_tests.output_json")


def _print_subagents_tests_report(
    options: SubagentsTestsOptions,
    report,
    classification,
    *,
    output_load_error: dict[str, object] | None = None,
) -> None:
    print("SUBAGENT TESTS")
    print(f"run_id={options.run_id} re_run={options.re_run}")
    if output_load_error:
        print(
            "output_load_error="
            f"{output_load_error.get('context')} "
            f"path={output_load_error.get('path')} "
            f"category={output_load_error.get('category')}"
        )
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
