
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from ..contracts.recovery_actions import RecoveryAction
from .execution.report import TestExecutionReport


@dataclass(frozen=True)
class TestFailureClassificationRequest:
    """Input bundle for classifying a test execution report."""

    __test__: ClassVar[bool] = False

    report: TestExecutionReport
    output: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class TestFailureClassificationItem:
    """Classification for one test execution record."""

    __test__: ClassVar[bool] = False

    test_name: str
    category: str
    recommended_action: str
    command: str = ""
    summary: str = ""


@dataclass(frozen=True)
class TestFailureClassificationReport:
    """Compact classification report over a test execution report."""

    __test__: ClassVar[bool] = False

    overall_status: str
    primary_category: str
    recommended_action: str
    counts: dict[str, int]
    items: list[TestFailureClassificationItem] = field(default_factory=list)
    test_execution_ref: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["schema"] = "test_failure_classification.v1"
        return payload


def classify_test_execution_report(
    request: TestFailureClassificationRequest,
) -> TestFailureClassificationReport:
    """Classify a test execution report for parent repair/rescue routing."""

    report = request.report
    if report.total_tests <= 0:
        return _zero_tests_report(request)
    items = [_classify_record(record) for record in report.records]
    counts = _counts(items)
    primary = _primary_category(counts)
    return TestFailureClassificationReport(
        overall_status="passed" if report.failed == 0 else "failed",
        primary_category=primary,
        recommended_action=_recommended_action(primary),
        counts=counts,
        items=items,
        test_execution_ref=str(report.json_path),
    )


def write_test_failure_classification(
    output_dir: str | Path,
    request: TestFailureClassificationRequest,
) -> Path:
    """Write a compact classification report and return its path."""

    report = classify_test_execution_report(request)
    path = Path(output_dir) / "test_failure_classification.json"
    path.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _zero_tests_report(request: TestFailureClassificationRequest) -> TestFailureClassificationReport:
    return TestFailureClassificationReport(
        overall_status="blocked",
        primary_category="runner_output_missing_tests",
        recommended_action=RecoveryAction.REPAIR.value,
        counts={"runner_output_missing_tests": 1},
        test_execution_ref=str(request.report.json_path),
    )


def _classify_record(record) -> TestFailureClassificationItem:
    category = _record_category(record)
    return TestFailureClassificationItem(
        test_name=record.test_name,
        category=category,
        recommended_action=_recommended_action(category),
        command=record.command,
        summary=_short_summary(record),
    )


def _record_category(record) -> str:
    if record.passed:
        return "passed"
    if not record.executed:
        return _not_executed_category(record)
    return _executed_failure_category(record)


def _not_executed_category(record) -> str:
    reason = _validation_reason(record)
    if reason == "timeout":
        return "timeout"
    return "command_rejected" if reason == "command_rejected" else "not_executed"


def _executed_failure_category(record) -> str:
    reason = _validation_reason(record)
    if reason == "timeout":
        return "timeout"
    method = str(record.validation_method or "").strip()
    if method == "command":
        return "command_failed"
    if method:
        return "validation_failed"
    return "runtime_failure"


def _validation_reason(record) -> str:
    if not isinstance(record.validation_result, dict):
        return ""
    return str(record.validation_result.get("reason") or "").strip()


def _primary_category(counts: dict[str, int]) -> str:
    for category in (
        "timeout",
        "command_failed",
        "validation_failed",
        "runtime_failure",
        "command_rejected",
        "not_executed",
        "runner_output_missing_tests",
    ):
        if counts.get(category):
            return category
    return "passed"


def _recommended_action(category: str) -> str:
    if category == "passed":
        return RecoveryAction.CLOSEOUT.value
    if category in {"command_failed", "validation_failed", "runtime_failure"}:
        return RecoveryAction.REPAIR.value
    if category in {"command_rejected", "not_executed", "runner_output_missing_tests"}:
        return RecoveryAction.REPAIR.value
    if category == "timeout":
        return RecoveryAction.TAKEOVER.value
    return RecoveryAction.MANUAL_REVIEW.value


def _counts(items: list[TestFailureClassificationItem]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        counts[item.category] = counts.get(item.category, 0) + 1
    return counts


def _short_summary(record) -> str:
    text = _record_text(record)
    if not text:
        return ""
    return text[-240:]


def _record_text(record) -> str:
    return "\n".join(str(item or "") for item in (record.error, record.stdout, record.stderr))
