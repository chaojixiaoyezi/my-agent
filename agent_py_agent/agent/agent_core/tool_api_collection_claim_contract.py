# LLM: Claim contract helpers keep staged source evidence checks out of the API tool gate shell.
# 模块用途: 校验 write_structured_json 的 claims 是否覆盖合同字段且满足 VERIFIED 要求。

from __future__ import annotations


def claim_contract_findings(payload: dict[str, object], contract: dict[str, object]) -> list[str]:
    findings: list[str] = []
    missing = _missing_required_claim_fields(payload, contract)
    if missing:
        findings.append("missing required claim fields: " + ", ".join(missing[:8]))
    if _requires_verified_claims(contract) and (unverified := _unverified_claim_fields(payload)):
        findings.append("claims must be VERIFIED for fields: " + ", ".join(unverified[:8]))
    return findings


def _missing_required_claim_fields(payload: dict[str, object], contract: dict[str, object]) -> list[str]:
    claimed = _payload_claim_fields(payload)
    return [field for field in _required_claim_fields(contract) if field not in claimed]


def _required_claim_fields(contract: dict[str, object]) -> list[str]:
    fields: list[str] = []
    evidence = contract.get("evidence_contract")
    if isinstance(evidence, dict):
        fields.extend(_string_list(evidence.get("required_fields")))
    collection = contract.get("collection_contract")
    if isinstance(collection, dict):
        fields.extend(_string_list(collection.get("required_item_evidence_fields")))
    return sorted(set(fields))


def _payload_claim_fields(payload: dict[str, object]) -> set[str]:
    return {
        field
        for claim in _payload_claims(payload)
        if (field := str(claim.get("field") or "").strip())
    }


def _unverified_claim_fields(payload: dict[str, object]) -> list[str]:
    fields: list[str] = []
    for claim in _payload_claims(payload):
        status = str(claim.get("verification_status") or "VERIFIED").strip()
        field = str(claim.get("field") or "").strip()
        if field and status != "VERIFIED":
            fields.append(field)
    return sorted(set(fields))


def _payload_claims(payload: dict[str, object]) -> list[dict[str, object]]:
    claims: list[dict[str, object]] = []
    for holder in _payload_holders(payload):
        value = holder.get("claims")
        if isinstance(value, list):
            claims.extend(item for item in value if isinstance(item, dict))
    return claims


def _payload_holders(payload: dict[str, object]) -> list[dict[str, object]]:
    holders = [payload]
    data = payload.get("data")
    if isinstance(data, dict):
        holders.append(data)
    return holders


def _requires_verified_claims(contract: dict[str, object]) -> bool:
    evidence = contract.get("evidence_contract")
    return isinstance(evidence, dict) and bool(evidence.get("require_verified", True))


def _string_list(value: object) -> list[str]:
    return [text for item in value if (text := str(item).strip())] if isinstance(value, list) else []


__all__ = ["claim_contract_findings"]
