# LLM: Parent acceptance auto-execution facade keeps default dry-run and gates the first manual test execution slice.
# 模块用途: 定义父级验收自动执行器的请求/结果包；默认只写审计，显式确认时只允许跑 tests。
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from .execution_executor import TestExecutor
from .execution_test_items import TestItemPreparationRequest, prepare_test_items
from .models import SubAgentTask
from .parent_acceptance_auto_execution_reports import (
    ConfirmedTestReportRequest,
    followup_path,
    write_confirmed_test_report,
    write_execution_followup,
)
from .parent_acceptance_auto_policy import (
    ParentAcceptanceAutoPolicy,
    build_parent_acceptance_auto_policy,
)
from .parsing import _dict_list
from .static_required_files import required_static_files_for_task, static_site_root_hints_for_task
from .utils import _read_json_object


# LLM: ParentAcceptanceAutoExecutionOptions is the stable bundle for future executor knobs.
# 类用途: 汇总父级验收自动执行的调用选项；新增开关时扩展这个包，避免业务接口继续增加散参数。
@dataclass(frozen=True)
class ParentAcceptanceAutoExecutionOptions:
    """Options bundle for parent acceptance auto-execution planning."""

    __test__: ClassVar[bool] = False

    mode: str = "dry_run"
    execute_tests: bool = False
    timeout_seconds: float = TestExecutor.DEFAULT_TIMEOUT_SECONDS
    reserved: dict[str, Any] = field(default_factory=dict)


# LLM: ParentAcceptanceAutoExecutionRequest is the explicit bundle future executors must consume.
# 类用途: 描述一次父级验收自动执行计划请求；只保存 policy/preflight 摘要和推荐命令，不执行命令。
@dataclass(frozen=True)
class ParentAcceptanceAutoExecutionRequest:
    """Request bundle for a parent acceptance auto-execution plan."""

    __test__: ClassVar[bool] = False

    run_id: str
    mode: str = "dry_run"
    policy_ref: str = ""
    recommended_command: str = ""
    preflight_status: str = "blocked"
    ready_for_manual_execution: bool = False
    ready_for_automatic_execution: bool = False
    preflight_blockers: list[str] = field(default_factory=list)
    manual_confirmed: bool = False
    timeout_seconds: float = TestExecutor.DEFAULT_TIMEOUT_SECONDS
    requested_by: str = "parent_acceptance_auto_policy"
    reserved: dict[str, Any] = field(default_factory=dict)

    # LLM: to_dict keeps request JSON stable for audit files and CLI rendering.
    # 函数用途: 把自动执行请求包转换为 JSON 友好字典；不展开任何引用文件正文。
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# LLM: ParentAcceptanceAutoExecutionResult records dry-run blocks or manual test execution refs without applying acceptance.
# 类用途: 记录自动执行计划结果、硬闸门和测试报告引用；即使跑 tests，也不改 task 状态。
@dataclass(frozen=True)
class ParentAcceptanceAutoExecutionResult:
    """Audit-only result for a parent acceptance auto-execution plan."""

    __test__: ClassVar[bool] = False

    run_id: str
    mode: str
    status: str
    request: ParentAcceptanceAutoExecutionRequest
    execution_allowed: bool = False
    guard_status: str = "blocked"
    guard_reason: str = "auto executor is dry-run only"
    executed: bool = False
    mutates_task_state: bool = False
    command: str = ""
    execution_ref: str = ""
    blocked_by: list[str] = field(default_factory=list)
    safety_boundaries: list[str] = field(default_factory=list)
    test_execution_ref: str = ""
    test_total: int = 0
    test_failed: int = 0
    followup_ref: str = ""
    followup_status: str = ""
    followup_action: str = ""
    followup_command: str = ""
    followup_reason: str = ""
    reserved: dict[str, Any] = field(default_factory=dict)

    # LLM: to_dict keeps nested request output consistent with dataclass serialization.
    # 函数用途: 把自动执行结果转换为 JSON 友好字典；保留 request 子结构和阻断原因。
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# LLM: TestsExecutedResultInput keeps confirmed-run result assembly bundle-shaped.
# 类用途: 保存测试执行结果组装所需的报告和 follow-up，不承载执行逻辑。
@dataclass(frozen=True)
class TestsExecutedResultInput:
    """Bundle for building a confirmed test execution result."""

    __test__: ClassVar[bool] = False

    request: ParentAcceptanceAutoExecutionRequest
    report: Any
    followup: Any
    followup_ref: str


# LLM: build_parent_acceptance_auto_execution plans by default and runs tests only when the options bundle confirms it.
# 函数用途: 生成父级验收自动执行计划并写审计文件；只有 options.execute_tests 为 true 时才跑 tests。
def build_parent_acceptance_auto_execution(
    task: SubAgentTask,
    *,
    workspace_root: str | Path,
    options: ParentAcceptanceAutoExecutionOptions | None = None,
) -> ParentAcceptanceAutoExecutionResult:
    opts = options or ParentAcceptanceAutoExecutionOptions()
    workspace = Path(workspace_root)
    policy = build_parent_acceptance_auto_policy(task, workspace_root=workspace_root)
    policy_ref = Path(task.reports_dir) / "parent_acceptance_auto_policy.json"
    request = _request_from_policy(
        task,
        policy,
        policy_ref=policy_ref,
        options=opts,
    )
    result = (
        _execute_confirmed_tests(task, request, workspace_root=workspace)
        if opts.execute_tests else _result_from_request(request)
    )
    write_parent_acceptance_auto_execution_file(task, result)
    return result


# LLM: write_parent_acceptance_auto_execution_file persists only the executor plan audit.
# 函数用途: 写入 `parent_acceptance_auto_execution.json`；只保存请求/结果和硬边界，不执行命令。
def write_parent_acceptance_auto_execution_file(
    task: SubAgentTask,
    result: ParentAcceptanceAutoExecutionResult,
    *,
    generated_at: float | None = None,
) -> Path:
    path = Path(task.reports_dir) / "parent_acceptance_auto_execution.json"
    payload = {
        "schema": "parent_acceptance_auto_execution.v1",
        "generated_at": generated_at if generated_at is not None else time.time(),
        "dry_run": not result.executed,
        "run_id": task.id,
        "request": result.request.to_dict(),
        "result": result.to_dict(),
        "reserved": {
            "refs_only": True,
            "executes_tests": bool(result.executed),
            "executes_command": bool(result.executed),
            "mutates_task_state": False,
            "future_execute_supported": True,
        },
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return path


# LLM: _request_from_policy narrows policy output into the executor-facing request bundle.
# 函数用途: 从 auto-policy 生成执行器请求包，只复制摘要字段和 refs，不读取引用文件正文。
def _request_from_policy(
    task: SubAgentTask,
    policy: ParentAcceptanceAutoPolicy,
    *,
    policy_ref: Path,
    options: ParentAcceptanceAutoExecutionOptions,
) -> ParentAcceptanceAutoExecutionRequest:
    mode = "manual_confirm_execute_tests" if options.execute_tests else options.mode
    return ParentAcceptanceAutoExecutionRequest(
        run_id=task.id,
        mode=mode,
        policy_ref=str(policy_ref),
        recommended_command=policy.recommended_command,
        preflight_status=policy.preflight_status,
        ready_for_manual_execution=policy.ready_for_manual_execution,
        ready_for_automatic_execution=policy.ready_for_automatic_execution,
        preflight_blockers=list(policy.preflight_blockers),
        manual_confirmed=options.execute_tests,
        timeout_seconds=options.timeout_seconds,
        reserved=dict(options.reserved),
    )


# LLM: _result_from_request records the current closed executor decision.
# 函数用途: 根据请求包生成 dry-run 结果；第一版即使有推荐命令，也固定不执行。
def _result_from_request(
    request: ParentAcceptanceAutoExecutionRequest,
) -> ParentAcceptanceAutoExecutionResult:
    blocked_by = [*request.preflight_blockers, "auto_executor_dry_run_only"]
    return ParentAcceptanceAutoExecutionResult(
        run_id=request.run_id,
        mode=request.mode,
        status="blocked",
        request=request,
        execution_allowed=False,
        guard_status="blocked",
        guard_reason="auto executor is dry-run only",
        command=request.recommended_command,
        blocked_by=blocked_by,
        safety_boundaries=[
            "dry_run_only",
            "no_process_execution",
            "no_task_state_mutation",
        ],
    )


# LLM: _execute_confirmed_tests is the only first-layer manual execution path.
# 函数用途: 在显式确认后执行 run_tests 类验证并写测试报告；不 apply、不 rescue、不改 task 状态。
def _execute_confirmed_tests(
    task: SubAgentTask,
    request: ParentAcceptanceAutoExecutionRequest,
    *,
    workspace_root: Path,
) -> ParentAcceptanceAutoExecutionResult:
    blockers = _manual_execution_blockers(request)
    if blockers:
        return _blocked_manual_result(request, blockers)
    output = _read_task_output(task)
    tests = _manual_execution_tests(task, output, workspace_root)
    if not tests:
        return _blocked_manual_result(request, ["missing_tests"])
    records = _manual_execution_records(tests, workspace_root, request.timeout_seconds)
    report = write_confirmed_test_report(
        ConfirmedTestReportRequest(task, request, workspace_root, records)
    )
    followup = write_execution_followup(task, workspace_root, report)
    return _tests_executed_result(
        TestsExecutedResultInput(request, report, followup, str(followup_path(task)))
    )


# LLM: _manual_execution_tests mirrors parent preflight normalization before confirmed execution.
# 函数用途: 为手动确认执行准备 tests；只归一化安全 cwd，不运行命令、不放开 shell。
def _manual_execution_tests(task: SubAgentTask, output: dict[str, object], workspace_root: Path) -> list[dict[str, Any]]:
    tests = _dict_list(output.get("tests", []))
    if not tests:
        return []
    # LLM: Manual acceptance uses the same static-site root hints as automatic dry-run preflight.
    return prepare_test_items(
        TestItemPreparationRequest(
            tests=tests,
            output=output,
            workspace_root=workspace_root,
            required_files=required_static_files_for_task(task),
            site_root_hints=static_site_root_hints_for_task(task),
        )
    )


# LLM: _manual_execution_records keeps subprocess execution out of the guard/result assembly function.
# 函数用途: 用受限 TestExecutor 执行已归一化 tests，并返回测试记录列表。
def _manual_execution_records(
    tests: list[dict[str, Any]],
    workspace_root: Path,
    timeout_seconds: float,
) -> list[Any]:
    executor = TestExecutor(workspace_root, timeout_seconds=timeout_seconds)
    return [executor.execute(test) for test in tests]


# LLM: _tests_executed_result centralizes the audit result shape for confirmed test runs.
# 函数用途: 根据真实测试报告和 follow-up 组装自动执行结果；仍不 apply、不 rescue、不改状态。
def _tests_executed_result(params: TestsExecutedResultInput) -> ParentAcceptanceAutoExecutionResult:
    request = params.request
    report = params.report
    followup = params.followup
    return ParentAcceptanceAutoExecutionResult(
        run_id=request.run_id,
        mode=request.mode,
        status="tests_executed",
        request=request,
        execution_allowed=True,
        guard_status="manual_confirmed",
        guard_reason="manual confirmation allowed run_tests execution",
        executed=True,
        mutates_task_state=False,
        command=request.recommended_command,
        blocked_by=[],
        safety_boundaries=[
            "manual_confirmed_only",
            "run_tests_only",
            "no_acceptance_apply",
            "no_rescue",
            "no_task_state_mutation",
        ],
        test_execution_ref=str(report.json_path),
        test_total=report.total_tests,
        test_failed=report.failed,
        followup_ref=params.followup_ref,
        followup_status=followup.status,
        followup_action=followup.action,
        followup_command=followup.command,
        followup_reason=followup.reason,
    )


# LLM: _manual_execution_blockers allows only the explicit run_tests path through.
# 函数用途: 检查手动确认执行是否仍被 blocker 拦住；自动执行禁用不阻止手动确认路径。
def _manual_execution_blockers(request: ParentAcceptanceAutoExecutionRequest) -> list[str]:
    blockers = [item for item in request.preflight_blockers if item != "automatic_execution_disabled"]
    if not request.manual_confirmed:
        blockers.append("missing_manual_confirmation")
    if not request.ready_for_manual_execution:
        blockers.append("not_ready_for_manual_execution")
    expected = f"subagents-tests {request.run_id} --re-run"
    if request.recommended_command != expected:
        blockers.append("unsupported_recommended_command")
    return blockers


# LLM: _blocked_manual_result keeps failed manual attempts audit-only.
# 函数用途: 生成手动确认路径的阻断结果；不执行 tests，也不修改 task 状态。
def _blocked_manual_result(
    request: ParentAcceptanceAutoExecutionRequest,
    blockers: list[str],
) -> ParentAcceptanceAutoExecutionResult:
    return ParentAcceptanceAutoExecutionResult(
        run_id=request.run_id,
        mode=request.mode,
        status="blocked",
        request=request,
        execution_allowed=False,
        guard_status="blocked",
        guard_reason="manual execution blocked by guard",
        command=request.recommended_command,
        blocked_by=blockers,
        safety_boundaries=[
            "manual_confirmed_only",
            "run_tests_only",
            "no_acceptance_apply",
            "no_rescue",
            "no_task_state_mutation",
        ],
    )


# LLM: _read_task_output uses the shared tolerant JSON reader so historical output.json defaults remain usable.
# 函数用途: 读取 task.output_json 的 tests 数组来源，兼容旧双层编码；失败时返回空结构，由 guard 生成阻断。
def _read_task_output(task: SubAgentTask) -> dict[str, object]:
    path = Path(getattr(task, "output_json", "") or "")
    if not path.is_file():
        return {}
    return _read_json_object(path)
