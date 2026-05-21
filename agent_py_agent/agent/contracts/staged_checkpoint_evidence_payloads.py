# LLM: staged checkpoint evidence payload helpers convert JSON dicts into typed evidence records.
# 模块用途: 从阶段 JSON 的 source_refs/claims 字段构造证据合同对象，不读取自然语言说明。

from __future__ import annotations

from typing import Any

from .evidence_contract import EvidenceClaim, EvidenceSourceRef


# LLM: source_refs converts JSON source_refs into EvidenceSourceRef records without trusting prose.
# 函数用途: 从阶段 JSON 的 source_refs 数组读取机器来源引用，坏项自然变成不可读来源。
def source_refs(value: object) -> list[EvidenceSourceRef]:
    if not isinstance(value, list):
        return []
    refs: list[EvidenceSourceRef] = []
    for item in value:
        if isinstance(item, dict):
            refs.append(_source_ref(item))
    return refs


# LLM: claims converts JSON claims into EvidenceClaim records for evidence validation.
# 函数用途: 从阶段 JSON 的 claims 数组读取 field/value/source_ids，不解析说明文本。
def claims(value: object) -> list[EvidenceClaim]:
    if not isinstance(value, list):
        return []
    return [_claim(index, item) for index, item in enumerate(value) if isinstance(item, dict)]


# LLM: string_list normalizes machine-declared string arrays.
# 函数用途: 提取 required_fields/source_ids 等结构化字符串列表，忽略空值。
def string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [text for item in value if (text := str(item).strip())]


# LLM: _source_ref maps one JSON object into a typed source ref record.
# 函数用途: 保留来源 ID、类型、URI、时间、artifact 引用和校验状态等机器字段。
def _source_ref(item: dict[str, Any]) -> EvidenceSourceRef:
    return EvidenceSourceRef(
        source_id=str(item.get("source_id") or ""),
        source_type=str(item.get("source_type") or ""),
        uri=str(item.get("uri") or ""),
        retrieved_at=str(item.get("retrieved_at") or ""),
        artifact_ref=str(item.get("artifact_ref") or ""),
        content_sha256=str(item.get("content_sha256") or ""),
        status=str(item.get("status") or "AVAILABLE"),
        reserved=dict(item.get("reserved")) if isinstance(item.get("reserved"), dict) else {},
    )


# LLM: _claim maps one JSON object into a typed evidence claim record.
# 函数用途: 保留 claim_id、字段、值、source_ids、置信度和验证状态这些结构化字段。
def _claim(index: int, item: dict[str, Any]) -> EvidenceClaim:
    return EvidenceClaim(
        claim_id=str(item.get("claim_id") or f"claim-{index}"),
        field=str(item.get("field") or ""),
        value=item.get("value"),
        source_ids=string_list(item.get("source_ids")),
        confidence=float(item.get("confidence", 1.0) or 0.0),
        verification_status=str(item.get("verification_status") or "VERIFIED"),
        value_type=str(item.get("value_type") or "exact"),
        methodology=str(item.get("methodology") or ""),
        reserved=dict(item.get("reserved")) if isinstance(item.get("reserved"), dict) else {},
    )


__all__ = ["claims", "source_refs", "string_list"]
