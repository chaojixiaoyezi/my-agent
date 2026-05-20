# LLM: Main-agent task execution result helpers keep runner orchestration thin.
# 模块用途: 汇总 case 验收、事件、issue 和恢复包引用，避免执行 runner 文件承载过多职责。

from __future__ import annotations

from .main_agent_task_acceptance import (
    TaskRunAcceptanceReport,
    TaskRunAcceptanceRequest,
    validate_task_artifacts,
)
from .main_agent_task_execution_files import append_event, rel
from .main_agent_task_execution_models import MainAgentTaskExecutionCaseResult
from .main_agent_task_execution_state import CaseResultBundle, CaseRuntime
from .main_agent_task_recovery_packet import (
    TaskRunRecoveryPacketRequest,
    task_recovery_refs,
    write_task_recovery_packet,
)
from .main_agent_task_runtime_issues import case_issue_codes


# LLM: activity_timeout_seconds derives a live-observation bound from the total case timeout.
# 函数用途: 真实任务如果长时间没有日志或文件活动，就提前停止并保留 checkpoint/日志证据。
def activity_timeout_seconds(task_timeout_seconds: int) -> int:
    total = max(1, int(task_timeout_seconds))
    if total <= 600:
        return total
    return min(total, max(600, total // 2))


# LLM: timeout_issues separates hard timeouts from already-valid deliverables.
# 函数用途: 超时时根据产物验收结果输出稳定 issue code，避免有效产物被误判失败。
def timeout_issues(
    acceptance: TaskRunAcceptanceReport,
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


# LLM: case_result converts per-case files into the execution report shape.
# 函数用途: 汇总单个任务的引用字段和执行状态，保持 stdout/stderr 外置。
def case_result(bundle: CaseResultBundle) -> MainAgentTaskExecutionCaseResult:
    runtime = bundle.runtime
    acceptance = bundle.acceptance
    return MainAgentTaskExecutionCaseResult(
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


# LLM: validate_case_artifacts connects subprocess completion to artifact acceptance.
# 函数用途: 读取该 case 的 expected_artifacts_ref，并在任务 workspace 内验收产物。
def validate_case_artifacts(runtime: CaseRuntime) -> TaskRunAcceptanceReport:
    return validate_task_artifacts(
        TaskRunAcceptanceRequest(
            expected_artifacts_path=runtime.workspace / runtime.case.expected_artifacts_ref,
            task_workspace=runtime.paths["workspace"],
            report_path=runtime.paths["acceptance_report"],
        )
    )


# LLM: append_acceptance_event records case-level validation outcome for live observation.
# 函数用途: 将产物验收通过/失败写进 events.jsonl，长任务未结束时也能看出卡点。
def append_acceptance_event(
    runtime: CaseRuntime,
    acceptance: TaskRunAcceptanceReport,
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


# LLM: case_issues merges process and artifact failures into structured short issue codes.
# 函数用途: 生成 case 级失败摘要；详细 findings 留在 acceptance_report_ref。
def case_issues(
    runtime: CaseRuntime,
    exit_code: int,
    acceptance: TaskRunAcceptanceReport,
) -> tuple[str, ...]:
    return case_issue_codes(
        exit_code=exit_code,
        acceptance=acceptance,
        stdout_path=runtime.paths["stdout"],
    )


# LLM: write_recovery_packet_ref materializes continuation facts for failed real tasks.
# 函数用途: 失败/超时时写 recovery_packet.json；完成任务不写恢复包，避免制造噪音。
def write_recovery_packet_ref(bundle: CaseResultBundle) -> str:
    runtime = bundle.runtime
    acceptance = bundle.acceptance
    if bundle.status == "DONE" or acceptance is None:
        return ""
    return write_task_recovery_packet(
        TaskRunRecoveryPacketRequest(
            case_id=runtime.case.case_id,
            title=runtime.case.title,
            status=bundle.status,
            exit_code=bundle.exit_code,
            duration_seconds=bundle.duration_seconds,
            reason_codes=bundle.issues,
            workspace_root=runtime.workspace,
            packet_path=runtime.paths["recovery_packet"],
            refs=task_recovery_refs(runtime.paths, runtime.case),
            acceptance=acceptance,
        )
    )


__all__ = [
    "activity_timeout_seconds",
    "append_acceptance_event",
    "case_issues",
    "case_result",
    "timeout_issues",
    "validate_case_artifacts",
    "write_recovery_packet_ref",
]
