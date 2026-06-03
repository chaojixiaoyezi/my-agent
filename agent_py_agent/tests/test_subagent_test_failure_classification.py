"""Tests for parent-owned classification of subagent test execution outcomes."""

from agent_py_agent.agent.subagents.execution import (
    TestExecutionRecord,
    TestExecutionReport,
    TestExecutionReportOptions,
    write_test_execution_report,
)
from agent_py_agent.agent.subagents.test_failure_classification import (
    TestFailureClassificationRequest,
    classify_test_execution_report,
    write_test_failure_classification,
)


def test_classify_zero_tests_as_runner_output_gap():
    """LLM: Zero executed tests should become a runner-output gap, not an acceptance success."""

    report = _report([])

    result = classify_test_execution_report(
        TestFailureClassificationRequest(report=report, output={"artifacts": []})
    )

    assert result.overall_status == "blocked"
    assert result.primary_category == "runner_output_missing_tests"
    assert result.recommended_action == "repair"
    assert result.counts["runner_output_missing_tests"] == 1


def test_classify_mixed_execution_failures():
    """LLM: Failed records should be grouped by failure shape for rescue routing."""

    report = _report([
        TestExecutionRecord(
            test_name="contract",
            command="python3 -m pytest tests/test_checkout.py -q",
            executed=True,
            exit_code=1,
            stdout="E       AssertionError: expected total to update",
            validation_method="command",
            validation_result={"ok": False},
        ),
        TestExecutionRecord(
            test_name="unsafe",
            command="cd app && python3 -m pytest",
            executed=False,
            error="测试命令包含高风险 shell 字符",
            validation_method="command",
            validation_result={"ok": False, "reason": "command_rejected"},
        ),
        TestExecutionRecord(
            test_name="syntax",
            command="python3 -m pytest tests/test_nav.py -q",
            executed=True,
            exit_code=2,
            stderr="SyntaxError: invalid syntax",
            validation_method="command",
            validation_result={"ok": False},
        ),
    ])

    result = classify_test_execution_report(TestFailureClassificationRequest(report=report))

    assert result.overall_status == "failed"
    assert result.primary_category == "assertion_failure"
    assert result.recommended_action == "repair"
    assert result.counts["assertion_failure"] == 1
    assert result.counts["command_rejected"] == 1
    assert result.counts["syntax_or_import_error"] == 1
    assert [item.category for item in result.items] == [
        "assertion_failure",
        "command_rejected",
        "syntax_or_import_error",
    ]


def test_write_test_failure_classification_refs_only(tmp_path):
    """LLM: Classification report should persist compact refs without copying stdout/stderr bodies."""

    report = write_test_execution_report(
        tmp_path,
        [
            TestExecutionRecord(
                test_name="checkout",
                command="python3 -m pytest tests/test_checkout.py -q",
                executed=True,
                exit_code=1,
                stdout="x" * 2000 + "AssertionError: cart total mismatch",
                validation_method="command",
                validation_result={"ok": False},
            )
        ],
        options=TestExecutionReportOptions(workspace_root=tmp_path),
    )

    path = write_test_failure_classification(
        tmp_path,
        TestFailureClassificationRequest(report=report),
    )

    text = path.read_text(encoding="utf-8")
    assert "assertion_failure" in text
    assert "cart total mismatch" in text
    assert "x" * 500 not in text


def _report(records: list[TestExecutionRecord]) -> TestExecutionReport:
    return TestExecutionReport.from_dict({
        "executed_at": "2026-05-09T10:00:00Z",
        "workspace_root": "/tmp/workspace",
        "timeout_seconds": 120,
        "records": [record.to_dict() for record in records],
    })
