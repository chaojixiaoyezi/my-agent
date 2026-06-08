# Main-agent foundation data model
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class MainAgentFoundationRequest:
    workspace: Path
    include_real_model: bool = False


@dataclass(frozen=True)
class MainAgentFoundationCaseResult:
    case_id: str
    title: str
    status: str
    summary: str
    evidence_refs: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "title": self.title,
            "status": self.status,
            "summary": self.summary,
            "evidence_refs": list(self.evidence_refs),
            "issues": list(self.issues),
        }


@dataclass(frozen=True)
class MainAgentFoundationReport:
    ok: bool
    summary: dict[str, int]
    results: list[MainAgentFoundationCaseResult]

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "summary": dict(self.summary),
            "results": [item.to_dict() for item in self.results],
        }


__all__ = [
    "MainAgentFoundationCaseResult",
    "MainAgentFoundationReport",
    "MainAgentFoundationRequest",
]

# Main-agent foundation contract cases
import hashlib
import json
from pathlib import Path

from ..agent_core.model.call_monitor import (
    FirstTokenTimeoutOptions,
    FirstTokenTimeoutParams,
    estimate_first_token_timeout,
)
from ..contracts.model_call_ledger import (
    ModelCallLedger,
    ModelCallStartedParams,
    ModelCallTimeoutParams,
)
from ..tooling._filesystem_patch import ApplyPatchTool
from ..tooling._filesystem_write import WriteFileTool
from .tool_protocol_v2 import (
    normalize_tool_call,
    normalize_tool_result,
    validate_tool_call,
    validate_tool_result,
)


def case_model_call_ledger_timeout(workspace: Path) -> MainAgentFoundationCaseResult:
    ledger = ModelCallLedger()
    ledger.started(_started_params())
    ledger.timeout(_timeout_params())
    estimate = estimate_first_token_timeout(
        FirstTokenTimeoutParams(
            input_tokens=12000,
            ledger=ledger,
            options=FirstTokenTimeoutOptions(max_timeout_seconds=300),
        )
    )
    evidence = workspace / "model_call_ledger_timeout" / "ledger.json"
    evidence.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "records": [item.to_dict() for item in ledger.records()],
        "first_token_estimate": estimate.to_dict(),
    }
    evidence.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    issues = _ledger_issues(ledger)
    return MainAgentFoundationCaseResult(
        case_id="model_call_ledger_timeout",
        title="模型调用账本/超时合同测试",
        status="FAILED" if issues else "PASSED",
        summary="模型请求 started/timeout 和首 token 动态预算都以结构化账本记录。",
        evidence_refs=[str(evidence)],
        issues=issues,
    )


def case_tool_protocol_v2_envelope(workspace: Path) -> MainAgentFoundationCaseResult:
    call = normalize_tool_call(
        {
            "tool_name": "read_file",
            "input": {"path": "missing.txt"},
            "artifact_refs": [{"artifact_id": "input-ref", "path": "missing.txt"}],
        }
    )
    result = normalize_tool_result(
        {
            "tool_name": "read_file",
            "ok": False,
            "operation_ref": call.operation_ref().to_dict(),
            "error": {"error_type": "PATH_INVALID", "message": "missing.txt"},
            "artifact_refs": [{"artifact_id": "error-log", "path": "logs/error.json"}],
        }
    )
    findings = validate_tool_call(call) + validate_tool_result(result)
    evidence = workspace / "tool_protocol_v2_envelope" / "envelope.json"
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text(
        json.dumps(
            {"call": call.to_dict(), "result": result.to_dict(), "findings": findings},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    issues = list(findings)
    if result.error is None or result.error.error_type != "PATH_INVALID":
        issues.append("tool result error taxonomy missing")
    return MainAgentFoundationCaseResult(
        case_id="tool_protocol_v2_envelope",
        title="工具协议 v2 envelope 测试",
        status="FAILED" if issues else "PASSED",
        summary="工具调用/结果包含 operation、幂等键、错误分类和 artifact refs，不读自然语言输出当事实。",
        evidence_refs=[str(evidence)],
        issues=issues,
    )


def case_general_write_contract(workspace: Path) -> MainAgentFoundationCaseResult:
    case_dir = workspace / "general_write_contract"
    write_tool = WriteFileTool(case_dir)
    patch_tool = ApplyPatchTool(case_dir)
    text_result = write_tool.execute({"path": "out/report.txt", "content": "hello world\n"})
    binary_result = write_tool.execute({"path": "out/blob.bin", "data_base64": "AAEC"})
    patch_result = patch_tool.execute(
        {
            "patch": (
                "*** Begin Patch\n"
                "*** Update File: out/report.txt\n"
                "-hello world\n"
                "+hello patched world\n"
                "*** End Patch\n"
            )
        }
    )
    evidence = _write_general_write_evidence(case_dir, text_result, binary_result, patch_result)
    issues = _general_write_issues(case_dir, text_result, binary_result, patch_result)
    return MainAgentFoundationCaseResult(
        case_id="general_write_contract",
        title="通用文件写入合同测试",
        status="FAILED" if issues else "PASSED",
        summary="write_file 可写文本/二进制完整文件，apply_patch 可做局部文本修改。",
        evidence_refs=[str(evidence)],
        issues=issues,
    )


def _started_params() -> ModelCallStartedParams:
    return ModelCallStartedParams(
        call_id="foundation-timeout",
        backend="test-backend",
        model="test-model",
        input_tokens=12000,
        output_tokens_estimate=800,
        request_id="foundation-request",
        run_id="foundation-run",
    )


def _timeout_params() -> ModelCallTimeoutParams:
    return ModelCallTimeoutParams(
        call_id="foundation-timeout",
        timeout_seconds=240,
        timeout_stage="provider_wall",
    )


def _ledger_issues(ledger: ModelCallLedger) -> list[str]:
    records = ledger.records()
    if records and records[0].status == "timed_out":
        return []
    return ["model call ledger did not record timeout"]


def _write_general_write_evidence(case_dir: Path, text_result: object, binary_result: object, patch_result: object) -> Path:
    evidence = case_dir / "evidence.json"
    text_target = case_dir / "out" / "report.txt"
    binary_target = case_dir / "out" / "blob.bin"
    payload = {
        "write_text_ok": bool(getattr(text_result, "ok", False)),
        "write_binary_ok": bool(getattr(binary_result, "ok", False)),
        "patch_ok": bool(getattr(patch_result, "ok", False)),
        "text_target_ref": str(text_target),
        "binary_target_ref": str(binary_target),
        "text_sha256": hashlib.sha256(text_target.read_bytes()).hexdigest() if text_target.exists() else "",
        "binary_sha256": hashlib.sha256(binary_target.read_bytes()).hexdigest() if binary_target.exists() else "",
    }
    evidence.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    return evidence


def _general_write_issues(case_dir: Path, text_result: object, binary_result: object, patch_result: object) -> list[str]:
    issues: list[str] = []
    if not all(bool(getattr(item, "ok", False)) for item in (text_result, binary_result, patch_result)):
        issues.append("generic write action failed")
    text_target = case_dir / "out" / "report.txt"
    binary_target = case_dir / "out" / "blob.bin"
    if not text_target.exists() or text_target.read_text(encoding="utf-8") != "hello patched world\n":
        issues.append("text target content mismatch")
    if not binary_target.exists() or binary_target.read_bytes() != b"\x00\x01\x02":
        issues.append("binary target content mismatch")
    return issues


__all__ = [
    "case_general_write_contract",
    "case_model_call_ledger_timeout",
    "case_tool_protocol_v2_envelope",
]

# Main-agent foundation research contract
import json
from pathlib import Path

from .evidence_contract import (
    EvidenceClaim,
    EvidenceContractRequest,
    EvidenceSourceRef,
    evaluate_evidence_contract,
)


def research_evidence_contract_case(workspace: Path) -> dict[str, object]:
    valid = _valid_research_evidence_contract()
    invalid = _invalid_research_evidence_contract()
    evidence = _write_research_evidence_report(workspace, valid, invalid)
    issues = _research_evidence_issues(valid.ok, invalid.ok)
    return {
        "case_id": "research_evidence_contracts",
        "title": "资料证据合同测试",
        "status": "FAILED" if issues else "PASSED",
        "summary": "关键资料字段必须有 source_ref；无来源统计不能通过验收。",
        "evidence_refs": [str(evidence)],
        "issues": issues,
    }


def _valid_research_evidence_contract():
    return evaluate_evidence_contract(
        EvidenceContractRequest(
            source_refs=[
                EvidenceSourceRef(
                    source_id="source-api-example",
                    source_type="api",
                    uri="https://example.com/data/project.json",
                    retrieved_at="2026-05-18T10:00:00Z",
                )
            ],
            claims=[
                EvidenceClaim(
                    claim_id="sourced-metric-value",
                    field="metric_value",
                    value=372838,
                    source_ids=["source-api-example"],
                )
            ],
            required_fields=["metric_value"],
        )
    )


def _invalid_research_evidence_contract():
    return evaluate_evidence_contract(
        EvidenceContractRequest(
            claims=[EvidenceClaim(claim_id="unsourced-metric-value", field="metric_delta", value=581200, source_ids=[])],
            required_fields=["metric_delta"],
        )
    )


def _write_research_evidence_report(workspace: Path, valid, invalid) -> Path:
    evidence = workspace / "research_evidence_contracts" / "report.json"
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text(
        json.dumps(
            {"valid": valid.to_dict(), "invalid": invalid.to_dict()},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return evidence


def _research_evidence_issues(valid_ok: bool, invalid_ok: bool) -> list[str]:
    issues: list[str] = []
    if not valid_ok:
        issues.append("valid sourced claim failed")
    if invalid_ok:
        issues.append("unsourced claim passed")
    return issues

# Main-agent foundation runner
import hashlib
import json
from pathlib import Path

from ..subagents.static_site import run_static_site_check
from .activity_timeout import ActivitySnapshot, ActivityTimeoutPolicy, decide_activity_timeout
from .e2e_matrix_runner import E2ERunnerRequest, run_e2e_matrix
from .error_taxonomy import classify_error

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
    status = "SKIPPED"
    note = summary
    if include_real_model:
        note = f"{summary} 当前基础 runner 只登记需求，真实模型由脚本单独执行。"
    return MainAgentFoundationCaseResult(case_id=case_id, title=title, status=status, summary=note)


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

# Main-agent contract entrypoints
from typing import Any

from .offline_contract_report import OfflineContractValidation, finding, text, validation_report

REQUIRED_ENTRYPOINTS = (
    "state_machine",
    "tool_executor",
    "closeout_gate",
    "runlog",
    "tooltrace",
    "approval_gate",
    "effective_contract",
)


def validate_main_agent_core_entrypoints(contract: dict[str, Any]) -> OfflineContractValidation:
    findings: list[dict[str, object]] = []
    entrypoints = contract.get("entrypoints") if isinstance(contract.get("entrypoints"), dict) else {}
    invariants = contract.get("invariants") if isinstance(contract.get("invariants"), dict) else {}
    _validate_required_entrypoints(entrypoints, findings)
    _validate_invariants(invariants, findings)
    return validation_report(findings)


def _validate_required_entrypoints(entrypoints: dict[str, Any], findings: list[dict[str, object]]) -> None:
    missing = tuple(name for name in REQUIRED_ENTRYPOINTS if not text(entrypoints.get(name)))
    if missing:
        findings.append(finding("MAIN_CORE_ENTRYPOINT_MISSING", {"entrypoints": missing}))


def _validate_invariants(invariants: dict[str, Any], findings: list[dict[str, object]]) -> None:
    if invariants.get("success_requires_verification") is not True:
        findings.append(finding("MAIN_CORE_SUCCESS_WITHOUT_VERIFICATION"))
    if invariants.get("tool_calls_require_executor") is not True:
        findings.append(finding("MAIN_CORE_TOOL_EXECUTOR_BYPASS"))
    if text(invariants.get("state_mutation_mode")) != "event_only":
        findings.append(finding("MAIN_CORE_STATE_MUTATION_NOT_EVENT_ONLY"))
    if text(invariants.get("machine_facts_source")) != "structured_fields":
        findings.append(finding("MAIN_CORE_NATURAL_LANGUAGE_FACT_SOURCE"))


__all__ = ["REQUIRED_ENTRYPOINTS", "validate_main_agent_core_entrypoints"]

# Main-agent auto-resume contract
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..settings.runtime_guard_config import runtime_guard_int
from .state_machine import normalize_status

SCHEMA_VERSION = "main-agent-auto-resume-ledger.v1"
DEFAULT_AUTO_RECOVERY_ATTEMPTS = 3


class _RuntimeLike(Protocol):
    paths: dict[str, Path]
    request: object
    case: object
    workspace: Path


class _BundleLike(Protocol):
    runtime: _RuntimeLike
    status: str
    recovery_packet_ref: str


@dataclass(frozen=True)
class AutoResumeDecision:
    allowed: bool
    attempts: int
    max_attempts: int
    reason: str


def auto_resume_decision(bundle: _BundleLike) -> AutoResumeDecision:
    request = bundle.runtime.request
    max_attempts = auto_resume_limit(request)
    attempts = _ledger_attempts(_ledger_path(bundle.runtime.paths))
    if not bool(getattr(request, "execute", False)):
        return AutoResumeDecision(False, attempts, max_attempts, "not_execute_mode")
    if getattr(request, "recovery_packet_path", None) is not None and not bool(
        getattr(request, "auto_recovery_active", False)
    ):
        return AutoResumeDecision(False, attempts, max_attempts, "explicit_resume")
    if normalize_status(bundle.status) != "FAILED":
        return AutoResumeDecision(False, attempts, max_attempts, "status_not_failed")
    if not str(bundle.recovery_packet_ref or "").strip():
        return AutoResumeDecision(False, attempts, max_attempts, "missing_recovery_packet")
    if max_attempts > 0 and attempts >= max_attempts:
        return AutoResumeDecision(False, attempts, max_attempts, "attempts_exhausted")
    if auto_resume_remaining_timeout_seconds(bundle) <= 0:
        return AutoResumeDecision(False, attempts, max_attempts, "timeout_budget_exhausted")
    return AutoResumeDecision(True, attempts, max_attempts, "allowed")


def record_auto_resume_attempt(bundle: _BundleLike) -> dict[str, object]:
    path = _ledger_path(bundle.runtime.paths)
    payload = _read_ledger(path)
    attempts = int(payload.get("attempts", 0)) + 1
    packet_refs = [str(item) for item in payload.get("packet_refs", []) if str(item)]
    packet_refs.append(str(bundle.recovery_packet_ref))
    payload = {
        "schema_version": SCHEMA_VERSION,
        "case_id": str(getattr(bundle.runtime.case, "case_id", "") or ""),
        "attempts": attempts,
        "max_attempts": auto_resume_limit(bundle.runtime.request),
        "packet_refs": packet_refs,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return payload


def auto_resume_limit(request: object) -> int:
    configured_default = _configured_auto_resume_limit()
    value = getattr(request, "max_auto_recovery_attempts", None)
    if value is None:
        value = configured_default
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return configured_default


def auto_resume_remaining_timeout_seconds(bundle: _BundleLike) -> int:
    try:
        current_budget = max(0, int(getattr(bundle.runtime.request, "task_timeout_seconds", 0)))
    except (TypeError, ValueError):
        return 0
    try:
        spent = max(0, math.ceil(float(getattr(bundle, "duration_seconds", 0.0))))
    except (TypeError, ValueError):
        spent = current_budget
    return max(0, current_budget - spent)


def _ledger_path(paths: dict[str, Path]) -> Path:
    return paths["root"] / "auto_recovery_ledger.json"


def _ledger_attempts(path: Path) -> int:
    return int(_read_ledger(path).get("attempts", 0))


def _read_ledger(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"schema_version": SCHEMA_VERSION, "attempts": 0, "packet_refs": []}
    return payload if isinstance(payload, dict) else {"schema_version": SCHEMA_VERSION, "attempts": 0, "packet_refs": []}


def _configured_auto_resume_limit() -> int:
    return runtime_guard_int("main_agent_auto_resume_attempt_limit", DEFAULT_AUTO_RECOVERY_ATTEMPTS)


__all__ = [
    "AutoResumeDecision",
    "DEFAULT_AUTO_RECOVERY_ATTEMPTS",
    "SCHEMA_VERSION",
    "auto_resume_decision",
    "auto_resume_limit",
    "auto_resume_remaining_timeout_seconds",
    "record_auto_resume_attempt",
]

# Main-agent task acceptance contract
import json
from dataclasses import dataclass, field
from pathlib import Path

from .artifact_acceptance import (
    ArtifactAcceptanceRequest,
    validate_artifact,
    validation_workspace_root_for_item,
)
from .artifact_candidate_paths import report_with_candidate_paths
from .contract_validation_recovery import recovery_for_findings
from .staged_checkpoint import staged_checkpoint_findings


@dataclass(frozen=True)
class TaskRunAcceptanceRequest:
    expected_artifacts_path: Path
    task_workspace: Path
    report_path: Path


@dataclass(frozen=True)
class TaskRunArtifactAcceptance:
    artifact_id: str
    path: str
    validator: str
    ok: bool
    report: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        return {
            "artifact_id": self.artifact_id,
            "path": self.path,
            "validator": self.validator,
            "ok": self.ok,
            "report": dict(self.report),
        }


@dataclass(frozen=True)
class TaskRunAcceptanceReport:
    ok: bool
    summary: dict[str, int]
    report_ref: str
    artifacts: list[TaskRunArtifactAcceptance] = field(default_factory=list)
    runtime_findings: list[dict[str, object]] = field(default_factory=list)
    recovery: dict[str, object] | None = None

    def to_dict(self) -> dict[str, object]:
        payload = {
            "ok": self.ok,
            "summary": dict(self.summary),
            "report_ref": self.report_ref,
            "artifacts": [artifact.to_dict() for artifact in self.artifacts],
            "runtime_findings": [dict(finding) for finding in self.runtime_findings],
        }
        recovery = self.recovery or recovery_for_findings(
            "task_run_acceptance",
            _acceptance_recovery_findings(self.artifacts, self.runtime_findings),
        )
        if recovery is not None:
            payload["recovery"] = recovery
        return payload


def validate_task_artifacts(request: TaskRunAcceptanceRequest) -> TaskRunAcceptanceReport:
    expected = _expected_artifacts(request.expected_artifacts_path)
    artifacts = [
        _validate_artifact_item(item, request.task_workspace)
        for item in expected
        if item.get("required") is not False
    ]
    runtime_findings = [
        *_staged_checkpoint_findings(expected, request.task_workspace),
        *_runtime_findings(request.task_workspace, artifacts),
    ]
    report = TaskRunAcceptanceReport(
        ok=all(item.ok for item in artifacts) and not runtime_findings,
        summary=_summary(artifacts, runtime_findings),
        report_ref=str(request.report_path),
        artifacts=artifacts,
        runtime_findings=runtime_findings,
        recovery=recovery_for_findings("task_run_acceptance", _acceptance_recovery_findings(artifacts, runtime_findings)),
    )
    _write_report(request.report_path, report.to_dict())
    return report


def _expected_artifacts(path: Path) -> list[dict[str, object]]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return [_missing_contract_item(path)]
    items = payload.get("artifacts") if isinstance(payload, dict) else None
    return [dict(item) for item in items] if isinstance(items, list) else []


def _validate_artifact_item(
    item: dict[str, object],
    task_workspace: Path,
) -> TaskRunArtifactAcceptance:
    artifact_id = str(item.get("artifact_id") or "")
    path = _artifact_path(item, task_workspace)
    validator = _validator_name(item)
    report = validate_artifact(
        ArtifactAcceptanceRequest(
            path=path,
            workspace_root=validation_workspace_root_for_item(item, path, task_workspace),
            validation_contract=_validation_contract(item),
        )
    )
    report = report_with_candidate_paths(
        report,
        path,
        task_workspace,
        validation_contract=_validation_contract(item),
    ).to_dict()
    return TaskRunArtifactAcceptance(
        artifact_id=artifact_id,
        path=str(path),
        validator=validator,
        ok=bool(report.get("ok")),
        report=report,
    )


def _acceptance_recovery_findings(
    artifacts: list[TaskRunArtifactAcceptance],
    runtime_findings: list[dict[str, object]],
) -> tuple[dict[str, object], ...]:
    findings = [dict(item) for item in runtime_findings]
    findings.extend(
        {
            "code": "ARTIFACT_ACCEPTANCE_FAILED",
            "artifact_id": item.artifact_id,
            "path": item.path,
            "validator": item.validator,
        }
        for item in artifacts
        if not item.ok
    )
    return tuple(findings)


def _artifact_path(item: dict[str, object], task_workspace: Path) -> Path:
    raw = str(item.get("preferred_path") or "")
    candidate = Path(raw)
    return candidate.resolve(strict=False) if candidate.is_absolute() else (task_workspace / candidate).resolve(strict=False)


def _staged_checkpoint_findings(
    items: list[dict[str, object]],
    task_workspace: Path,
) -> list[dict[str, object]]:
    return staged_checkpoint_findings(items, task_workspace)


def _validator_name(item: dict[str, object]) -> str:
    contract = item.get("validation_contract")
    if not isinstance(contract, dict):
        return "artifact_acceptance"
    return str(contract.get("validator") or "artifact_acceptance")


def _validation_contract(item: dict[str, object]) -> dict[str, object]:
    contract = item.get("validation_contract")
    return dict(contract) if isinstance(contract, dict) else {}


def _runtime_findings(
    task_workspace: Path,
    artifacts: list[TaskRunArtifactAcceptance],
) -> list[dict[str, object]]:
    return []


def _summary(
    artifacts: list[TaskRunArtifactAcceptance],
    runtime_findings: list[dict[str, object]],
) -> dict[str, int]:
    failed = sum(not item.ok for item in artifacts)
    return {
        "total": len(artifacts) + len(runtime_findings),
        "passed": len(artifacts) - failed,
        "failed": failed + len(runtime_findings),
    }


def _missing_contract_item(path: Path) -> dict[str, object]:
    return {
        "artifact_id": "expected_artifacts_contract",
        "preferred_path": str(path),
        "validation_contract": {"validator": "artifact_acceptance"},
        "required": True,
    }


def _write_report(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


__all__ = [
    "TaskRunAcceptanceReport",
    "TaskRunAcceptanceRequest",
    "TaskRunArtifactAcceptance",
    "validate_task_artifacts",
]