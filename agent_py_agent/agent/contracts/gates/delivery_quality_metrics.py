# LLM: Delivery metric quality checks enforce declared measurement semantics.
# 模块用途: 检查 metric_kind、时间窗口和估算限制，防止当前总量冒充时间段增量。

from __future__ import annotations

from typing import Any

from ..evidence_contract import EvidenceClaim, EvidenceSourceRef
from .models import GateFinding


# LLM: delivery_quality_metric_findings evaluates all metric contracts.
# 函数用途: 按 metric_contracts 的 field/expected_kind 检查 claims，不解析字段名称语义。
def delivery_quality_metric_findings(
    metric_contracts: object,
    source_records: list[EvidenceSourceRef],
    claim_records: list[EvidenceClaim],
) -> list[GateFinding]:
    if not isinstance(metric_contracts, list):
        return []
    sources_by_id = {source.source_id: source for source in source_records if source.source_id}
    findings: list[GateFinding] = []
    for metric_contract in metric_contracts:
        if isinstance(metric_contract, dict):
            findings.extend(_one_metric_contract_findings(metric_contract, claim_records, sources_by_id))
    return findings


# LLM: _one_metric_contract_findings checks all claims for one metric field.
# 函数用途: 单字段口径检查，不按字段名称语义猜测，只按合同和 claim metadata 判断。
def _one_metric_contract_findings(
    metric_contract: dict[str, Any],
    claim_records: list[EvidenceClaim],
    sources_by_id: dict[str, EvidenceSourceRef],
) -> list[GateFinding]:
    field = _text(metric_contract.get("field"))
    expected_kind = _text(metric_contract.get("expected_kind"))
    if not field:
        return [GateFinding("METRIC_CONTRACT_FIELD_MISSING")]
    matched = [claim for claim in claim_records if claim.field == field]
    if not matched:
        return [GateFinding("METRIC_CLAIM_MISSING", evidence={"field": field})]
    findings: list[GateFinding] = []
    for claim in matched:
        findings.extend(_metric_kind_findings(claim, expected_kind, sources_by_id))
        if bool(metric_contract.get("required_window")) and not _has_time_window(claim, sources_by_id):
            findings.append(_claim_metric_finding("METRIC_WINDOW_MISSING", claim, expected_kind=expected_kind))
        findings.extend(_metric_estimate_findings(claim, metric_contract))
    return findings


# LLM: _metric_kind_findings compares actual and expected metric_kind.
# 函数用途: 从 claim/source reserved 读取 actual metric kind，不解析 value 文本。
def _metric_kind_findings(
    claim: EvidenceClaim,
    expected_kind: str,
    sources_by_id: dict[str, EvidenceSourceRef],
) -> list[GateFinding]:
    if not expected_kind:
        return []
    actual_kind = _metric_kind(claim, sources_by_id)
    if not actual_kind:
        return [_claim_metric_finding("METRIC_KIND_MISSING", claim, expected_kind=expected_kind)]
    if actual_kind == expected_kind:
        return []
    return [_claim_metric_finding("METRIC_KIND_MISMATCH", claim, expected_kind=expected_kind, actual_kind=actual_kind)]


# LLM: _metric_estimate_findings enforces estimate policy per metric field.
# 函数用途: 估算型 claim 必须由合同显式允许，并按要求携带结构化 limitations。
def _metric_estimate_findings(
    claim: EvidenceClaim,
    metric_contract: dict[str, Any],
) -> list[GateFinding]:
    if _text(claim.value_type) != "estimated":
        return []
    findings: list[GateFinding] = []
    if metric_contract.get("allow_estimated") is False:
        findings.append(_claim_metric_finding("METRIC_ESTIMATE_NOT_ALLOWED", claim))
    if bool(metric_contract.get("require_limitations_for_estimates")) and not _has_estimate_limitations(claim):
        findings.append(_claim_metric_finding("METRIC_ESTIMATE_LIMITATIONS_MISSING", claim))
    return findings


# LLM: _metric_kind returns the most specific metric kind declared by claim or its sources.
# 函数用途: 优先读取 claim.reserved.metric_kind，缺失再读来源 reserved.metric_kind。
def _metric_kind(claim: EvidenceClaim, sources_by_id: dict[str, EvidenceSourceRef]) -> str:
    for key in ("metric_kind", "observed_metric_kind"):
        if kind := _text(claim.reserved.get(key)):
            return kind
    for source_id in claim.source_ids:
        source = sources_by_id.get(source_id)
        if source and (kind := _text(source.reserved.get("metric_kind"))):
            return kind
    return ""


# LLM: _has_time_window verifies metric windows are explicit machine fields.
# 函数用途: 时间窗口必须在 claim/source reserved 中以 window_start/window_end 或 time_window 表达。
def _has_time_window(claim: EvidenceClaim, sources_by_id: dict[str, EvidenceSourceRef]) -> bool:
    if _has_time_window_fields(claim.reserved):
        return True
    return any(_has_time_window_fields(sources_by_id[source_id].reserved) for source_id in claim.source_ids if source_id in sources_by_id)


# LLM: _has_time_window_fields accepts the two supported structured window shapes.
# 函数用途: 支持 window_start/window_end 和 time_window.start/end，不读说明文字。
def _has_time_window_fields(value: dict[str, Any]) -> bool:
    if _text(value.get("window_start")) and _text(value.get("window_end")):
        return True
    window = value.get("time_window")
    return isinstance(window, dict) and _text(window.get("start")) and _text(window.get("end"))


# LLM: _has_estimate_limitations checks explicit uncertainty/limitations metadata.
# 函数用途: 估算限制必须在 reserved.limitations 或 reserved.uncertainty_notes 中结构化给出。
def _has_estimate_limitations(claim: EvidenceClaim) -> bool:
    for key in ("limitations", "uncertainty_notes"):
        value = claim.reserved.get(key)
        if isinstance(value, str) and value.strip():
            return True
        if isinstance(value, list) and any(_text(item) for item in value):
            return True
    return False


# LLM: _claim_metric_finding creates consistent metric finding evidence.
# 函数用途: 给 metric finding 附 claim_id/field/expected/actual 这些机器字段。
def _claim_metric_finding(
    code: str,
    claim: EvidenceClaim,
    *,
    expected_kind: str = "",
    actual_kind: str = "",
) -> GateFinding:
    evidence = {"claim_id": claim.claim_id, "field": claim.field}
    if expected_kind:
        evidence["expected_kind"] = expected_kind
    if actual_kind:
        evidence["actual_kind"] = actual_kind
    return GateFinding(code, evidence=evidence)


# LLM: _text normalizes scalar values for exact comparisons only.
# 函数用途: 将结构化标量转成去空白字符串，不解释自然语言含义。
def _text(value: object) -> str:
    return str(value or "").strip()


__all__ = ["delivery_quality_metric_findings"]
