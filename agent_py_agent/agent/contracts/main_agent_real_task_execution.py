# LLM: Main-agent real task execution runs planned cases through bounded subprocess workers.
# 模块用途: 根据真实任务套件计划生成隔离配置和命令；显式 execute 时受控启动 my-agent run。

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
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
from .main_agent_real_task_execution_state import CaseResultBundle, CaseRuntime
from .main_agent_real_task_execution_summary import (
    concurrency_summary,
    execution_summary,
    select_cases,
)
from .main_agent_real_task_recovery_packet import (
    RealTaskRecoveryPacketRequest,
    real_task_recovery_refs,
    write_real_task_recovery_packet,
)
from .main_agent_real_task_recovery_resume import (
    case_ids_for_recovery_request,
    resume_attempt_paths,
)
from .main_agent_real_task_revalidation import revalidate_main_agent_real_task_execution
from .main_agent_real_task_runtime_issues import case_issue_codes
from .main_agent_real_task_subprocess import (
    RealTaskSubprocessRequest,
    RealTaskSubprocessResult,
    run_real_task_subprocess,
)
from .main_agent_real_task_suite import (
    MainAgentRealTaskCasePlan,
    MainAgentRealTaskSuiteRequest,
    plan_main_agent_real_task_suite,
)


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
    selected = select_cases(
        suite.cases,
        case_ids_for_recovery_request(request.case_ids, request.recovery_packet_path),
    )
    results = _run_selected_cases(selected, request, workspace=workspace)
    report = MainAgentRealTaskExecutionReport(
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
        return _case_result(CaseResultBundle(runtime=runtime, status="PLANNED"))
    return _run_case(runtime)


# LLM: _run_case executes the subprocess without shell expansion and stores bounded refs.
# 函数用途: 启动一次主代理 run，捕获输出文件；超时和非零退出都会进结构化 issues。
def _run_case(runtime: CaseRuntime) -> MainAgentRealTaskExecutionCaseResult:
    append_event(runtime.paths["events"], "case_started", {"case_id": runtime.case.case_id})
    process = run_real_task_subprocess(
        RealTaskSubprocessRequest(
            runtime.command,
            cwd=package_root(runtime.request),
            stdout_path=runtime.paths["stdout"],
            stderr_path=runtime.paths["stderr"],
            workspace=runtime.paths["workspace"],
            timeout_seconds=runtime.request.task_timeout_seconds,
            activity_timeout_seconds=_activity_timeout_seconds(runtime.request.task_timeout_seconds),
        )
    )
    if process.timed_out:
        return _timeout_case_result(runtime, process)
    return _completed_case_result(runtime, process)


# LLM: _completed_case_result converts one subprocess completion into report facts.
# 函数用途: 写 stdout/stderr、验收产物、追加事件，并返回最终 case 结果。
def _completed_case_result(
    runtime: CaseRuntime,
    process: RealTaskSubprocessResult,
) -> MainAgentRealTaskExecutionCaseResult:
    append_event(
        runtime.paths["events"],
        "case_finished",
        {"case_id": runtime.case.case_id, "exit_code": process.exit_code},
    )
    acceptance = _validate_case_artifacts(runtime)
    status = "COMPLETED" if process.exit_code == 0 and acceptance.ok else "FAILED"
    _append_acceptance_event(runtime, acceptance)
    issues = _case_issues(runtime, process.exit_code, acceptance)
    bundle = CaseResultBundle(
        runtime=runtime,
        status=status,
        acceptance=acceptance,
        exit_code=process.exit_code,
        duration_seconds=process.duration_seconds,
        issues=issues,
    )
    bundle.recovery_packet_ref = _write_recovery_packet_ref(bundle)
    return _case_result(bundle)


# LLM: _timeout_case_result keeps timeout handling separate from normal completion.
# 函数用途: 记录 timeout stdout/stderr 和事件，返回稳定失败结果。
def _timeout_case_result(
    runtime: CaseRuntime,
    process: RealTaskSubprocessResult,
) -> MainAgentRealTaskExecutionCaseResult:
    acceptance = _validate_case_artifacts(runtime)
    append_event(
        runtime.paths["events"],
        "case_timeout" if process.timeout_reason == "timeout" else "case_activity_timeout",
        {
            "case_id": runtime.case.case_id,
            "timeout_reason": process.timeout_reason or "timeout",
            "timeout_seconds": runtime.request.task_timeout_seconds,
        },
    )
    _append_acceptance_event(runtime, acceptance)
    status = "COMPLETED" if acceptance.ok else "FAILED"
    issues = _timeout_issues(acceptance, timeout_reason=process.timeout_reason)
    bundle = CaseResultBundle(
        runtime=runtime,
        status=status,
        acceptance=acceptance,
        exit_code=124,
        duration_seconds=process.duration_seconds,
        issues=issues,
    )
    bundle.recovery_packet_ref = _write_recovery_packet_ref(bundle)
    return _case_result(bundle)


# LLM: _timeout_issues separates hard timeouts from already-valid deliverables.
# 函数用途: 超时时根据产物验收结果输出稳定 issue code，避免有效产物被误判失败。
def _timeout_issues(
    acceptance: RealTaskAcceptanceReport,
    *,
    timeout_reason: str,
) -> tuple[str, ...]:
    if acceptance.ok:
        return ("process_timeout_after_valid_artifact",)
    failed = int(acceptance.summary.get("failed", 0))
    issues = [timeout_reason or "timeout"]
    if failed:
        issues.append(f"artifact_acceptance_failed={failed}")
    return tuple(issues)


# LLM: _activity_timeout_seconds derives a live-observation bound from the total case timeout.
# 函数用途: 真实任务如果长时间没有日志或文件活动，就提前停止并保留 checkpoint/日志证据。
def _activity_timeout_seconds(task_timeout_seconds: int) -> int:
    total = max(1, int(task_timeout_seconds))
    if total <= 600:
        return total
    return min(total, max(600, total // 2))


# LLM: _case_result converts per-case files into the execution report shape.
# 函数用途: 汇总单个任务的引用字段和执行状态，保持 stdout/stderr 外置。
def _case_result(bundle: CaseResultBundle) -> MainAgentRealTaskExecutionCaseResult:
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
        recovery_packet_ref=bundle.recovery_packet_ref,
        acceptance_summary=dict(acceptance.summary if acceptance else {}),
        exit_code=bundle.exit_code,
        duration_seconds=bundle.duration_seconds,
        issues=list(bundle.issues),
    )


# LLM: _validate_case_artifacts connects subprocess completion to artifact acceptance.
# 函数用途: 读取该 case 的 expected_artifacts_ref，并在任务 workspace 内验收产物。
def _validate_case_artifacts(runtime: CaseRuntime) -> RealTaskAcceptanceReport:
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
    runtime: CaseRuntime,
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
    runtime: CaseRuntime,
    exit_code: int,
    acceptance: RealTaskAcceptanceReport,
) -> tuple[str, ...]:
    return case_issue_codes(
        exit_code=exit_code,
        acceptance=acceptance,
        stdout_path=runtime.paths["stdout"],
    )


# LLM: _write_recovery_packet_ref materializes continuation facts for failed real tasks.
# 函数用途: 失败/超时时写 recovery_packet.json；完成任务不写恢复包，避免制造噪音。
def _write_recovery_packet_ref(bundle: CaseResultBundle) -> str:
    runtime = bundle.runtime
    acceptance = bundle.acceptance
    if bundle.status == "COMPLETED" or acceptance is None:
        return ""
    return write_real_task_recovery_packet(
        RealTaskRecoveryPacketRequest(
            case_id=runtime.case.case_id,
            title=runtime.case.title,
            status=bundle.status,
            exit_code=bundle.exit_code,
            duration_seconds=bundle.duration_seconds,
            reason_codes=bundle.issues,
            workspace_root=runtime.workspace,
            packet_path=runtime.paths["recovery_packet"],
            refs=real_task_recovery_refs(runtime.paths, runtime.case),
            acceptance=acceptance,
        )
    )


__all__ = ["MainAgentRealTaskExecutionCaseResult", "MainAgentRealTaskExecutionReport", "MainAgentRealTaskExecutionRequest", "revalidate_main_agent_real_task_execution", "run_main_agent_real_task_execution"]
