# LLM: Evidence contracts make research/table facts traceable to structured source refs.
# 模块用途: 验证资料整理、表格和报告里的关键数据是否有可读来源；机器只信结构化字段，不解析自然语言说明。

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .contract_validation_recovery import recovery_for_findings


# LLM: EvidenceSourceRef describes one source that can be re-read or audited.
# 类用途: 保存 API、网页、文件或 artifact 来源的稳定 id、地址和抓取时间。
@dataclass(frozen=True)
class EvidenceSourceRef:
    source_id: str
    source_type: str = ""
    uri: str = ""
    retrieved_at: str = ""
    artifact_ref: str = ""
    content_sha256: str = ""
    status: str = "AVAILABLE"
    reserved: dict[str, Any] = field(default_factory=dict)

    # LLM: to_dict keeps evidence reports stable for JSON and future frontends.
    # 函数用途: 转成普通 dict；后续新增字段优先放 reserved，避免破坏旧报告。
    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "source_type": self.source_type,
            "uri": self.uri,
            "retrieved_at": self.retrieved_at,
            "artifact_ref": self.artifact_ref,
            "content_sha256": self.content_sha256,
            "status": self.status,
            "reserved": dict(self.reserved),
        }


# LLM: EvidenceClaim links one produced fact to one or more source refs.
# 类用途: 保存表格单元格、报告字段或统计结果的来源关系和验证状态。
@dataclass(frozen=True)
class EvidenceClaim:
    claim_id: str
    field: str
    value: Any
    source_ids: list[str] = field(default_factory=list)
    confidence: float = 1.0
    verification_status: str = "VERIFIED"
    value_type: str = "exact"
    methodology: str = ""
    reserved: dict[str, Any] = field(default_factory=dict)

    # LLM: to_dict serializes claims without assuming value type.
    # 函数用途: 输出结构化 claim，供报告、验收和后续恢复链路读取。
    def to_dict(self) -> dict[str, Any]:
        return {
            "claim_id": self.claim_id,
            "field": self.field,
            "value": self.value,
            "source_ids": list(self.source_ids),
            "confidence": self.confidence,
            "verification_status": self.verification_status,
            "value_type": self.value_type,
            "methodology": self.methodology,
            "reserved": dict(self.reserved),
        }


# LLM: EvidenceContractRequest bundles every source and claim needed for one audit.
# 类用途: 描述一次资料证据检查；required_fields 用机器字段名表达必须有证据的数据。
@dataclass(frozen=True)
class EvidenceContractRequest:
    source_refs: list[EvidenceSourceRef] = field(default_factory=list)
    claims: list[EvidenceClaim] = field(default_factory=list)
    required_fields: list[str] = field(default_factory=list)
    require_verified: bool = False
    allowed_value_types: list[str] = field(default_factory=lambda: ["exact"])
    min_confidence: float = 0.0
    require_methodology_for_estimates: bool = False
    reserved: dict[str, Any] = field(default_factory=dict)


# LLM: EvidenceContractReport is the refs-first result consumed by acceptance and E2E tests.
# 类用途: 保存资料证据检查结果、摘要和结构化 findings，不复制网页/API 正文。
@dataclass(frozen=True)
class EvidenceContractReport:
    ok: bool
    summary: dict[str, int]
    findings: list[dict[str, Any]] = field(default_factory=list)
    source_refs: list[EvidenceSourceRef] = field(default_factory=list)
    claims: list[EvidenceClaim] = field(default_factory=list)
    recovery: dict[str, object] | None = None

    # LLM: to_dict gives CLI/tests a stable machine-readable payload.
    # 函数用途: 输出摘要、findings、source_refs 和 claims，方便定位哪个字段缺证据。
    def to_dict(self) -> dict[str, Any]:
        payload = {
            "ok": self.ok,
            "summary": dict(self.summary),
            "findings": list(self.findings),
            "source_refs": [item.to_dict() for item in self.source_refs],
            "claims": [item.to_dict() for item in self.claims],
        }
        recovery = self.recovery or recovery_for_findings("evidence_contract", self.findings)
        if recovery is not None:
            payload["recovery"] = recovery
        return payload


# LLM: _ClaimValidationContext holds evidence claim validation policy in one object.
# 类用途: 避免 claim 检查函数靠多参数散传，保持合同字段集中可扩展。
@dataclass(frozen=True)
class _ClaimValidationContext:
    sources_by_id: dict[str, EvidenceSourceRef]
    allowed_value_types: set[str]
    min_confidence: float
    require_methodology_for_estimates: bool
    require_verified: bool


# LLM: evaluate_evidence_contract validates sources and claim links without reading prose.
# 函数用途: 检查来源是否可读、claim 是否有来源、必需字段是否存在；不把自然语言说明当证据。
def evaluate_evidence_contract(request: EvidenceContractRequest) -> EvidenceContractReport:
    sources_by_id = {item.source_id: item for item in request.source_refs if item.source_id}
    findings: list[dict[str, Any]] = []
    findings.extend(_source_findings(request.source_refs))
    findings.extend(_claim_findings(request.claims, _claim_validation_context(request, sources_by_id)))
    findings.extend(_required_field_findings(request.required_fields, request.claims))
    return EvidenceContractReport(
        ok=not findings,
        summary=_summary(request, findings),
        findings=findings,
        source_refs=list(request.source_refs),
        claims=list(request.claims),
        recovery=recovery_for_findings("evidence_contract", findings),
    )


# LLM: _source_findings checks source readability before validating claim references.
# 函数用途: 缺 uri 且缺 artifact_ref 的 source 不能作为可审计来源。
def _source_findings(source_refs: list[EvidenceSourceRef]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for item in source_refs:
        if not item.source_id or not (item.uri or item.artifact_ref):
            findings.append(
                _finding(
                    "EVIDENCE_SOURCE_UNREADABLE",
                    "hard",
                    "source ref lacks source_id or readable uri/artifact_ref",
                    details={"source_id": item.source_id},
                )
            )
    return findings


# LLM: _claim_findings verifies claim-to-source edges using source ids as machine truth.
# 函数用途: 检查 claim 是否缺来源、引用未知来源或未被验证。
def _claim_findings(claims: list[EvidenceClaim], context: _ClaimValidationContext) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for claim in claims:
        if not claim.source_ids:
            findings.append(_claim_finding("EVIDENCE_CLAIM_UNSOURCED", "claim has no source ids", claim))
            continue
        missing = [source_id for source_id in claim.source_ids if source_id not in context.sources_by_id]
        if missing:
            findings.append(_claim_finding("EVIDENCE_SOURCE_MISSING", "claim references missing source ids", claim, {"missing_source_ids": missing}))
        findings.extend(_claim_policy_findings(claim, context))
    return findings


# LLM: _claim_validation_context normalizes evidence policy from the request.
# 函数用途: 将 allowed_value_types 等合同字段转成 claim 检查用的稳定上下文。
def _claim_validation_context(
    request: EvidenceContractRequest,
    sources_by_id: dict[str, EvidenceSourceRef],
) -> _ClaimValidationContext:
    allowed_types = {str(item).strip() for item in request.allowed_value_types if str(item).strip()} or {"exact"}
    return _ClaimValidationContext(
        sources_by_id=sources_by_id,
        allowed_value_types=allowed_types,
        min_confidence=request.min_confidence,
        require_methodology_for_estimates=request.require_methodology_for_estimates,
        require_verified=request.require_verified,
    )


# LLM: _claim_policy_findings checks verification, value type, confidence, and estimate methodology.
# 函数用途: 将每条 claim 的策略类 finding 拆小，便于扩展更多机器字段。
def _claim_policy_findings(claim: EvidenceClaim, context: _ClaimValidationContext) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    if context.require_verified and claim.verification_status != "VERIFIED":
        findings.append(_claim_finding("EVIDENCE_CLAIM_UNVERIFIED", "claim verification_status is not VERIFIED", claim))
    findings.extend(_claim_value_type_findings(claim, context))
    findings.extend(_claim_confidence_findings(claim, context))
    findings.extend(_claim_estimate_method_findings(claim, context))
    return findings


# LLM: _claim_value_type_findings enforces allowed evidence value types.
# 函数用途: 防止估算值冒充精确事实。
def _claim_value_type_findings(claim: EvidenceClaim, context: _ClaimValidationContext) -> list[dict[str, Any]]:
    value_type = str(claim.value_type or "exact").strip()
    if value_type in context.allowed_value_types:
        return []
    return [
        _claim_finding(
            "EVIDENCE_VALUE_TYPE_NOT_ALLOWED",
            "claim value_type is not allowed by the evidence contract",
            claim,
            {"value_type": value_type},
        )
    ]


# LLM: _claim_confidence_findings enforces numeric confidence thresholds.
# 函数用途: 估算或弱证据低于合同阈值时拒绝通过。
def _claim_confidence_findings(claim: EvidenceClaim, context: _ClaimValidationContext) -> list[dict[str, Any]]:
    if context.min_confidence <= 0 or claim.confidence >= context.min_confidence:
        return []
    return [
        _claim_finding(
            "EVIDENCE_CLAIM_LOW_CONFIDENCE",
            "claim confidence is below the required minimum",
            claim,
            {"confidence": claim.confidence, "min_confidence": context.min_confidence},
        )
    ]


# LLM: _claim_estimate_method_findings requires methodology for declared estimates.
# 函数用途: 估算类 claim 必须说明结构化计算/采样口径。
def _claim_estimate_method_findings(claim: EvidenceClaim, context: _ClaimValidationContext) -> list[dict[str, Any]]:
    value_type = str(claim.value_type or "exact").strip()
    if not context.require_methodology_for_estimates or value_type != "estimated" or claim.methodology.strip():
        return []
    return [_claim_finding("EVIDENCE_ESTIMATE_METHOD_MISSING", "estimated claim lacks machine-readable methodology", claim)]


# LLM: _claim_finding adds claim_id to evidence findings consistently.
# 函数用途: 构造 claim 级 finding，附加字段只来自机器参数。
def _claim_finding(code: str, message: str, claim: EvidenceClaim, details: dict[str, Any] | None = None) -> dict[str, Any]:
    return _finding(code, "hard", message, details={"claim_id": claim.claim_id, **(details or {})})


# LLM: _required_field_findings ensures required output columns are present as structured claims.
# 函数用途: 只按 field 字段判断必需项，不从表头或说明文字里猜。
def _required_field_findings(required_fields: list[str], claims: list[EvidenceClaim]) -> list[dict[str, Any]]:
    claimed_fields = {item.field for item in claims if item.field}
    return [
        _finding(
            "EVIDENCE_REQUIRED_FIELD_MISSING",
            "hard",
            "required field has no claim",
            details={"field": field},
        )
        for field in sorted({item for item in required_fields if item and item not in claimed_fields})
    ]


# LLM: _summary keeps reports small but useful for dashboards and CI.
# 函数用途: 汇总来源、claim、已验证 claim 和 finding 数量。
def _summary(request: EvidenceContractRequest, findings: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "sources": len(request.source_refs),
        "claims": len(request.claims),
        "verified_claims": sum(item.verification_status == "VERIFIED" for item in request.claims),
        "findings": len(findings),
    }


# LLM: _finding creates a consistent finding shape across evidence checks.
# 函数用途: 构造统一 code/severity/message 结构，额外机器字段通过 details 展开。
def _finding(code: str, severity: str, message: str, *, details: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"code": code, "severity": severity, "message": message, **dict(details or {})}


__all__ = [
    "EvidenceClaim",
    "EvidenceContractReport",
    "EvidenceContractRequest",
    "EvidenceSourceRef",
    "evaluate_evidence_contract",
]
