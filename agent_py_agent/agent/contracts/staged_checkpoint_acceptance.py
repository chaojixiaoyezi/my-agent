# LLM: staged_checkpoint_acceptance centralizes generic staged checkpoint validation for long-running artifact flows.
# 模块用途: 统一校验阶段产物是否缺失、为空、JSON 截断或无数据，避免真实任务和普通任务各自维护一套判断。

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .artifact_collection_contract import collection_contract_finding_dicts
from .contract_trace import trace_entry, with_contract_trace
from .evidence_contract import (
    EvidenceContractRequest,
    evaluate_evidence_contract,
)
from .gates.delivery_quality_metrics import delivery_quality_metric_findings
from .staged_checkpoint_contract_options import staged_checkpoint_contexts
from .staged_checkpoint_evidence_payloads import claims, source_refs, string_list
from .staged_checkpoint_files import artifact_path, json_checkpoint_status


# LLM: StagedEvidenceOptions keeps this contract helper structure-first and stable.
# 类用途: 支撑本模块的机器字段校验、转换或汇总，不读取普通自然语言作为事实。
@dataclass(frozen=True)
class StagedEvidenceOptions:
    phase: str = "staged"
    emit_path_findings: bool = True


# LLM: StagedEvidenceRequest keeps this contract helper structure-first and stable.
# 类用途: 支撑本模块的机器字段校验、转换或汇总，不读取普通自然语言作为事实。
@dataclass(frozen=True)
class StagedEvidenceRequest:
    ref: str
    task_workspace: Path
    evidence_contract: dict[str, object]
    options: StagedEvidenceOptions = StagedEvidenceOptions()


# LLM: StagedCheckpointOptions carries optional shape checks for one checkpoint.
# 类用途: 将列、sheet 和 validation_contract 打包，避免函数参数继续膨胀。
@dataclass(frozen=True)
class StagedCheckpointOptions:
    required_columns: list[str] | None = None
    required_sheets_min: int = 0
    validation_contract: dict[str, object] | None = None


_DEFAULT_STAGED_CHECKPOINT_OPTIONS = StagedCheckpointOptions()


# LLM: staged_checkpoint_findings inspects only machine-declared checkpoint refs and emits stable findings.
# 函数用途: 根据 staging_contract.checkpoint_refs 检查阶段文件状态，只返回结构化 finding，不读取提示词或自然语言摘要。
def staged_checkpoint_findings(
    items: list[dict[str, object]],
    task_workspace: Path,
) -> list[dict[str, object]]:
    findings: list[dict[str, object]] = []
    preferred_paths = {str(item.get("preferred_path") or item.get("path") or "") for item in items}
    for item in items:
        contexts = staged_checkpoint_contexts(item, preferred_paths)
        for context in contexts:
            findings.extend(
                one_staged_checkpoint_findings(
                    context.ref,
                    task_workspace,
                    StagedCheckpointOptions(
                        context.required_columns,
                        context.required_sheets_min,
                        context.validation_contract,
                    ),
                )
            )
            findings.extend(
                staged_json_evidence_findings(
                    StagedEvidenceRequest(
                        context.ref,
                        task_workspace,
                        context.evidence_contract,
                        StagedEvidenceOptions(emit_path_findings=False),
                    )
                )
            )
        if contexts:
            findings.extend(collection_contract_finding_dicts(contexts[0].validation_contract, task_workspace))
    return findings


# LLM: one_staged_checkpoint_findings validates one checkpoint file with generic shape-aware rules.
# 函数用途: 单独检查一个阶段文件，区分缺失、空文件、JSON 非法、JSON 无有效数据等通用错误。
def one_staged_checkpoint_findings(
    ref: str,
    task_workspace: Path,
    options: StagedCheckpointOptions = _DEFAULT_STAGED_CHECKPOINT_OPTIONS,
) -> list[dict[str, object]]:
    try:
        path = artifact_path(ref, task_workspace)
    except ValueError:
        return [_path_outside_workspace_finding(ref, task_workspace, "Staged checkpoint path is outside task workspace.")]
    if not path.exists():
        return [_finding("STAGED_ARTIFACT_MISSING", ref, path, {"message": "Staged checkpoint does not exist."})]
    if path.is_file() and path.stat().st_size <= 0:
        return [_finding("STAGED_ARTIFACT_EMPTY", ref, path, {"message": "Staged checkpoint is empty."})]
    if path.suffix.lower() != ".json":
        return _artifact_validation_findings(ref, path, task_workspace, options.validation_contract or {})
    return _json_checkpoint_findings(
        ref,
        path,
        options,
    )


# LLM: _artifact_validation_findings routes non-JSON checkpoints through the shared artifact validator.
# 函数用途: CSV/XLSX/PDF/TXT 阶段文件不再只检查存在；统一复用产物注册表输出结构化 findings。
def _artifact_validation_findings(
    ref: str,
    path: Path,
    task_workspace: Path,
    validation_contract: dict[str, object],
) -> list[dict[str, object]]:
    from .artifact_acceptance import validate_artifact
    from .artifact_acceptance_models import ArtifactAcceptanceRequest

    checkpoint_contract = dict(validation_contract)
    checkpoint_contract.pop("validator", None)
    report = validate_artifact(
        ArtifactAcceptanceRequest(
            path=path,
            workspace_root=task_workspace,
            validation_contract=checkpoint_contract,
        )
    )
    return [
        with_contract_trace(
            {
                **finding.to_dict(),
                "stage_ref": ref,
                "artifact_kind": report.artifact_kind,
            },
            (
                trace_entry("staged_checkpoint_acceptance", ref=ref, path=path),
                trace_entry("artifact_acceptance", code=finding.code),
            ),
        )
        for finding in report.findings
    ]


# LLM: staged_json_evidence_findings validates source/claim refs for factual staged data.
# 函数用途: 对 source_data.json 这类阶段文件执行通用证据合同，不读取自然语言说明。
def staged_json_evidence_findings(request: StagedEvidenceRequest) -> list[dict[str, object]]:
    ref = request.ref
    task_workspace = request.task_workspace
    evidence_contract = request.evidence_contract
    if not evidence_contract:
        return []
    options = request.options
    try:
        path = artifact_path(ref, task_workspace)
    except ValueError:
        if not options.emit_path_findings:
            return []
        return [_path_outside_workspace_finding(ref, task_workspace, "Staged evidence path is outside task workspace.")]
    value = _staged_json_dict(path)
    if not isinstance(value, dict):
        return []
    source_records = source_refs(value.get("source_refs"))
    claim_records = claims(value.get("claims"))
    findings = _evidence_finding_dicts(
        _evaluate_staged_evidence(evidence_contract, source_records, claim_records, phase=options.phase),
        ref,
        path,
    )
    findings.extend(
        _metric_finding_dicts(
            delivery_quality_metric_findings(evidence_contract.get("metric_contracts"), source_records, claim_records),
            ref,
            path,
        )
    )
    return findings


# LLM: _staged_json_dict keeps this contract helper structure-first and stable.
# 函数用途: 支撑本模块的机器字段校验、转换或汇总，不读取普通自然语言作为事实。
def _staged_json_dict(path: Path) -> dict[str, object] | None:
    if not path.exists() or path.suffix.lower() != ".json":
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


# LLM: _evaluate_staged_evidence keeps this contract helper structure-first and stable.
# 函数用途: 支撑本模块的机器字段校验、转换或汇总，不读取普通自然语言作为事实。
def _evaluate_staged_evidence(
    evidence_contract: dict[str, object],
    source_records: list[dict[str, object]],
    claim_records: list[dict[str, object]],
    *,
    phase: str = "staged",
) -> object:
    return evaluate_evidence_contract(
        EvidenceContractRequest(
            source_refs=source_records,
            claims=claim_records,
            required_fields=string_list(evidence_contract.get("required_fields")),
            allowed_value_types=string_list(evidence_contract.get("allowed_value_types")) or ["exact"],
            min_confidence=_float_value(evidence_contract.get("min_confidence")),
            require_methodology_for_estimates=bool(evidence_contract.get("require_methodology_for_estimates", False)),
            require_verified=_requires_verified(evidence_contract, phase),
        )
    )


# LLM: _evidence_finding_dicts keeps this contract helper structure-first and stable.
# 函数用途: 支撑本模块的机器字段校验、转换或汇总，不读取普通自然语言作为事实。
def _evidence_finding_dicts(report: object, ref: str, path: Path) -> list[dict[str, object]]:
    return [
        with_contract_trace(
            {
                "code": str(item.get("code") or "EVIDENCE_CONTRACT_FAILED"),
                "severity": str(item.get("severity") or "hard"),
                "stage_ref": ref,
                "location": str(path),
                "message": str(item.get("message") or "Evidence contract failed."),
                **{key: val for key, val in item.items() if key not in {"code", "severity", "message"}},
            },
            (
                trace_entry("staged_json_evidence", ref=ref, path=path),
                trace_entry("evidence_contract", code=str(item.get("code") or "")),
            ),
        )
        for item in report.findings
    ]


# LLM: _metric_finding_dicts keeps this contract helper structure-first and stable.
# 函数用途: 支撑本模块的机器字段校验、转换或汇总，不读取普通自然语言作为事实。
def _metric_finding_dicts(findings: object, ref: str, path: Path) -> list[dict[str, object]]:
    return [
        with_contract_trace(
            {
                "code": item.code,
                "severity": item.severity,
                "stage_ref": ref,
                "location": str(path),
                "message": item.message or "Metric quality contract failed.",
                **({"evidence": item.evidence} if item.evidence else {}),
            },
            (
                trace_entry("staged_metric_quality", ref=ref, path=path),
                trace_entry("metric_contract", code=str(item.code)),
            ),
        )
        for item in findings
    ]


# LLM: _json_checkpoint_findings keeps this contract helper structure-first and stable.
# 函数用途: 支撑本模块的机器字段校验、转换或汇总，不读取普通自然语言作为事实。
def _json_checkpoint_findings(
    ref: str,
    path: Path,
    options: StagedCheckpointOptions,
) -> list[dict[str, object]]:
    status = json_checkpoint_status(
        path,
        required_columns=options.required_columns,
        required_sheets_min=options.required_sheets_min,
    )
    code = status["code"]
    if code == "STAGED_JSON_INVALID":
        return [_finding(code, ref, path, {"message": "Staged JSON checkpoint is invalid or truncated.", "parse_error": status.get("parse_error", "")})]
    if code == "STAGED_JSON_NO_ROWS":
        return [_finding(code, ref, path, {"message": "Staged JSON checkpoint has no data rows."})]
    if code != "OK":
        return [_finding(code, ref, path, dict(status))]
    return []


# LLM: _float_value normalizes optional numeric evidence contract thresholds.
# 函数用途: 从 evidence_contract.min_confidence 读取浮点阈值，坏值按 0 处理。
def _float_value(value: object) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


# LLM: _requires_verified keeps this contract helper structure-first and stable.
# 函数用途: 支撑本模块的机器字段校验、转换或汇总，不读取普通自然语言作为事实。
def _requires_verified(evidence_contract: dict[str, object], phase: str) -> bool:
    if phase == "staged":
        return bool(
            evidence_contract.get(
                "staging_require_verified",
                evidence_contract.get("require_verified_during_staging", False),
            )
        )
    return bool(evidence_contract.get("require_verified", True))


# LLM: _display_path keeps this contract helper structure-first and stable.
# 函数用途: 支撑本模块的机器字段校验、转换或汇总，不读取普通自然语言作为事实。
def _display_path(ref: str, task_workspace: Path) -> Path:
    preferred = Path(str(ref or ""))
    return preferred.resolve(strict=False) if preferred.is_absolute() else (task_workspace / preferred).resolve(strict=False)


# LLM: _path_outside_workspace_finding keeps this contract helper structure-first and stable.
# 函数用途: 支撑本模块的机器字段校验、转换或汇总，不读取普通自然语言作为事实。
def _path_outside_workspace_finding(ref: str, task_workspace: Path, message: str) -> dict[str, object]:
    return _finding(
        "STAGED_ARTIFACT_PATH_OUTSIDE_WORKSPACE",
        ref,
        _display_path(ref, task_workspace),
        {"message": message},
    )


# LLM: _finding keeps staged checkpoint findings compact and machine-readable.
# 函数用途: 统一生成阶段文件 finding，必要时带上额外字段，例如 parse_error。
def _finding(code: str, ref: str, path: Path, detail: dict[str, object] | None = None) -> dict[str, object]:
    payload = detail or {}
    return with_contract_trace(
        {
            "code": code,
            "severity": "hard",
            "stage_ref": ref,
            "location": str(path),
            "message": str(payload.get("message") or "Staged checkpoint failed."),
            **{key: value for key, value in payload.items() if key != "message"},
        },
        (trace_entry("staged_checkpoint_acceptance", ref=ref, path=path, code=code),),
    )


__all__ = [
    "StagedEvidenceOptions",
    "StagedEvidenceRequest",
    "StagedCheckpointOptions",
    "artifact_path",
    "json_checkpoint_status",
    "one_staged_checkpoint_findings",
    "staged_json_evidence_findings",
    "staged_checkpoint_findings",
]
