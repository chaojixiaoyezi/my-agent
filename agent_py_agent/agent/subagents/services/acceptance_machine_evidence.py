# LLM: Machine-generated acceptance evidence helpers; keep them read-only and report-based.
# 模块用途: 把父级真实测试报告作为可追溯机器证据读取，不信任 runner 自述。

from __future__ import annotations

"""Helpers for machine-generated acceptance evidence."""

from pathlib import Path
from typing import TYPE_CHECKING

from ..execution_report import TestExecutionReport, load_test_execution_report

if TYPE_CHECKING:
    from ..models import SubAgentTask


# LLM: passed_test_execution_report treats parent-run tests as machine evidence only when every record passed.
# 函数用途: 读取 reports/test_execution.json；只有真实执行且全部通过时才返回报告，否则返回 None。
def passed_test_execution_report(task: SubAgentTask) -> TestExecutionReport | None:
    """Return the passed parent test report, if one exists."""

    report = _load_test_execution_report(task)
    if report is None:
        return None
    if report.total_tests <= 0 or report.failed != 0:
        return None
    return report


# LLM: _load_test_execution_report is intentionally tolerant so acceptance can keep using older tasks.
# 函数用途: 宽容读取真实测试报告；文件不存在或格式异常时返回 None，不影响旧验收路径。
def _load_test_execution_report(task: SubAgentTask) -> TestExecutionReport | None:
    path = Path(task.reports_dir) / "test_execution.json"
    if not path.exists():
        return None
    try:
        return load_test_execution_report(path)
    except (OSError, ValueError, TypeError):
        return None
