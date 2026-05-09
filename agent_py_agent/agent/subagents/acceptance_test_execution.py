# LLM: Explicit acceptance test execution bridge; writes execution reports and returns review findings.
# 模块用途: 在父级验收显式开启时执行 runner tests，生成 test_execution 报告和阻断 finding。

from __future__ import annotations

"""Bridge real test execution into acceptance findings."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .execution_executor import TestExecutor
from .execution_report import TestExecutionReportOptions, write_test_execution_report
from .execution_test_items import TestItemPreparationRequest, prepare_test_items
from .parsing import _dict_list
from .reports import AcceptanceReviewFinding


# LLM: AcceptanceTestExecutionRequest bundles opt-in acceptance execution context without growing helper signatures.
# 类用途: 保存真实测试执行接入所需上下文；它只服务当前验收轮次，不修改 task 状态。
@dataclass(frozen=True)
class AcceptanceTestExecutionRequest:
    """Bundle for opt-in acceptance test execution."""

    manager: Any
    task: Any
    output: dict[str, object]
    options: Any
    created_at: float


# LLM: build_acceptance_test_execution_findings is opt-in and leaves task state unchanged.
# 函数用途: 显式执行 output.json 里的 tests，写 test_execution.json/md，并返回可阻断验收的 findings。
def build_acceptance_test_execution_findings(
    request: AcceptanceTestExecutionRequest,
) -> list[AcceptanceReviewFinding]:
    """Execute runner tests and return acceptance findings."""

    manager = request.manager
    task = request.task
    output = request.output
    options = request.options
    tests = _dict_list(output.get("tests", []))
    workspace_root = _acceptance_test_workspace(manager)
    # LLM: Parent tests run from an inferred artifact cwd when runner emitted relative commands.
    tests = prepare_test_items(
        TestItemPreparationRequest(
            tests=tests,
            output=output,
            workspace_root=workspace_root,
        )
    )
    executor = TestExecutor(workspace_root, timeout_seconds=options.test_timeout_seconds)
    records = [executor.execute(test) for test in tests]
    report = write_test_execution_report(
        task.reports_dir,
        records,
        options=TestExecutionReportOptions(
            workspace_root=workspace_root,
            timeout_seconds=options.test_timeout_seconds,
        ),
    )
    return [
        _execution_recorded_finding(report, bool(tests), request.created_at),
        _execution_passed_finding(report, request.created_at),
    ]


# LLM: _acceptance_test_workspace prefers the explicit manager root so hidden runtime folders do not become test cwd.
# 函数用途: 推断真实测试命令的工作目录；新旧 subagent workspace 下都应返回用户项目根目录。
def _acceptance_test_workspace(manager: Any) -> Path:
    """Return the workspace root for acceptance test execution."""

    workspace_root = getattr(manager, "workspace_root", None)
    if workspace_root:
        return Path(workspace_root).resolve()
    workspace = Path(manager.workspace)
    if workspace.name == "subagents" and workspace.parent.name == ".my_agent":
        return workspace.parent.parent
    return workspace.parent if workspace.parent != workspace else workspace


# LLM: _execution_recorded_finding proves that explicit test execution produced a machine-readable report.
# 函数用途: 生成“真实测试记录已产生”的 finding；没有 tests 时显式失败，避免空验收。
def _execution_recorded_finding(report: Any, had_tests: bool, created_at: float) -> AcceptanceReviewFinding:
    """Return finding for test execution report creation."""

    return AcceptanceReviewFinding(
        name="test_execution_recorded",
        ok=had_tests,
        severity="P1",
        message=(
            f"真实测试执行记录已生成: total={report.total_tests} executed={report.executed}。"
            if had_tests else "显式要求真实测试执行，但 output.json 未提供 tests。"
        ),
        evidence_path=str(report.json_path),
        created_at=created_at,
    )


# LLM: _execution_passed_finding is the blocking gate over real execution records.
# 函数用途: 根据 test_execution.json 汇总判断真实测试是否通过；失败时作为 P0 阻断验收。
def _execution_passed_finding(report: Any, created_at: float) -> AcceptanceReviewFinding:
    """Return finding for real test execution pass/fail state."""

    ok = report.total_tests > 0 and report.failed == 0
    return AcceptanceReviewFinding(
        name="test_execution_passed",
        ok=ok,
        severity="P0",
        message=(
            f"真实执行的 {report.total_tests} 条测试均通过。"
            if ok else f"真实测试执行未通过: total={report.total_tests} failed={report.failed}。"
        ),
        evidence_path=str(report.json_path),
        created_at=created_at,
    )
