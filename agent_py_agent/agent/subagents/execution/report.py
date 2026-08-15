
from __future__ import annotations

"""Persistence helpers for test execution reports."""

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, ClassVar

from .records import TestExecutionRecord


@dataclass(frozen=True)
class TestExecutionReportOptions:
    """Options for persisted test execution reports."""

    __test__: ClassVar[bool] = False

    workspace_root: str | Path = ""
    timeout_seconds: float = 0
    executed_at: str = ""


@dataclass
class TestExecutionReport:
    """Summary and file refs for a persisted test execution report."""

    __test__: ClassVar[bool] = False

    executed_at: str
    workspace_root: str
    timeout_seconds: float
    total_tests: int
    executed: int
    passed: int
    failed: int
    records: list[TestExecutionRecord] = field(default_factory=list)
    json_path: Path = Path()
    markdown_path: Path = Path()

    def to_dict(self) -> dict[str, Any]:
        """Serialize report payload for JSON storage."""

        return {
            "executed_at": self.executed_at,
            "workspace_root": self.workspace_root,
            "timeout_seconds": self.timeout_seconds,
            "total_tests": self.total_tests,
            "executed": self.executed,
            "passed": self.passed,
            "failed": self.failed,
            "records": [record.to_dict() for record in self.records],
        }

    @classmethod
    def from_dict(
        cls,
        data: dict[str, Any] | None,
        *,
        json_path: str | Path = "",
        markdown_path: str | Path = "",
    ) -> TestExecutionReport:
        """Create a report from a stored JSON dictionary."""

        payload = data if isinstance(data, dict) else {}
        records = [
            TestExecutionRecord.from_dict(item)
            for item in payload.get("records", [])
            if isinstance(item, dict)
        ]
        return cls(
            executed_at=str(payload.get("executed_at") or ""),
            workspace_root=str(payload.get("workspace_root") or ""),
            timeout_seconds=float(payload.get("timeout_seconds") or 0),
            total_tests=int(payload.get("total_tests") or len(records)),
            executed=int(payload.get("executed") or sum(1 for record in records if record.executed)),
            passed=int(payload.get("passed") or sum(1 for record in records if record.passed)),
            failed=int(payload.get("failed") or sum(1 for record in records if not record.passed)),
            records=records,
            json_path=Path(json_path) if json_path else Path(),
            markdown_path=Path(markdown_path) if markdown_path else Path(),
        )


def write_test_execution_report(
    output_dir: str | Path,
    records: list[TestExecutionRecord],
    *,
    options: TestExecutionReportOptions | None = None,
) -> TestExecutionReport:
    """Persist test execution records as JSON and Markdown."""

    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    opts = options or TestExecutionReportOptions()
    report = _build_report(
        records,
        options=opts,
        json_path=root / "test_execution.json",
        markdown_path=root / "test_execution.md",
    )
    report.json_path.write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    report.markdown_path.write_text(render_test_execution_markdown(report), encoding="utf-8")
    return report


def load_test_execution_report(path: str | Path) -> TestExecutionReport:
    """Load a persisted test execution report from JSON."""

    json_path = Path(path)
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    return TestExecutionReport.from_dict(
        payload,
        json_path=json_path,
        markdown_path=json_path.with_suffix(".md"),
    )


def render_test_execution_markdown(report: TestExecutionReport) -> str:
    """Render a human-readable test execution report."""

    lines = [
        "# 测试执行报告",
        "",
        f"- 执行时间: {report.executed_at or 'unknown'}",
        f"- Workspace: {report.workspace_root or 'unknown'}",
        f"- 总数: {report.total_tests}",
        f"- 已执行: {report.executed}",
        f"- 通过: {report.passed}",
        f"- 失败: {report.failed}",
        "",
        "## Records",
    ]
    for record in report.records:
        status = "PASS" if record.passed else "FAIL"
        detail = record.error or f"method={record.validation_method}"
        lines.append(f"- {status} {record.test_name or 'unknown'}: {detail}")
    lines.append("")
    return "\n".join(lines)


def _build_report(
    records: list[TestExecutionRecord],
    *,
    options: TestExecutionReportOptions,
    json_path: Path,
    markdown_path: Path,
) -> TestExecutionReport:
    """Build a report object from records and output paths."""

    total = len(records)
    passed = sum(1 for record in records if record.passed)
    return TestExecutionReport(
        executed_at=options.executed_at or _utc_now_iso(),
        workspace_root=str(options.workspace_root or ""),
        timeout_seconds=float(options.timeout_seconds or 0),
        total_tests=total,
        executed=sum(1 for record in records if record.executed),
        passed=passed,
        failed=total - passed,
        records=list(records),
        json_path=json_path,
        markdown_path=markdown_path,
    )


def _utc_now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
