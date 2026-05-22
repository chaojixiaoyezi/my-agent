# LLM: staged_checkpoint_acceptance centralizes generic staged checkpoint validation for long-running artifact flows.
# 模块用途: 统一校验阶段产物是否缺失、为空、JSON 截断或无数据，避免真实任务和普通任务各自维护一套判断。

from __future__ import annotations

import json
from pathlib import Path

from .artifact_collection_contract import collection_contract_finding_dicts
from .evidence_contract import (
    EvidenceContractRequest,
    evaluate_evidence_contract,
)
from .gates.delivery_quality_metrics import delivery_quality_metric_findings
from .staged_checkpoint_contract_options import staged_checkpoint_contexts
from .staged_checkpoint_evidence_payloads import claims, source_refs, string_list
from .staged_checkpoint_tabular_shape import (
    contains_nonempty_list,
    tabular_json_shape_issue,
)


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
                    required_columns=context.required_columns,
                    required_sheets_min=context.required_sheets_min,
                )
            )
            findings.extend(staged_json_evidence_findings(context.ref, task_workspace, context.evidence_contract))
        if contexts:
            findings.extend(collection_contract_finding_dicts(contexts[0].validation_contract, task_workspace))
    return findings


# LLM: one_staged_checkpoint_findings validates one checkpoint file with generic shape-aware rules.
# 函数用途: 单独检查一个阶段文件，区分缺失、空文件、JSON 非法、JSON 无有效数据等通用错误。
def one_staged_checkpoint_findings(
    ref: str,
    task_workspace: Path,
    *,
    required_columns: list[str] | None = None,
    required_sheets_min: int = 0,
) -> list[dict[str, object]]:
    path = artifact_path(ref, task_workspace)
    if not path.exists():
        return [_finding("STAGED_ARTIFACT_MISSING", ref, path, {"message": "Staged checkpoint does not exist."})]
    if path.is_file() and path.stat().st_size <= 0:
        return [_finding("STAGED_ARTIFACT_EMPTY", ref, path, {"message": "Staged checkpoint is empty."})]
    if path.suffix.lower() != ".json":
        return []
    status = json_checkpoint_status(
        path,
        required_columns=required_columns,
        required_sheets_min=required_sheets_min,
    )
    if status["code"] == "STAGED_JSON_INVALID":
        return [
            _finding(
                "STAGED_JSON_INVALID",
                ref,
                path,
                {
                    "message": "Staged JSON checkpoint is invalid or truncated.",
                    "parse_error": status.get("parse_error", ""),
                },
            )
        ]
    if status["code"] == "STAGED_JSON_NO_ROWS":
        return [
            _finding(
                "STAGED_JSON_NO_ROWS",
                ref,
                path,
                {"message": "Staged JSON checkpoint has no data rows."},
            )
        ]
    if status["code"] != "OK":
        return [_finding(status["code"], ref, path, dict(status))]
    return []


# LLM: staged_json_evidence_findings validates source/claim refs for factual staged data.
# 函数用途: 对 source_data.json 这类阶段文件执行通用证据合同，不读取自然语言说明。
def staged_json_evidence_findings(
    ref: str,
    task_workspace: Path,
    evidence_contract: dict[str, object] | None,
) -> list[dict[str, object]]:
    if not evidence_contract:
        return []
    path = artifact_path(ref, task_workspace)
    value = _staged_json_dict(path)
    if not isinstance(value, dict):
        return []
    source_records = source_refs(value.get("source_refs"))
    claim_records = claims(value.get("claims"))
    findings = _evidence_finding_dicts(
        _evaluate_staged_evidence(evidence_contract, source_records, claim_records),
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


def _staged_json_dict(path: Path) -> dict[str, object] | None:
    if not path.exists() or path.suffix.lower() != ".json":
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _evaluate_staged_evidence(
    evidence_contract: dict[str, object],
    source_records: list[dict[str, object]],
    claim_records: list[dict[str, object]],
) -> object:
    return evaluate_evidence_contract(
        EvidenceContractRequest(
            source_refs=source_records,
            claims=claim_records,
            required_fields=string_list(evidence_contract.get("required_fields")),
            allowed_value_types=string_list(evidence_contract.get("allowed_value_types")) or ["exact"],
            min_confidence=_float_value(evidence_contract.get("min_confidence")),
            require_methodology_for_estimates=bool(evidence_contract.get("require_methodology_for_estimates", False)),
            require_verified=bool(evidence_contract.get("require_verified", True)),
        )
    )


def _evidence_finding_dicts(report: object, ref: str, path: Path) -> list[dict[str, object]]:
    return [
        {
            "code": str(item.get("code") or "EVIDENCE_CONTRACT_FAILED"),
            "severity": str(item.get("severity") or "hard"),
            "stage_ref": ref,
            "location": str(path),
            "message": str(item.get("message") or "Evidence contract failed."),
            **{key: val for key, val in item.items() if key not in {"code", "severity", "message"}},
        }
        for item in report.findings
    ]


def _metric_finding_dicts(findings: object, ref: str, path: Path) -> list[dict[str, object]]:
    return [
        {
            "code": item.code,
            "severity": item.severity,
            "stage_ref": ref,
            "location": str(path),
            "message": item.message or "Metric quality contract failed.",
            **({"evidence": item.evidence} if item.evidence else {}),
        }
        for item in findings
    ]


# LLM: artifact_path resolves one workspace-relative checkpoint ref without accepting prose-derived paths.
# 函数用途: 把阶段 ref 解析到任务工作区里的绝对路径，保持和主验收一致的路径语义。
def artifact_path(ref: str, task_workspace: Path) -> Path:
    preferred = Path(str(ref or ""))
    if preferred.is_absolute():
        return preferred
    return (task_workspace / preferred).resolve()


# LLM: json_checkpoint_status separates invalid JSON from valid-but-empty structured data.
# 函数用途: 返回阶段 JSON 的结构状态，避免把被截断的 JSON 误判成“只是没有数据”。
def json_checkpoint_status(
    path: Path,
    required_columns: list[str] | None = None,
    required_sheets_min: int = 0,
) -> dict[str, str]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        return {"code": "STAGED_JSON_INVALID", "parse_error": str(exc)}
    except json.JSONDecodeError as exc:
        return {"code": "STAGED_JSON_INVALID", "parse_error": str(exc)}
    if not contains_nonempty_list(value):
        return {"code": "STAGED_JSON_NO_ROWS"}
    if shape_issue := tabular_json_shape_issue(
        value,
        required_columns=required_columns,
        required_sheets_min=required_sheets_min,
    ):
        return shape_issue
    return {"code": "OK"}


# LLM: _float_value normalizes optional numeric evidence contract thresholds.
# 函数用途: 从 evidence_contract.min_confidence 读取浮点阈值，坏值按 0 处理。
def _float_value(value: object) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


# LLM: _finding keeps staged checkpoint findings compact and machine-readable.
# 函数用途: 统一生成阶段文件 finding，必要时带上额外字段，例如 parse_error。
def _finding(code: str, ref: str, path: Path, detail: dict[str, object] | None = None) -> dict[str, object]:
    payload = detail or {}
    return {
        "code": code,
        "severity": "hard",
        "stage_ref": ref,
        "location": str(path),
        "message": str(payload.get("message") or "Staged checkpoint failed."),
        **{key: value for key, value in payload.items() if key != "message"},
    }


__all__ = [
    "artifact_path",
    "json_checkpoint_status",
    "one_staged_checkpoint_findings",
    "staged_json_evidence_findings",
    "staged_checkpoint_findings",
]
