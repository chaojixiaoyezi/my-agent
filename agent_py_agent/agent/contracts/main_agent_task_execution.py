# LLM: Main-agent task execution runs planned cases through bounded subprocess workers.
# 模块用途: 根据通用任务套件计划生成隔离配置和命令；显式 execute 时受控启动 my-agent run。

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

from .main_agent_task_execution_files import (
    append_event,
    case_paths,
    command_for_case,
    execution_root,
    package_root,
    rel,
    write_case_config,
    write_json,
)
from .main_agent_task_execution_models import (
    SCHEMA_VERSION,
    MainAgentTaskExecutionCaseResult,
    MainAgentTaskExecutionReport,
    MainAgentTaskExecutionRequest,
)
from .main_agent_task_execution_results import (
    activity_timeout_seconds,
    append_acceptance_event,
    case_issues,
    case_result,
    timeout_issues,
    validate_case_artifacts,
    write_recovery_packet_ref,
)
from .main_agent_task_execution_state import CaseResultBundle, CaseRuntime
from .main_agent_task_execution_summary import (
    concurrency_summary,
    execution_summary,
    select_cases,
)
from .main_agent_task_recovery_resume import (
    case_ids_for_recovery_request,
    resume_attempt_paths,
)
from .main_agent_task_revalidation import revalidate_main_agent_task_execution
from .main_agent_task_subprocess import (
    TaskRunSubprocessRequest,
    TaskRunSubprocessResult,
    run_task_subprocess,
)
from .main_agent_task_suite import (
    MainAgentTaskCasePlan,
    MainAgentTaskSuiteRequest,
    plan_main_agent_task_suite,
)

_AUTO_RECOVERY_ATTEMPTS = 1


# LLM: run_main_agent_task_execution is the public controlled runner entrypoint.
# 函数用途: 生成真实任务计划和隔离运行文件；execute=True 时逐个受控启动主代理 run。
def run_main_agent_task_execution(
    request: MainAgentTaskExecutionRequest,
) -> MainAgentTaskExecutionReport:
    workspace = Path(request.workspace).expanduser().resolve()
    suite = plan_main_agent_task_suite(
        MainAgentTaskSuiteRequest(
            workspace=workspace,
            max_workers=request.max_workers,
            task_timeout_seconds=request.task_timeout_seconds,
            execute=request.execute,
        )
    )
    selected = select_cases(
        suite.cases,
        case_ids_for_recovery_request(request.case_ids, request.recovery_packet_path),
    )
    results = _run_selected_cases(selected, request, workspace=workspace)
    report = MainAgentTaskExecutionReport(
        ok=not any(case.status == "FAILED" for case in results),
        schema_version=SCHEMA_VERSION,
        execution_mode="execute" if request.execute else "plan_only",
        summary=execution_summary(results),
        concurrency=concurrency_summary(request, selected),
        suite_report_ref=suite.report_ref,
        report_ref=rel(execution_root(workspace) / "execution_report.json", workspace),
        cases=results,
    )
    write_json(execution_root(workspace) / "execution_report.json", report.to_dict())
    return report


# LLM: _run_selected_cases honors max_workers while keeping each task isolated.
# 函数用途: 按结构化 case 列表并发准备或执行任务，避免 CLI 一次只能跑一个主代理任务。
def _run_selected_cases(
    cases: list[MainAgentTaskCasePlan],
    request: MainAgentTaskExecutionRequest,
    *,
    workspace: Path,
) -> list[MainAgentTaskExecutionCaseResult]:
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
    case: MainAgentTaskCasePlan,
    request: MainAgentTaskExecutionRequest,
    *,
    workspace: Path,
) -> MainAgentTaskExecutionCaseResult:
    _ensure_case_contract_refs(case, request, workspace=workspace)
    paths = case_paths(workspace, case.case_id)
    paths = resume_attempt_paths(paths, request.recovery_packet_path)
    task_workspace = paths["workspace"]
    config_path = paths["config"]
    write_case_config(config_path, request.base_config_path, task_workspace)
    command = command_for_case(
        case,
        request,
        config_path=config_path,
        workspace=workspace,
        delivery_contract_path=paths["delivery_contract"],
    )
    write_json(paths["command"], {"argv": command, "cwd": str(package_root(request))})
    append_event(paths["events"], "case_prepared", {"case_id": case.case_id})
    runtime = CaseRuntime(
        case=case,
        request=request,
        paths=paths,
        command=command,
        workspace=workspace,
    )
    if not request.execute:
        return _case_result(CaseResultBundle(runtime=runtime, status="PLANNING"))
    return _run_case(runtime)


# LLM: _ensure_case_contract_refs lazily re-materializes suite files when a selected case is missing contract refs.
# 函数用途: 确保 prompt/acceptance/expected_artifacts 这些 case 依赖文件已经在工作区落地。
def _ensure_case_contract_refs(
    case: MainAgentTaskCasePlan,
    request: MainAgentTaskExecutionRequest,
    *,
    workspace: Path,
) -> None:
    refs = [case.prompt_ref, case.acceptance_ref, case.expected_artifacts_ref]
    if all((workspace / ref).exists() for ref in refs if ref):
        return
    plan_main_agent_task_suite(
        MainAgentTaskSuiteRequest(
            workspace=workspace,
            max_workers=request.max_workers,
            task_timeout_seconds=request.task_timeout_seconds,
            execute=request.execute,
        )
    )


# LLM: _run_case executes the subprocess without shell expansion and stores bounded refs.
# 函数用途: 启动一次主代理 run，捕获输出文件；超时和非零退出都会进结构化 issues。
def _run_case(runtime: CaseRuntime) -> MainAgentTaskExecutionCaseResult:
    append_event(runtime.paths["events"], "case_started", {"case_id": runtime.case.case_id})
    process = run_task_subprocess(
        TaskRunSubprocessRequest(
            runtime.command,
            cwd=package_root(runtime.request),
            stdout_path=runtime.paths["stdout"],
            stderr_path=runtime.paths["stderr"],
            workspace=runtime.paths["workspace"],
            timeout_seconds=runtime.request.task_timeout_seconds,
            activity_timeout_seconds=activity_timeout_seconds(runtime.request.task_timeout_seconds),
        )
    )
    if process.timed_out:
        return _timeout_case_result(runtime, process)
    return _completed_case_result(runtime, process)


# LLM: _completed_case_result converts one subprocess completion into report facts.
# 函数用途: 写 stdout/stderr、验收产物、追加事件，并返回最终 case 结果。
def _completed_case_result(
    runtime: CaseRuntime,
    process: TaskRunSubprocessResult,
) -> MainAgentTaskExecutionCaseResult:
    append_event(
        runtime.paths["events"],
        "case_finished",
        {"case_id": runtime.case.case_id, "exit_code": process.exit_code},
    )
    acceptance = validate_case_artifacts(runtime)
    status = "DONE" if process.exit_code == 0 and acceptance.ok else "FAILED"
    append_acceptance_event(runtime, acceptance)
    issues = case_issues(runtime, process.exit_code, acceptance)
    bundle = CaseResultBundle(
        runtime=runtime,
        status=status,
        acceptance=acceptance,
        exit_code=process.exit_code,
        duration_seconds=process.duration_seconds,
        issues=issues,
    )
    bundle.recovery_packet_ref = write_recovery_packet_ref(bundle)
    if _should_auto_resume(bundle):
        return _auto_resume_case(bundle)
    return case_result(bundle)


# LLM: _timeout_case_result keeps timeout handling separate from normal completion.
# 函数用途: 记录 timeout stdout/stderr 和事件，返回稳定失败结果。
def _timeout_case_result(
    runtime: CaseRuntime,
    process: TaskRunSubprocessResult,
) -> MainAgentTaskExecutionCaseResult:
    acceptance = validate_case_artifacts(runtime)
    append_event(
        runtime.paths["events"],
        "case_timeout" if process.timeout_reason == "timeout" else "case_activity_timeout",
        {
            "case_id": runtime.case.case_id,
            "timeout_reason": process.timeout_reason or "timeout",
            "timeout_seconds": runtime.request.task_timeout_seconds,
        },
    )
    append_acceptance_event(runtime, acceptance)
    status = "DONE" if acceptance.ok else "FAILED"
    issues = timeout_issues(acceptance, timeout_reason=process.timeout_reason)
    bundle = CaseResultBundle(
        runtime=runtime,
        status=status,
        acceptance=acceptance,
        exit_code=124,
        duration_seconds=process.duration_seconds,
        issues=issues,
    )
    bundle.recovery_packet_ref = write_recovery_packet_ref(bundle)
    if _should_auto_resume(bundle):
        return _auto_resume_case(bundle)
    return case_result(bundle)


# LLM: _should_auto_resume decides whether a failed case should be retried from its freshly written recovery packet.
# 函数用途: 根据失败状态、recovery packet 和当前请求上下文判断是否触发一次自动续跑。
def _should_auto_resume(bundle: CaseResultBundle) -> bool:
    runtime = bundle.runtime
    if _AUTO_RECOVERY_ATTEMPTS <= 0:
        return False
    return bool(
        bundle.status == "FAILED"
        and bundle.recovery_packet_ref
        and runtime.request.recovery_packet_path is None
    )


# LLM: _auto_resume_case re-enters the normal execution path with a structured recovery packet instead of ad hoc retry logic.
# 函数用途: 使用刚生成的 recovery packet 重建请求，再走同一套 case 准备和执行流程。
def _auto_resume_case(bundle: CaseResultBundle) -> MainAgentTaskExecutionCaseResult:
    runtime = bundle.runtime
    packet_path = (runtime.workspace / bundle.recovery_packet_ref).resolve()
    append_event(
        runtime.paths["events"],
        "case_auto_resume_started",
        {"case_id": runtime.case.case_id, "recovery_packet_ref": bundle.recovery_packet_ref},
    )
    return _prepare_or_execute(
        runtime.case,
        replace(runtime.request, recovery_packet_path=packet_path),
        workspace=runtime.workspace,
    )

__all__ = ["MainAgentTaskExecutionCaseResult", "MainAgentTaskExecutionReport", "MainAgentTaskExecutionRequest", "revalidate_main_agent_task_execution", "run_main_agent_task_execution"]
