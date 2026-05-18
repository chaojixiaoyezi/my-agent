# LLM: Main-agent real task execution runs planned cases through bounded subprocess workers.
# 模块用途: 根据真实任务套件计划生成隔离配置和命令；显式 execute 时受控启动 my-agent run。

from __future__ import annotations

import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from .main_agent_real_task_execution_files import (
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
    try:
        completed = subprocess.run(
            runtime.command,
            cwd=package_root(runtime.request),
            capture_output=True,
            text=True,
            timeout=runtime.request.task_timeout_seconds,
            check=False,
        )
        duration = time.monotonic() - start
        runtime.paths["stdout"].write_text(completed.stdout, encoding="utf-8")
        runtime.paths["stderr"].write_text(completed.stderr, encoding="utf-8")
        status = "COMPLETED" if completed.returncode == 0 else "FAILED"
        issues = () if completed.returncode == 0 else (f"exit_code={completed.returncode}",)
        return _case_result(
            _CaseResultBundle(
                runtime=runtime,
                status=status,
                exit_code=completed.returncode,
                duration_seconds=duration,
                issues=issues,
            )
        )
    except subprocess.TimeoutExpired as exc:
        duration = time.monotonic() - start
        runtime.paths["stdout"].write_text(str(exc.stdout or ""), encoding="utf-8")
        runtime.paths["stderr"].write_text(str(exc.stderr or ""), encoding="utf-8")
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
        exit_code=bundle.exit_code,
        duration_seconds=bundle.duration_seconds,
        issues=list(bundle.issues),
    )


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


__all__ = [
    "MainAgentRealTaskExecutionCaseResult",
    "MainAgentRealTaskExecutionReport",
    "MainAgentRealTaskExecutionRequest",
    "run_main_agent_real_task_execution",
]
