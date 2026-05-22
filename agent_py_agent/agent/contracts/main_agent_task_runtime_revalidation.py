# LLM: Main-agent task runtime revalidation shares report replay across task tracks.
# 模块用途: 统一读取 execution_report、解析 case refs、复验产物并重写报告。

from __future__ import annotations

from pathlib import Path
from typing import Any

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
from .main_agent_task_runtime_adapter import MainAgentTaskRuntimeAdapter


# LLM: revalidate_main_agent_task_runtime re-checks artifacts without rerunning commands.
# 函数用途: 读取已有 execution_report.json，只根据结构化 refs 和 expected artifacts 复验。
def revalidate_main_agent_task_runtime(
    report_path: Path,
    *,
    workspace: Path | None = None,
    runtime_adapter: MainAgentTaskRuntimeAdapter,
) -> Any:
    report_path = Path(report_path)
    base = Path(workspace).expanduser().resolve() if workspace else report_path.parent.parent
    payload, load_error = read_report_payload(report_path)
    if load_error:
        report = _invalid_report(report_path, base, load_error, runtime_adapter)
        runtime_adapter.write_json(report_path, report.to_dict())
        return report
    cases = [_revalidate_case(item, base, report_path, runtime_adapter) for item in payload_cases(payload)]
    report = runtime_adapter.report_model(
        ok=not any(case.status == "FAILED" for case in cases),
        schema_version=runtime_adapter.schema_version,
        execution_mode="revalidate",
        summary=status_summary(cases),
        concurrency=payload_concurrency(payload, len(cases)),
        suite_report_ref=str(payload.get("suite_report_ref") or ""),
        report_ref=relative_ref(report_path, base),
        cases=cases,
    )
    runtime_adapter.write_json(report_path, report.to_dict())
    return report


# LLM: _invalid_report creates a stable failed report for unreadable JSON.
# 函数用途: 坏 execution_report 不抛异常，而是落结构化 revalidation_failed case。
def _invalid_report(
    report_path: Path,
    workspace: Path,
    error: dict[str, str],
    runtime_adapter: MainAgentTaskRuntimeAdapter,
) -> Any:
    return build_invalid_revalidation_report(
        InvalidRevalidationReportRequest(
            report_path,
            workspace,
            error,
            runtime_adapter.schema_version,
            runtime_adapter.case_result_model,
            runtime_adapter.report_model,
        )
    )


# LLM: _revalidate_case reconstructs one case result from refs and artifact contracts.
# 函数用途: 不执行 command，只从 case_id/ref 找 expected_artifacts.json 和 workspace 后重跑验收。
def _revalidate_case(
    item: dict[str, object],
    workspace: Path,
    report_path: Path,
    runtime_adapter: MainAgentTaskRuntimeAdapter,
) -> Any:
    case_id = str(item.get("case_id") or "")
    paths = resolve_case_paths(
        ResolveCasePathsRequest(
            item,
            workspace,
            case_id,
            runtime_adapter.case_paths(workspace, case_id),
            report_path,
            runtime_adapter.compatible_execution_roots,
        )
    )
    acceptance = runtime_adapter.validate_case_artifacts(
        runtime_adapter.case_runtime_model(
            _CaseRef(case_id, item, runtime_adapter.suite_root_ref),
            _RequestRef(workspace),
            paths,
            [],
            workspace,
        )
    )
    return runtime_adapter.case_result_model(
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


# LLM: _CaseRef supplies only structured fields needed by acceptance helpers.
# 类用途: 让 runtime revalidation 复用 validate_case_artifacts，不构造自然语言事实。
class _CaseRef:
    def __init__(self, case_id: str, item: dict[str, object], suite_root_ref: str) -> None:
        self.case_id = case_id
        self.title = str(item.get("title") or "")
        self.worker_slot = int(item.get("worker_slot") or 0)
        self.timeout_seconds = int(item.get("timeout_seconds") or 0)
        self.prompt_ref = str(item.get("prompt_ref") or "")
        self.expected_artifacts_ref = expected_artifacts_ref(item, suite_root_ref=suite_root_ref)


# LLM: _RequestRef is a minimal request object for revalidation-only code paths.
# 类用途: 提供 validate/recovery helper 可能读取的 workspace 字段，不携带执行控制。
class _RequestRef:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace


__all__ = ["revalidate_main_agent_task_runtime"]
