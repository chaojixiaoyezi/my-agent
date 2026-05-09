# LLM: Auto-execution report helpers keep the executor facade small and bundle-based.
# 模块用途: 写入父级验收自动执行后的 test report 和 follow-up，不负责执行命令或改 task。
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from .execution_records import TestExecutionRecord
from .execution_report import (
    TestExecutionReport,
    TestExecutionReportOptions,
    write_test_execution_report,
)
from .models import SubAgentTask
from .parent_acceptance_auto_followup import (
    ParentAcceptanceAutoFollowUp,
    build_parent_acceptance_auto_followup,
)
from .test_failure_classification import (
    TestFailureClassificationRequest,
    write_test_failure_classification,
)


# LLM: ConfirmedTestReportRequest bundles report-write inputs so helper signatures stay stable.
# 类用途: 保存显式测试执行后的报告写入上下文；它不执行测试、不修改 task 状态。
@dataclass(frozen=True)
class ConfirmedTestReportRequest:
    """Bundle for writing a confirmed parent acceptance test report."""

    __test__: ClassVar[bool] = False

    task: SubAgentTask
    request: Any
    workspace_root: Path
    records: list[TestExecutionRecord]


# LLM: write_confirmed_test_report keeps execution separate from result construction.
# 函数用途: 把真实测试执行记录写成 test_execution 报告；只写报告，不改 task 状态。
def write_confirmed_test_report(params: ConfirmedTestReportRequest) -> TestExecutionReport:
    report = write_test_execution_report(
        params.task.reports_dir,
        params.records,
        options=TestExecutionReportOptions(
            workspace_root=params.workspace_root,
            timeout_seconds=params.request.timeout_seconds,
        ),
    )
    write_test_failure_classification(
        params.task.reports_dir,
        TestFailureClassificationRequest(report=report),
    )
    return report


# LLM: write_execution_followup produces the post-test handoff without applying the task.
# 函数用途: 写入测试后的 follow-up 审计，给上级判断下一步人工 apply 或救援。
def write_execution_followup(
    task: SubAgentTask,
    workspace_root: Path,
    report: TestExecutionReport,
) -> ParentAcceptanceAutoFollowUp:
    return build_parent_acceptance_auto_followup(
        task,
        workspace_root=workspace_root,
        execution_ref=str(Path(task.reports_dir) / "parent_acceptance_auto_execution.json"),
        test_execution_ref=str(report.json_path),
    )


# LLM: followup_path centralizes the task-local auto follow-up audit filename.
# 函数用途: 返回测试执行后的 follow-up 审计路径；调用方只保存 ref，不读取文件正文。
def followup_path(task: SubAgentTask) -> Path:
    return Path(task.reports_dir) / "parent_acceptance_auto_followup.json"
