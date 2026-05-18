# LLM: Main-agent real task execution runs planned cases through bounded subprocess workers.
# 模块用途: 根据真实任务套件计划生成隔离配置和命令；显式 execute 时受控启动 my-agent run。

from __future__ import annotations

import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from .main_agent_real_task_acceptance import (
    RealTaskAcceptanceReport,
    RealTaskAcceptanceRequest,
    validate_real_task_artifacts,
)
from .main_agent_real_task_execution_files import (
    append_event,
    case_paths,
    command_for_case,
    execution_root,
    package_root,
    rel,
    write_case_config,
    write_json,
)
from .main_agent_real_task_execution_models import (
    SCHEMA_VERSION,
    MainAgentRealTaskExecutionCaseResult,
    MainAgentRealTaskExecutionReport,
    MainAgentRealTaskExecutionRequest,
)
from .main_agent_real_task_suite import (
    MainAgentRealTaskCasePlan,
    MainAgentRealTaskSuiteRequest,
    plan_main_agent_real_task_suite,
)


# LLM: _CaseRuntime bundles all immutable facts needed to execute one planned case.
# 类用途: 聚合单个真实任务的计划、请求、路径、命令和工作区，避免内部 helper 参数散落。
@dataclass(frozen=True)
class _CaseRuntime:
    case: MainAgentRealTaskCasePlan
    request: MainAgentRealTaskExecutionRequest
    paths: dict[str, Path]
    command: list[str]
    workspace: Path


# LLM: _CaseResultBundle bundles status facts before converting them into the public report.
# 类用途: 描述单个任务的执行结果输入，减少 helper 参数数量并保持报告字段结构化。
@dataclass(frozen=True)
class _CaseResultBundle:
    runtime: _CaseRuntime
    status: str
    acceptance: RealTaskAcceptanceReport | None = None
    exit_code: int | None = None
    duration_seconds: float = 0.0
    issues: tuple[str, ...] = field(default_factory=tuple)


# LLM: run_main_agent_real_task_execution is the public controlled runner entrypoint.
# 函数用途: 生成真实任务计划和隔离运行文件；execute=True 时逐个受控启动主代理 run。
def run_main_agent_real_task_execution(
    request: MainAgentRealTaskExecutionRequest,
) -> MainAgentRealTaskExecutionReport:
    workspace = Path(request.workspace).expanduser().resolve()
    suite = plan_main_agent_real_task_suite(
        MainAgentRealTaskSuiteRequest(
            workspace=workspace,
            max_workers=request.max_workers,
            task_timeout_seconds=request.task_timeout_seconds,
            execute=request.execute,
        )
    )
    selected = _select_cases(suite.cases, request.case_ids)
    results = _run_selected_cases(selected, request, workspace=workspace)
    report = MainAgentRealTaskExecutionReport(
        ok=not any(case.status == "FAILED" for case in results),
        schema_version=SCHEMA_VERSION,
        execution_mode="execute" if request.execute else "plan_only",
        summary=_summary(results),
        concurrency=_concurrency_summary(request, selected),
        suite_report_ref=suite.report_ref,
        report_ref=rel(execution_root(workspace) / "execution_report.json", workspace),
        cases=results,
    )
    write_json(execution_root(workspace) / "execution_report.json", report.to_dict())
    return report


# LLM: _run_selected_cases honors max_workers while keeping each task isolated.
# 函数用途: 按结构化 case 列表并发准备或执行任务，避免 CLI 一次只能跑一个主代理任务。
def _run_selected_cases(
    cases: list[MainAgentRealTaskCasePlan],
    request: MainAgentRealTaskExecutionRequest,
    *,
    workspace: Path,
) -> list[MainAgentRealTaskExecutionCaseResult]:
    max_workers = max(1, min(request.max_workers, len(cases) or 1))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        return list(
            executor.map(
                lambda case: _prepare_or_execute(case, request, workspace=workspace),
                cases,
            )
        )


# LLM: _prepare_or_execute materializes one case command and optionally runs it.
# 函数用途: 为单个任务写配置/命令/日志路径；执行时捕获 stdout、stderr、退出码和超时。
def _prepare_or_execute(
    case: MainAgentRealTaskCasePlan,
    request: MainAgentRealTaskExecutionRequest,
    *,
    workspace: Path,
) -> MainAgentRealTaskExecutionCaseResult:
    paths = case_paths(workspace, case.case_id)
    task_workspace = paths["workspace"]
    config_path = paths["config"]
    write_case_config(config_path, request.base_config_path, task_workspace)
    command = command_for_case(case, request, config_path=config_path, workspace=workspace)
    write_json(paths["command"], {"argv": command, "cwd": str(package_root(request))})
    append_event(paths["events"], "case_prepared", {"case_id": case.case_id})
    runtime = _CaseRuntime(
        case=case,
        request=request,
        paths=paths,
        command=command,
        workspace=workspace,
    )
    if not request.execute:
        return _case_result(_CaseResultBundle(runtime=runtime, status="PLANNED"))
    return _run_case(runtime)


# LLM: _run_case executes the subprocess without shell expansion and stores bounded refs.
# 函数用途: 启动一次主代理 run，捕获输出文件；超时和非零退出都会进结构化 issues。
def _run_case(runtime: _CaseRuntime) -> MainAgentRealTaskExecutionCaseResult:
    start = time.monotonic()
    append_event(runtime.paths["events"], "case_started", {"case_id": runtime.case.case_id})
    try:
        completed = subprocess.run(
            runtime.command,
            cwd=package_root(runtime.request),
            capture_output=True,
            text=True,
            timeout=runtime.request.task_timeout_seconds,
            check=False,
        )
        return _completed_case_result(runtime, completed, duration=time.monotonic() - start)
    except subprocess.TimeoutExpired as exc:
        return _timeout_case_result(runtime, exc, duration=time.monotonic() - start)


# LLM: _completed_case_result converts one subprocess completion into report facts.
# 函数用途: 写 stdout/stderr、验收产物、追加事件，并返回最终 case 结果。
def _completed_case_result(
    runtime: _CaseRuntime,
    completed: subprocess.CompletedProcess[str],
    *,
    duration: float,
) -> MainAgentRealTaskExecutionCaseResult:
    runtime.paths["stdout"].write_text(completed.stdout, encoding="utf-8")
    runtime.paths["stderr"].write_text(completed.stderr, encoding="utf-8")
    append_event(
        runtime.paths["events"],
        "case_finished",
        {"case_id": runtime.case.case_id, "exit_code": completed.returncode},
    )
    acceptance = _validate_case_artifacts(runtime)
    status = "COMPLETED" if completed.returncode == 0 and acceptance.ok else "FAILED"
    _append_acceptance_event(runtime, acceptance)
    return _case_result(
        _CaseResultBundle(
            runtime=runtime,
            status=status,
            acceptance=acceptance,
            exit_code=completed.returncode,
            duration_seconds=duration,
            issues=_case_issues(completed.returncode, acceptance),
        )
    )


# LLM: _timeout_case_result keeps timeout handling separate from normal completion.
# 函数用途: 记录 timeout stdout/stderr 和事件，返回稳定失败结果。
def _timeout_case_result(
    runtime: _CaseRuntime,
    exc: subprocess.TimeoutExpired,
    *,
    duration: float,
) -> MainAgentRealTaskExecutionCaseResult:
    runtime.paths["stdout"].write_text(str(exc.stdout or ""), encoding="utf-8")
    runtime.paths["stderr"].write_text(str(exc.stderr or ""), encoding="utf-8")
    append_event(
        runtime.paths["events"],
        "case_timeout",
        {"case_id": runtime.case.case_id, "timeout_seconds": runtime.request.task_timeout_seconds},
    )
    return _case_result(
        _CaseResultBundle(
            runtime=runtime,
            status="FAILED",
            exit_code=124,
            duration_seconds=duration,
            issues=("timeout",),
        )
    )


# LLM: _case_result converts per-case files into the execution report shape.
# 函数用途: 汇总单个任务的引用字段和执行状态，保持 stdout/stderr 外置。
def _case_result(bundle: _CaseResultBundle) -> MainAgentRealTaskExecutionCaseResult:
    runtime = bundle.runtime
    acceptance = bundle.acceptance
    return MainAgentRealTaskExecutionCaseResult(
        case_id=runtime.case.case_id,
        title=runtime.case.title,
        status=bundle.status,
        worker_slot=runtime.case.worker_slot,
        timeout_seconds=runtime.case.timeout_seconds,
        prompt_ref=runtime.case.prompt_ref,
        config_ref=rel(runtime.paths["config"], runtime.workspace),
        command_ref=rel(runtime.paths["command"], runtime.workspace),
        stdout_ref=rel(runtime.paths["stdout"], runtime.workspace),
        stderr_ref=rel(runtime.paths["stderr"], runtime.workspace),
        acceptance_report_ref=rel(runtime.paths["acceptance_report"], runtime.workspace),
        events_ref=rel(runtime.paths["events"], runtime.workspace),
        acceptance_summary=dict(acceptance.summary if acceptance else {}),
        exit_code=bundle.exit_code,
        duration_seconds=bundle.duration_seconds,
        issues=list(bundle.issues),
    )


# LLM: _validate_case_artifacts connects subprocess completion to artifact acceptance.
# 函数用途: 读取该 case 的 expected_artifacts_ref，并在任务 workspace 内验收产物。
def _validate_case_artifacts(runtime: _CaseRuntime) -> RealTaskAcceptanceReport:
    return validate_real_task_artifacts(
        RealTaskAcceptanceRequest(
            expected_artifacts_path=runtime.workspace / runtime.case.expected_artifacts_ref,
            task_workspace=runtime.paths["workspace"],
            report_path=runtime.paths["acceptance_report"],
        )
    )


# LLM: _append_acceptance_event records case-level validation outcome for live observation.
# 函数用途: 将产物验收通过/失败写进 events.jsonl，长任务未结束时也能看出卡点。
def _append_acceptance_event(
    runtime: _CaseRuntime,
    acceptance: RealTaskAcceptanceReport,
) -> None:
    append_event(
        runtime.paths["events"],
        "case_acceptance_passed" if acceptance.ok else "case_acceptance_failed",
        {
            "case_id": runtime.case.case_id,
            "summary": dict(acceptance.summary),
            "report_ref": rel(runtime.paths["acceptance_report"], runtime.workspace),
        },
    )


# LLM: _case_issues merges process and artifact failures into structured short issue codes.
# 函数用途: 生成 case 级失败摘要；详细 findings 留在 acceptance_report_ref。
def _case_issues(
    exit_code: int,
    acceptance: RealTaskAcceptanceReport,
) -> tuple[str, ...]:
    issues: list[str] = []
    if exit_code != 0:
        issues.append(f"exit_code={exit_code}")
    failed = int(acceptance.summary.get("failed", 0))
    if failed:
        issues.append(f"artifact_acceptance_failed={failed}")
    return tuple(issues)


# LLM: _select_cases filters by structured case ids, never by prompt text.
# 函数用途: 按 case_id 选择要计划或执行的任务；空列表表示全量。
def _select_cases(
    cases: list[MainAgentRealTaskCasePlan],
    case_ids: tuple[str, ...],
) -> list[MainAgentRealTaskCasePlan]:
    if not case_ids:
        return cases
    allowed = set(case_ids)
    return [case for case in cases if case.case_id in allowed]


# LLM: _summary counts planned and executed case statuses for CLI display.
# 函数用途: 汇总执行报告状态，方便用户快速看计划/成功/失败数量。
def _summary(cases: list[MainAgentRealTaskExecutionCaseResult]) -> dict[str, int]:
    return {
        "total": len(cases),
        "planned": sum(case.status == "PLANNED" for case in cases),
        "completed": sum(case.status == "COMPLETED" for case in cases),
        "failed": sum(case.status == "FAILED" for case in cases),
    }


# LLM: _concurrency_summary records effective worker limits for later real API smoke runs.
# 函数用途: 把请求并发和实际执行工位写进报告，方便观察 4 主代理并发是否按合同运行。
def _concurrency_summary(
    request: MainAgentRealTaskExecutionRequest,
    cases: list[MainAgentRealTaskCasePlan],
) -> dict[str, int]:
    return {
        "requested_max_workers": request.max_workers,
        "effective_max_workers": max(1, min(request.max_workers, len(cases) or 1)),
        "case_count": len(cases),
    }


__all__ = [
    "MainAgentRealTaskExecutionCaseResult",
    "MainAgentRealTaskExecutionReport",
    "MainAgentRealTaskExecutionRequest",
    "run_main_agent_real_task_execution",
]
