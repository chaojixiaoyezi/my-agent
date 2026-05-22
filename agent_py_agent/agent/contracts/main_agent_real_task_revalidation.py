# LLM: Real task revalidation re-checks artifacts from a stored execution report.
# 模块用途: 读取已有真实任务执行报告，只复验产物合同，不重新启动模型或工具进程。

from __future__ import annotations

from pathlib import Path

from .main_agent_real_task_acceptance import (
    RealTaskAcceptanceRequest,
    validate_real_task_artifacts,
)
from .main_agent_real_task_execution_files import case_paths, write_json
from .main_agent_real_task_execution_models import (
    SCHEMA_VERSION,
    MainAgentRealTaskExecutionCaseResult,
    MainAgentRealTaskExecutionReport,
)
from .main_agent_task_common import (
    InvalidRevalidationReportRequest,
    ResolveCasePathsRequest,
    build_invalid_revalidation_report,
    case_issues,
    expected_artifacts_ref,
    optional_int,
    payload_cases,
    payload_concurrency,
    read_report_payload,
    relative_ref,
    resolve_case_paths,
    status_summary,
)


# LLM: revalidate_main_agent_real_task_execution re-checks artifacts without rerunning commands.
# 函数用途: 读取已有 execution_report.json，只复验 expected artifact 合同并重写验收报告。
def revalidate_main_agent_real_task_execution(
    report_path: Path,
    *,
    workspace: Path | None = None,
) -> MainAgentRealTaskExecutionReport:
    base = Path(workspace).expanduser().resolve() if workspace else Path(report_path).parent.parent
    payload, load_error = read_report_payload(Path(report_path))
    if load_error:
        report = _invalid_report(Path(report_path), base, load_error)
        write_json(Path(report_path), report.to_dict())
        return report
    cases = [_revalidate_case(item, workspace=base, report_path=Path(report_path)) for item in payload_cases(payload)]
    report = MainAgentRealTaskExecutionReport(
        ok=not any(case.status == "FAILED" for case in cases),
        schema_version=SCHEMA_VERSION,
        execution_mode="revalidate",
        summary=status_summary(cases),
        concurrency=payload_concurrency(payload, len(cases)),
        suite_report_ref=str(payload.get("suite_report_ref") or ""),
        report_ref=relative_ref(Path(report_path), base),
        cases=cases,
    )
    write_json(Path(report_path), report.to_dict())
    return report


# LLM: _invalid_report keeps this contract helper structure-first and stable.
# 函数用途: 支撑本模块的机器字段校验、转换或汇总，不读取普通自然语言作为事实。
def _invalid_report(report_path: Path, workspace: Path, error: dict[str, str]) -> MainAgentRealTaskExecutionReport:
    return build_invalid_revalidation_report(
        InvalidRevalidationReportRequest(
            report_path,
            workspace,
            error,
            SCHEMA_VERSION,
            MainAgentRealTaskExecutionCaseResult,
            MainAgentRealTaskExecutionReport,
        )
    )


# LLM: _revalidate_case reconstructs one case result from refs and artifact contracts.
# 函数用途: 不执行 command，只从 case_id/ref 找 expected_artifacts.json 和 workspace 后重跑验收。
def _revalidate_case(
    item: dict[str, object],
    *,
    workspace: Path,
    report_path: Path,
) -> MainAgentRealTaskExecutionCaseResult:
    case_id = str(item.get("case_id") or "")
    paths = resolve_case_paths(
        ResolveCasePathsRequest(
            item,
            workspace,
            case_id,
            case_paths(workspace, case_id),
            report_path,
            ("main_agent_task_execution",),
        )
    )
    acceptance = validate_real_task_artifacts(
        RealTaskAcceptanceRequest(
            expected_artifacts_path=workspace / expected_artifacts_ref(
                item,
                suite_root_ref="main_agent_real_task_suite",
            ),
            task_workspace=paths["workspace"],
            report_path=paths["acceptance_report"],
        )
    )
    return MainAgentRealTaskExecutionCaseResult(
        case_id=case_id,
        title=str(item.get("title") or ""),
        status="DONE" if acceptance.ok else "FAILED",
        worker_slot=int(item.get("worker_slot") or 0),
        timeout_seconds=int(item.get("timeout_seconds") or 0),
        prompt_ref=str(item.get("prompt_ref") or ""),
        config_ref=str(item.get("config_ref") or relative_ref(paths["config"], workspace)),
        command_ref=str(item.get("command_ref") or relative_ref(paths["command"], workspace)),
        stdout_ref=str(item.get("stdout_ref") or relative_ref(paths["stdout"], workspace)),
        stderr_ref=str(item.get("stderr_ref") or relative_ref(paths["stderr"], workspace)),
        acceptance_report_ref=relative_ref(paths["acceptance_report"], workspace),
        events_ref=str(item.get("events_ref") or relative_ref(paths["events"], workspace)),
        acceptance_summary=dict(acceptance.summary),
        exit_code=optional_int(item.get("exit_code")),
        duration_seconds=float(item.get("duration_seconds") or 0.0),
        issues=case_issues(int(item.get("exit_code") or 0), acceptance.summary),
    )


__all__ = ["revalidate_main_agent_real_task_execution"]
