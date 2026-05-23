# LLM: Main-agent task runtime executes task-like suites through one orchestration path.
# 模块用途: 统一 task/real_task 计划、执行、验收、恢复和报告流程，轨道差异由 adapter 提供。

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Any

from .main_agent_auto_resume import (
    auto_resume_decision,
    auto_resume_remaining_timeout_seconds,
    record_auto_resume_attempt,
)
from .main_agent_task_runtime_adapter import MainAgentTaskRuntimeAdapter
from .state_machine import normalize_status


# LLM: run_main_agent_task_runtime is the shared controlled runner.
# 函数用途: 根据 adapter 生成套件、选择 case、并发准备/执行，并写 refs-first 执行报告。
def run_main_agent_task_runtime(
    request: Any,
    *,
    runtime_adapter: MainAgentTaskRuntimeAdapter,
) -> Any:
    workspace = Path(request.workspace).expanduser().resolve()
    suite = _plan_suite(request, workspace, runtime_adapter)
    selected = _select_cases(
        suite.cases,
        runtime_adapter.case_ids_for_recovery_request(request.case_ids, request.recovery_packet_path),
    )
    results = _run_selected_cases(selected, request, workspace, runtime_adapter=runtime_adapter)
    report_path = runtime_adapter.execution_root(workspace) / "execution_report.json"
    report = runtime_adapter.report_model(
        ok=not any(normalize_status(case.status) == "FAILED" for case in results),
        schema_version=runtime_adapter.schema_version,
        execution_mode="execute" if request.execute else "plan_only",
        summary=_execution_summary(results),
        concurrency=_concurrency_summary(request, selected),
        suite_report_ref=suite.report_ref,
        report_ref=runtime_adapter.rel(report_path, workspace),
        cases=results,
    )
    runtime_adapter.write_json(report_path, report.to_dict())
    return report


# LLM: prepare_or_execute_case materializes one case then optionally runs it.
# 函数用途: 写配置、命令、事件和 runtime facts；execute=False 时只返回 PLANNING 结果。
def prepare_or_execute_case(
    case: Any,
    request: Any,
    *,
    workspace: Path,
    runtime_adapter: MainAgentTaskRuntimeAdapter,
) -> Any:
    _ensure_case_contract_refs(case, request, runtime_adapter)
    paths = runtime_adapter.resume_attempt_paths(
        runtime_adapter.case_paths(workspace, case.case_id),
        request.recovery_packet_path,
    )
    runtime_adapter.write_case_config(paths["config"], request.base_config_path, paths["workspace"])
    command = runtime_adapter.command_for_case(
        case,
        request,
        config_path=paths["config"],
        workspace=workspace,
        delivery_contract_path=paths["delivery_contract"],
    )
    runtime_adapter.write_json(paths["command"], {"argv": command, "cwd": str(runtime_adapter.package_root(request))})
    runtime_adapter.append_event(paths["events"], "case_prepared", {"case_id": case.case_id})
    runtime = runtime_adapter.case_runtime_model(case, request, paths, command, workspace)
    if not request.execute:
        return runtime_adapter.case_result(runtime_adapter.case_result_bundle_model(runtime, "PLANNING"))
    return run_case(runtime, runtime_adapter=runtime_adapter)


# LLM: run_case executes the subprocess without shell expansion.
# 函数用途: 启动一次主代理 run，捕获输出文件；超时和非零退出都进入结构化 issues。
def run_case(runtime: Any, *, runtime_adapter: MainAgentTaskRuntimeAdapter) -> Any:
    runtime_adapter.append_event(runtime.paths["events"], "case_started", {"case_id": runtime.case.case_id})
    process = runtime_adapter.run_subprocess(_subprocess_request(runtime, runtime_adapter))
    if process.timed_out:
        return timeout_case_result(runtime, process, runtime_adapter=runtime_adapter)
    return completed_case_result(runtime, process, runtime_adapter=runtime_adapter)


# LLM: completed_case_result converts subprocess completion into report facts.
# 函数用途: 写验收事件、生成 issue、写恢复包，必要时按结构化预算自动续跑。
def completed_case_result(
    runtime: Any,
    process: Any,
    *,
    runtime_adapter: MainAgentTaskRuntimeAdapter,
) -> Any:
    runtime_adapter.append_event(
        runtime.paths["events"],
        "case_finished",
        {"case_id": runtime.case.case_id, "exit_code": process.exit_code},
    )
    acceptance = runtime_adapter.validate_case_artifacts(runtime)
    status = "DONE" if process.exit_code == 0 and acceptance.ok else "FAILED"
    runtime_adapter.append_acceptance_event(runtime, acceptance)
    issues = runtime_adapter.case_issues(runtime, process.exit_code, acceptance)
    return _finalize_case_bundle(
        runtime_adapter.case_result_bundle_model(runtime, status, acceptance, process.exit_code, process.duration_seconds, issues),
        runtime_adapter,
    )


# LLM: timeout_case_result keeps timeout handling separate from normal completion.
# 函数用途: 超时后仍复验已有产物；有效产物可通过，缺失产物会写恢复包。
def timeout_case_result(
    runtime: Any,
    process: Any,
    *,
    runtime_adapter: MainAgentTaskRuntimeAdapter,
) -> Any:
    acceptance = runtime_adapter.validate_case_artifacts(runtime)
    event_type = "case_timeout" if process.timeout_reason == "timeout" else "case_activity_timeout"
    runtime_adapter.append_event(
        runtime.paths["events"],
        event_type,
        {"case_id": runtime.case.case_id, "timeout_reason": process.timeout_reason or "timeout"},
    )
    runtime_adapter.append_acceptance_event(runtime, acceptance)
    status = "DONE" if acceptance.ok else "FAILED"
    issues = runtime_adapter.timeout_issues(acceptance, timeout_reason=process.timeout_reason)
    return _finalize_case_bundle(
        runtime_adapter.case_result_bundle_model(runtime, status, acceptance, 124, process.duration_seconds, issues),
        runtime_adapter,
    )


# LLM: _run_selected_cases honors max_workers while keeping each case isolated.
# 函数用途: 并发执行结构化 case 列表，不共享 workspace/config/log 文件。
def _run_selected_cases(
    cases: list[Any],
    request: Any,
    workspace: Path,
    *,
    runtime_adapter: MainAgentTaskRuntimeAdapter,
) -> list[Any]:
    max_workers = max(1, min(request.max_workers, len(cases) or 1))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        return list(
            executor.map(
                lambda case: prepare_or_execute_case(case, request, workspace=workspace, runtime_adapter=runtime_adapter),
                cases,
            )
        )


# LLM: _finalize_case_bundle centralizes recovery packet and auto-resume behavior.
# 函数用途: 对所有轨道统一写恢复包、判断自动恢复预算、返回最终 case result。
def _finalize_case_bundle(bundle: Any, runtime_adapter: MainAgentTaskRuntimeAdapter) -> Any:
    bundle.recovery_packet_ref = runtime_adapter.write_recovery_packet_ref(bundle)
    if auto_resume_decision(bundle).allowed:
        return _auto_resume_case(bundle, runtime_adapter)
    return runtime_adapter.case_result(bundle)


# LLM: _auto_resume_case re-enters the same runtime with a recovery packet.
# 函数用途: 失败后记录 attempt ledger，再用同一 case/workspace 和结构化恢复包续跑。
def _auto_resume_case(bundle: Any, runtime_adapter: MainAgentTaskRuntimeAdapter) -> Any:
    runtime = bundle.runtime
    packet_path = (runtime.workspace / bundle.recovery_packet_ref).resolve()
    next_timeout_seconds = auto_resume_remaining_timeout_seconds(bundle)
    ledger = record_auto_resume_attempt(bundle)
    runtime_adapter.append_event(
        runtime.paths["events"],
        "case_auto_resume_started",
        {
            "case_id": runtime.case.case_id,
            "recovery_packet_ref": bundle.recovery_packet_ref,
            "attempts": ledger.get("attempts"),
            "timeout_seconds": next_timeout_seconds,
        },
    )
    return prepare_or_execute_case(
        runtime.case,
        replace(
            runtime.request,
            recovery_packet_path=packet_path,
            auto_recovery_active=True,
            task_timeout_seconds=next_timeout_seconds,
        ),
        workspace=runtime.workspace,
        runtime_adapter=runtime_adapter,
    )


# LLM: _subprocess_request maps runtime facts into the adapter process request model.
# 函数用途: 统一构造命令、cwd、stdout/stderr、workspace 和 timeout 参数。
def _subprocess_request(runtime: Any, runtime_adapter: MainAgentTaskRuntimeAdapter) -> Any:
    return runtime_adapter.subprocess_request_model(
        runtime.command,
        cwd=runtime_adapter.package_root(runtime.request),
        stdout_path=runtime.paths["stdout"],
        stderr_path=runtime.paths["stderr"],
        workspace=runtime.paths["workspace"],
        timeout_seconds=runtime.request.task_timeout_seconds,
        activity_timeout_seconds=runtime_adapter.activity_timeout_seconds(runtime.request.task_timeout_seconds),
    )


# LLM: _ensure_case_contract_refs re-materializes missing structured suite refs.
# 函数用途: 续跑或旧工作区缺文件时按 suite 重新落 prompt/acceptance/expected_artifacts。
def _ensure_case_contract_refs(
    case: Any,
    request: Any,
    runtime_adapter: MainAgentTaskRuntimeAdapter,
) -> None:
    workspace = Path(request.workspace).expanduser().resolve()
    refs = [case.prompt_ref, case.acceptance_ref, case.expected_artifacts_ref]
    if all((workspace / ref).exists() for ref in refs if ref):
        return
    _plan_suite(request, workspace, runtime_adapter)


# LLM: _plan_suite delegates suite generation to the selected track adapter.
# 函数用途: 用相同字段生成 task 或 real_task 的套件请求，避免两套执行器重复。
def _plan_suite(request: Any, workspace: Path, runtime_adapter: MainAgentTaskRuntimeAdapter) -> Any:
    return runtime_adapter.plan_suite(
        runtime_adapter.suite_request_model(
            workspace=workspace,
            max_workers=request.max_workers,
            task_timeout_seconds=request.task_timeout_seconds,
            execute=request.execute,
            prompt_overrides=dict(getattr(request, "prompt_overrides", {}) or {}),
        )
    )


# LLM: _select_cases filters by case_id structure only.
# 函数用途: 空 case_ids 表示全量；非空只选择匹配 case_id 的结构化 case。
def _select_cases(cases: list[Any], case_ids: tuple[str, ...]) -> list[Any]:
    if not case_ids:
        return cases
    allowed = set(case_ids)
    return [case for case in cases if case.case_id in allowed]


# LLM: _execution_summary normalizes status strings across legacy tracks.
# 函数用途: 同时输出 planning/planned 和 done/completed，兼容旧报告消费方。
def _execution_summary(cases: list[Any]) -> dict[str, int]:
    statuses = [normalize_status(case.status) for case in cases]
    planning = sum(status == "PLANNING" for status in statuses)
    done = sum(status == "DONE" for status in statuses)
    return {
        "total": len(cases),
        "planning": planning,
        "planned": planning,
        "done": done,
        "completed": done,
        "failed": sum(status == "FAILED" for status in statuses),
    }


# LLM: _concurrency_summary stores requested and effective worker limits.
# 函数用途: 让后续观察并发主代理任务时可以直接读机器字段。
def _concurrency_summary(request: Any, cases: list[Any]) -> dict[str, int]:
    return {
        "requested_max_workers": request.max_workers,
        "effective_max_workers": max(1, min(request.max_workers, len(cases) or 1)),
        "case_count": len(cases),
    }


__all__ = [
    "completed_case_result",
    "prepare_or_execute_case",
    "run_case",
    "run_main_agent_task_runtime",
    "timeout_case_result",
]
