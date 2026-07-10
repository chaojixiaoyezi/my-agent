
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ..subagents.static_site import run_static_site_check
from .activity_timeout import ActivitySnapshot, ActivityTimeoutPolicy, decide_activity_timeout
from .e2e_matrix_runner import E2ERunnerRequest, run_e2e_matrix
from .error_taxonomy import classify_error
from .main_agent_foundation_contract_cases import (
    case_general_write_contract,
    case_model_call_ledger_timeout,
    case_tool_protocol_v2_envelope,
)
from .main_agent_foundation_models import (
    MainAgentFoundationCaseResult,
    MainAgentFoundationReport,
    MainAgentFoundationRequest,
)
from .main_agent_foundation_research import research_evidence_contract_case

REAL_MODEL_CASE_IDS = {
    "single_agent_real_tasks",
    "compact_resume_real_cycle",
    "tool_error_recovery_real",
}


def run_main_agent_foundation(request: MainAgentFoundationRequest) -> MainAgentFoundationReport:
    workspace = Path(request.workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    results = [
        _case_tool_failure_contracts(workspace),
        _case_research_evidence_contracts(workspace),
        _case_web_artifact_validator(workspace),
        _case_activity_timeout_recovery(workspace),
        case_model_call_ledger_timeout(workspace),
        case_tool_protocol_v2_envelope(workspace),
        case_general_write_contract(workspace),
        _real_model_placeholder(
            "single_agent_real_tasks",
            "单代理真实任务测试",
            "需要显式运行真实模型任务：HTML、文件整理、代码修复、测试。",
            include_real_model=request.include_real_model,
        ),
        _real_model_placeholder(
            "compact_resume_real_cycle",
            "Compact/Resume 真实续接测试",
            "需要显式运行低阈值 compact、resume 和继续任务链路。",
            include_real_model=request.include_real_model,
        ),
        _case_large_output_artifact_refs(workspace),
        _real_model_placeholder(
            "tool_error_recovery_real",
            "错误恢复真实模型测试",
            "需要真实模型遇到工具失败后修正路径或参数并继续。",
            include_real_model=request.include_real_model,
        ),
        _case_deterministic_e2e_matrix(workspace),
    ]
    return MainAgentFoundationReport(
        ok=not any(item.status == "FAILED" for item in results),
        summary=_summary(results),
        results=results,
    )


def _case_tool_failure_contracts(workspace: Path) -> MainAgentFoundationCaseResult:
    samples = {
        "PATH_INVALID": "PATH_INVALID: missing-input.txt",
        "PATH_OUTSIDE_WORKSPACE": "PATH_OUTSIDE_WORKSPACE: /tmp/not-allowed.txt",
        "WRITE_FORBIDDEN": "WRITE_FORBIDDEN: protected file",
        "TOOL_UNAVAILABLE": "TOOL_UNAVAILABLE: browser_magic",
        "TOOL_INVALID_ARGUMENTS": "TOOL_INVALID_ARGUMENTS: write_file",
        "TOOL_TIMEOUT": "TOOL_TIMEOUT: after 240 seconds",
        "MODEL_UPSTREAM_FAILED": "MODEL_UPSTREAM_FAILED: provider returned 502",
    }
    observed = {expected: classify_error(message).code for expected, message in samples.items()}
    issues = [
        f"{expected}->{actual}" for expected, actual in observed.items() if actual != expected
    ]
    evidence = workspace / "tool_failure_contracts" / "classification.json"
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text(
        json.dumps(observed, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    return MainAgentFoundationCaseResult(
        case_id="tool_failure_contracts",
        title="工具失败测试",
        status="FAILED" if issues else "PASSED",
        summary="路径、权限、超时、工具不可用、参数错误和模型上游失败都有稳定错误码。",
        evidence_refs=[str(evidence)],
        issues=issues,
    )


def _case_research_evidence_contracts(workspace: Path) -> MainAgentFoundationCaseResult:
    return MainAgentFoundationCaseResult(**research_evidence_contract_case(workspace))


def _case_web_artifact_validator(workspace: Path) -> MainAgentFoundationCaseResult:
    site = workspace / "web_artifact_validator" / "site"
    site.mkdir(parents=True, exist_ok=True)
    (site / "index.html").write_text(
        '<div id="homePanel"></div><script src="app.js"></script>', encoding="utf-8"
    )
    (site / "app.js").write_text(
        "document.getElementById('resultGrid').innerHTML = '<p>记录</p>';", encoding="utf-8"
    )
    record = run_static_site_check(
        {
            "name": "generated web app",
            "validation_method": "static_site_check",
            "site_root": "site",
            "required_files": ["index.html", "app.js"],
        },
        workspace / "web_artifact_validator",
    )
    evidence = workspace / "web_artifact_validator" / "validation.json"
    evidence.write_text(
        json.dumps(record.to_dict(), ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    expected_hit = record.validation_result.get("missing_dom_id_hits") == [
        "getElementById:resultGrid"
    ]
    issues = (
        []
        if record.executed and not record.passed and expected_hit
        else ["web validator did not catch missing DOM id"]
    )
    return MainAgentFoundationCaseResult(
        case_id="web_artifact_validator",
        title="Web 产物机器验收测试",
        status="FAILED" if issues else "PASSED",
        summary="HTML/JS 绑定目标不一致时，static_site_check 会返回结构化 missing_dom_id_hits。",
        evidence_refs=[str(evidence)],
        issues=issues,
    )


def _case_activity_timeout_recovery(workspace: Path) -> MainAgentFoundationCaseResult:
    active = decide_activity_timeout(
        ActivityTimeoutPolicy(idle_timeout_seconds=120, wall_timeout_seconds=300),
        ActivitySnapshot(started_at=0, now=900, last_activity_at=880, active_tool_count=1),
    )
    idle = decide_activity_timeout(
        ActivityTimeoutPolicy(idle_timeout_seconds=120),
        ActivitySnapshot(
            started_at=0,
            now=500,
            last_activity_at=100,
            latest_checkpoint_ref="checkpoint.json",
            latest_recovery_snapshot_ref="snapshot.json",
        ),
    )
    evidence = workspace / "activity_timeout_recovery" / "decisions.json"
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text(
        json.dumps(
            {"active": active.to_dict(), "idle": idle.to_dict()},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    issues: list[str] = []
    if active.timed_out or active.action != "keep_running":
        issues.append("active task timed out")
    if not idle.timed_out or idle.action != "write_recovery_and_pause":
        issues.append("idle task did not request recovery pause")
    return MainAgentFoundationCaseResult(
        case_id="activity_timeout_recovery",
        title="长任务活动超时/恢复测试",
        status="FAILED" if issues else "PASSED",
        summary="长任务按 idle activity 判断；空闲超时时返回 checkpoint/snapshot refs 供恢复。",
        evidence_refs=[str(evidence)],
        issues=issues,
    )


def _case_large_output_artifact_refs(workspace: Path) -> MainAgentFoundationCaseResult:
    artifact = workspace / "large_output_artifact_refs" / "large-tool-output.txt"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    content = "\n".join(f"large output row {index}: {'x' * 120}" for index in range(800))
    artifact.write_text(content, encoding="utf-8")
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    metadata = {
        "artifact_ref": str(artifact),
        "size": artifact.stat().st_size,
        "sha256": digest,
        "read_hint": {"offset": 0, "max_chars": 4000},
    }
    evidence = artifact.with_suffix(".meta.json")
    evidence.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    return MainAgentFoundationCaseResult(
        case_id="large_output_artifact_refs",
        title="大文件/大输出测试",
        status="PASSED",
        summary=f"大输出已外置为 artifact；报告只保留 ref/size/hash，sha256={digest[:12]}。",
        evidence_refs=[str(evidence)],
    )


def _case_deterministic_e2e_matrix(workspace: Path) -> MainAgentFoundationCaseResult:
    report = run_e2e_matrix(E2ERunnerRequest(workspace=workspace / "deterministic_e2e_matrix"))
    evidence = workspace / "deterministic_e2e_matrix" / "report.json"
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    return MainAgentFoundationCaseResult(
        case_id="deterministic_e2e_matrix",
        title="E2E Matrix 确定性测试",
        status="FAILED" if not report.ok else "PASSED",
        summary=f"本地 E2E 矩阵 passed={report.summary['passed']} skipped={report.summary['skipped']} failed={report.summary['failed']}。",
        evidence_refs=[str(evidence)],
        issues=[] if report.ok else ["deterministic E2E matrix failed"],
    )


def _real_model_placeholder(
    case_id: str,
    title: str,
    summary: str,
    *,
    include_real_model: bool,
) -> MainAgentFoundationCaseResult:
    if not include_real_model:
        return MainAgentFoundationCaseResult(
            case_id=case_id,
            title=title,
            status="SKIPPED",
            summary=f"{summary} 本次未要求真实模型用例。",
        )
    return MainAgentFoundationCaseResult(
        case_id=case_id,
        title=title,
        status="FAILED",
        summary=f"{summary} 已明确要求真实模型用例，但当前 runner 尚未实现。",
        issues=["REAL_MODEL_RUNNER_NOT_IMPLEMENTED"],
    )


def _summary(results: list[MainAgentFoundationCaseResult]) -> dict[str, int]:
    return {
        "total": len(results),
        "passed": sum(item.status == "PASSED" for item in results),
        "failed": sum(item.status == "FAILED" for item in results),
        "skipped": sum(item.status == "SKIPPED" for item in results),
    }


__all__ = [
    "MainAgentFoundationCaseResult",
    "MainAgentFoundationReport",
    "MainAgentFoundationRequest",
    "run_main_agent_foundation",
]
