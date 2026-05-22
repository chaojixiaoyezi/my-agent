# LLM: Real task revalidation re-checks artifacts from a stored execution report.
# 模块用途: 读取已有真实任务执行报告，只复验产物合同，不重新启动模型或工具进程。

from __future__ import annotations

import json
from pathlib import Path

from .main_agent_task_acceptance import (
    TaskRunAcceptanceRequest,
    validate_task_artifacts,
)
from .main_agent_task_execution_files import case_paths, rel, write_json
from .main_agent_task_execution_models import (
    SCHEMA_VERSION,
    MainAgentTaskExecutionCaseResult,
    MainAgentTaskExecutionReport,
)
from .state_machine import normalize_status


# LLM: revalidate_main_agent_task_execution re-checks artifacts without rerunning commands.
# 函数用途: 读取已有 execution_report.json，只复验 expected artifact 合同并重写验收报告。
def revalidate_main_agent_task_execution(
    report_path: Path,
    *,
    workspace: Path | None = None,
) -> MainAgentTaskExecutionReport:
    base = Path(workspace).expanduser().resolve() if workspace else Path(report_path).parent.parent
    payload, load_error = _read_report_payload(Path(report_path))
    if load_error:
        report = _invalid_report(Path(report_path), base, load_error)
        write_json(Path(report_path), report.to_dict())
        return report
    cases = [_revalidate_case(item, workspace=base) for item in _payload_cases(payload)]
    report = MainAgentTaskExecutionReport(
        ok=not any(case.status == "FAILED" for case in cases),
        schema_version=SCHEMA_VERSION,
        execution_mode="revalidate",
        summary=_summary(cases),
        concurrency=_payload_concurrency(payload, cases),
        suite_report_ref=str(payload.get("suite_report_ref") or ""),
        report_ref=rel(Path(report_path), base),
        cases=cases,
    )
    write_json(Path(report_path), report.to_dict())
    return report


# LLM: _read_report_payload turns corrupt report files into a structured error record.
# 函数用途: 读取 execution_report.json；坏 JSON/非对象报告不抛原始异常，交给 revalidate 写失败报告。
def _read_report_payload(report_path: Path) -> tuple[dict[str, object], dict[str, str] | None]:
    try:
        payload = json.loads(report_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return {}, {"code": "REVALIDATION_REPORT_INVALID_JSON", "message": exc.msg}
    except OSError as exc:
        return {}, {"code": "REVALIDATION_REPORT_UNREADABLE", "message": str(exc)}
    if not isinstance(payload, dict):
        return {}, {"code": "REVALIDATION_REPORT_NOT_OBJECT", "message": "execution report must be a JSON object"}
    return payload, None


# LLM: _invalid_report preserves the public report shape when revalidation cannot read the old report.
# 函数用途: 用一个 synthetic failed case 承载读报告失败码，避免 CLI/恢复链路被异常截断。
def _invalid_report(report_path: Path, workspace: Path, error: dict[str, str]) -> MainAgentTaskExecutionReport:
    case = MainAgentTaskExecutionCaseResult(
        case_id="__report__",
        title="Invalid execution report",
        status="FAILED",
        worker_slot=0,
        timeout_seconds=0,
        prompt_ref="",
        config_ref="",
        command_ref="",
        stdout_ref="",
        stderr_ref="",
        acceptance_report_ref="",
        events_ref="",
        acceptance_summary={"passed": 0, "failed": 1},
        issues=[str(error.get("code") or "REVALIDATION_REPORT_INVALID")],
    )
    return MainAgentTaskExecutionReport(
        ok=False,
        schema_version=SCHEMA_VERSION,
        execution_mode="revalidate",
        summary=_summary([case]),
        concurrency={"requested_max_workers": 1, "effective_max_workers": 1, "case_count": 1},
        suite_report_ref="",
        report_ref=rel(report_path, workspace),
        cases=[case],
    )


# LLM: _revalidate_case reconstructs one case result from refs and artifact contracts.
# 函数用途: 不执行 command，只从 case_id/ref 找 expected_artifacts.json 和 workspace 后重跑验收。
def _revalidate_case(
    item: dict[str, object],
    *,
    workspace: Path,
) -> MainAgentTaskExecutionCaseResult:
    case_id = str(item.get("case_id") or "")
    paths = _resolved_case_paths(item, workspace, case_id)
    acceptance = validate_task_artifacts(
        TaskRunAcceptanceRequest(
            expected_artifacts_path=workspace / _expected_artifacts_ref(item),
            task_workspace=paths["workspace"],
            report_path=paths["acceptance_report"],
        )
    )
    return MainAgentTaskExecutionCaseResult(
        case_id=case_id,
        title=str(item.get("title") or ""),
        status="DONE" if acceptance.ok else "FAILED",
        worker_slot=int(item.get("worker_slot") or 0),
        timeout_seconds=int(item.get("timeout_seconds") or 0),
        prompt_ref=str(item.get("prompt_ref") or ""),
        config_ref=str(item.get("config_ref") or rel(paths["config"], workspace)),
        command_ref=str(item.get("command_ref") or rel(paths["command"], workspace)),
        stdout_ref=str(item.get("stdout_ref") or rel(paths["stdout"], workspace)),
        stderr_ref=str(item.get("stderr_ref") or rel(paths["stderr"], workspace)),
        acceptance_report_ref=rel(paths["acceptance_report"], workspace),
        events_ref=str(item.get("events_ref") or rel(paths["events"], workspace)),
        acceptance_summary=dict(acceptance.summary),
        exit_code=_optional_int(item.get("exit_code")),
        duration_seconds=float(item.get("duration_seconds") or 0.0),
        issues=_case_issues(int(item.get("exit_code") or 0), acceptance.summary),
    )


# LLM: _expected_artifacts_ref derives the structured artifact contract ref from the prompt ref.
# 函数用途: 根据 suite 目录结构定位 expected_artifacts.json，不解析 prompt 自然语言。
def _expected_artifacts_ref(item: dict[str, object]) -> str:
    prompt_ref = Path(str(item.get("prompt_ref") or ""))
    if prompt_ref.name == "prompt.md":
        return str(prompt_ref.with_name("expected_artifacts.json"))
    case_id = str(item.get("case_id") or "")
    return f"main_agent_task_suite/tasks/{case_id}/expected_artifacts.json"


# LLM: _payload_cases returns only dict case records from a stored report.
# 函数用途: 容忍旧报告字段缺失，复验入口只处理结构化 cases 列表。
def _payload_cases(payload: object) -> list[dict[str, object]]:
    cases = payload.get("cases") if isinstance(payload, dict) else None
    return [dict(item) for item in cases] if isinstance(cases, list) else []


# LLM: _payload_concurrency preserves old concurrency metadata during revalidation.
# 函数用途: 复验报告沿用已有并发事实；旧报告没有该字段时生成最小摘要。
def _payload_concurrency(
    payload: object,
    cases: list[MainAgentTaskExecutionCaseResult],
) -> dict[str, int]:
    value = payload.get("concurrency") if isinstance(payload, dict) else None
    if isinstance(value, dict):
        return {str(key): int(raw) for key, raw in value.items() if isinstance(raw, int)}
    return {"requested_max_workers": 1, "effective_max_workers": 1, "case_count": len(cases)}


# LLM: _case_issues creates short issue codes from process and artifact facts.
# 函数用途: 复验时重算产物失败摘要；详细 findings 仍在 acceptance_report_ref。
def _case_issues(exit_code: int, summary: dict[str, int]) -> list[str]:
    issues: list[str] = []
    failed = int(summary.get("failed", 0))
    if exit_code == 124 and not failed:
        return ["process_timeout_after_valid_artifact"]
    if exit_code != 0:
        issues.append(f"exit_code={exit_code}")
    if failed:
        issues.append(f"artifact_acceptance_failed={failed}")
    return issues


# LLM: _optional_int keeps restored exit_code compatible with JSON null values.
# 函数用途: 从旧报告恢复可选退出码，非整数值返回 None。
def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) else None


# LLM: _summary counts planned and executed case statuses for CLI display.
# 函数用途: 汇总复验报告状态，和执行报告保持同一 summary shape。
def _summary(cases: list[MainAgentTaskExecutionCaseResult]) -> dict[str, int]:
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


# LLM: _resolved_case_paths prefers current generic roots but falls back to legacy task roots when revalidating old reports.
# 函数用途: 兼容旧执行报告里的真实任务目录，让通用复验合同可以直接复用历史产物和日志。
def _resolved_case_paths(item: dict[str, object], workspace: Path, case_id: str) -> dict[str, Path]:
    paths = case_paths(workspace, case_id)
    if paths["workspace"].exists() or paths["root"].exists():
        return paths
    legacy_root = workspace / "main_agent_real_task_execution" / "tasks" / case_id
    if not legacy_root.exists():
        return paths
    return {
        "root": legacy_root,
        "workspace": legacy_root / "workspace",
        "config": legacy_root / "config.yaml",
        "command": legacy_root / "command.json",
        "delivery_contract": legacy_root / "delivery_contract.json",
        "stdout": legacy_root / "stdout.txt",
        "stderr": legacy_root / "stderr.txt",
        "acceptance_report": legacy_root / "acceptance_report.json",
        "recovery_packet": legacy_root / "recovery_packet.json",
        "events": legacy_root / "events.jsonl",
    }


__all__ = ["revalidate_main_agent_task_execution"]
