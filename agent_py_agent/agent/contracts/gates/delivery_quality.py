# LLM: Delivery quality gate checks structured data quality before final closeout.
# 模块用途: 校验证据、数据口径、目标语言字段和合同 hash，不把普通自然语言当机器事实。

from __future__ import annotations

from typing import Any

from ..evidence_contract import (
    EvidenceClaim,
    EvidenceContractRequest,
    EvidenceSourceRef,
    evaluate_evidence_contract,
)
from ..staged_checkpoint_evidence_payloads import claims, source_refs, string_list
from .delivery_quality_language import delivery_quality_language_findings
from .delivery_quality_metrics import delivery_quality_metric_findings
from .delivery_quality_trace import DeliveryQualityTraceScope, append_delivery_quality_gate_trace
from .models import GateDecision, GateFinding


# LLM: evaluate_delivery_quality_gate composes data/evidence/language/artifact checks.
# 函数用途: 收口前用同一份机器合同检查交付质量，不依赖任务类型或提示词关键词。
def evaluate_delivery_quality_gate(
    payload: dict[str, Any],
    contract: dict[str, Any],
    *,
    contract_hash: str = "",
) -> GateDecision:
    source_records = source_refs(payload.get("source_refs"))
    claim_records = claims(payload.get("claims"))
    findings = [
        *_evidence_findings(contract, source_records, claim_records),
        *delivery_quality_metric_findings(contract.get("metric_contracts"), source_records, claim_records),
        *delivery_quality_language_findings(payload, contract.get("language_contract"), claim_records),
        *_artifact_hash_findings(payload, contract_hash),
    ]
    evidence = _decision_evidence(payload, source_records, claim_records, contract_hash)
    if findings:
        return GateDecision.repair("delivery_quality", findings, evidence=evidence)
    return GateDecision.allow("delivery_quality", evidence=evidence)


# LLM: _evidence_findings reuses the shared source/claim contract.
# 函数用途: 把 evidence_contract 的结果转成 GateFinding，避免另写一套证据逻辑。
def _evidence_findings(
    contract: dict[str, Any],
    source_records: list[EvidenceSourceRef],
    claim_records: list[EvidenceClaim],
) -> list[GateFinding]:
    evidence_contract = contract.get("evidence_contract")
    if not isinstance(evidence_contract, dict):
        return []
    report = evaluate_evidence_contract(
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
    return [_gate_finding(item) for item in report.findings]


# LLM: _artifact_hash_findings ties artifact validation to the current effective contract hash.
# 函数用途: 产物记录必须带当前合同 hash，防止旧验收结果在新合同下假通过。
def _artifact_hash_findings(payload: dict[str, Any], contract_hash: str) -> list[GateFinding]:
    artifacts = _artifact_records(payload)
    if not artifacts or not contract_hash:
        return []
    findings: list[GateFinding] = []
    for index, artifact in enumerate(artifacts):
        artifact_ref = _text(artifact.get("artifact_ref") or artifact.get("path"))
        validated_hash = _text(artifact.get("validated_contract_hash") or artifact.get("contract_hash"))
        findings.extend(_one_artifact_hash_findings(index, artifact_ref, validated_hash, contract_hash))
    return findings


# LLM: _one_artifact_hash_findings validates one artifact's contract hash binding.
# 函数用途: 缺 hash 或 hash 不匹配时输出稳定 finding code。
def _one_artifact_hash_findings(
    index: int,
    artifact_ref: str,
    validated_hash: str,
    contract_hash: str,
) -> list[GateFinding]:
    evidence = {"index": index, "artifact_ref": artifact_ref}
    if not validated_hash:
        return [GateFinding("ARTIFACT_VALIDATION_CONTRACT_HASH_MISSING", evidence=evidence)]
    if validated_hash == contract_hash:
        return []
    return [
        GateFinding(
            "ARTIFACT_VALIDATION_CONTRACT_HASH_MISMATCH",
            evidence={
                **evidence,
                "validated_contract_hash": validated_hash,
                "contract_hash": contract_hash,
            },
        )
    ]


# LLM: _artifact_records returns structured artifact rows from payload.
# 函数用途: 提取 artifacts 数组中的 dict 项，供合同 hash 检查和 trace summary 使用。
def _artifact_records(payload: dict[str, Any]) -> list[dict[str, Any]]:
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, list):
        return []
    return [dict(item) for item in artifacts if isinstance(item, dict)]


# LLM: _decision_evidence summarizes the quality gate inputs without artifact bodies.
# 函数用途: 给 gate result 记录 source/claim/artifact 数量和合同 hash。
def _decision_evidence(
    payload: dict[str, Any],
    source_records: list[EvidenceSourceRef],
    claim_records: list[EvidenceClaim],
    contract_hash: str,
) -> dict[str, Any]:
    return {
        "source_count": len(source_records),
        "claim_count": len(claim_records),
        "artifact_count": len(_artifact_records(payload)),
        "contract_hash": contract_hash,
    }


# LLM: _gate_finding converts shared evidence findings into gate findings.
# 函数用途: 保留 code/severity/message 和其余结构化字段。
def _gate_finding(item: dict[str, Any]) -> GateFinding:
    public = {"code", "severity", "message"}
    return GateFinding(
        _text(item.get("code")) or "DELIVERY_QUALITY_FINDING",
        severity=_text(item.get("severity")) or "hard",
        message=_text(item.get("message")),
        evidence={key: value for key, value in item.items() if key not in public},
    )


# LLM: _float_value parses optional numeric policy values.
# 函数用途: 合同阈值字段不是数字时按默认值处理，不从文本里猜。
def _float_value(value: object, *, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# LLM: _text normalizes scalar values for exact comparisons only.
# 函数用途: 将结构化标量转成去空白字符串，不解释自然语言含义。
def _text(value: object) -> str:
    return str(value or "").strip()


__all__ = [
    "DeliveryQualityTraceScope",
    "append_delivery_quality_gate_trace",
    "evaluate_delivery_quality_gate",
]
