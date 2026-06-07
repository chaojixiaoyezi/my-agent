
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..common.value_parsing import text_value as _text
from .contract_validation_recovery import recovery_for_findings


@dataclass(frozen=True)
class OfflineVerifierIntegrityValidation:
    ok: bool
    error_codes: tuple[str, ...]
    findings: tuple[dict[str, object], ...]
    recovery: dict[str, object] | None = None


def validate_verifier_integrity(facts: dict[str, Any]) -> OfflineVerifierIntegrityValidation:
    findings: list[dict[str, object]] = []
    _validate_section_content(facts, findings)
    _validate_evidence_claims(facts, findings)
    _validate_verifier_runtime(facts, findings)
    return OfflineVerifierIntegrityValidation(
        ok=not findings,
        error_codes=tuple(dict.fromkeys(_text(item.get("code")) for item in findings)),
        findings=tuple(findings),
        recovery=recovery_for_findings("offline_verifier_integrity", findings),
    )


def _validate_section_content(facts: dict[str, Any], findings: list[dict[str, object]]) -> None:
    for section in _dict_items(facts.get("report_sections")):
        if _text(section.get("content_hash")) == "same_as_heading":
            findings.append(_finding("VERIFIER_KEYWORD_ONLY_CONTENT", {"section": _text(section.get("name"))}))
            return


def _validate_evidence_claims(facts: dict[str, Any], findings: list[dict[str, object]]) -> None:
    store_refs = set(_string_tuple(facts.get("evidence_store_refs")))
    tool_names = _successful_tool_names(facts.get("tool_trace"))
    max_age = _max_evidence_age(facts)
    for claim in _dict_items(facts.get("evidence_claims")):
        _validate_one_claim_ref(claim, store_refs, findings)
        _validate_one_claim_tool(claim, tool_names, findings)
        _validate_one_claim_freshness(claim, max_age, findings)


def _validate_one_claim_ref(
    claim: dict[str, Any],
    store_refs: set[str],
    findings: list[dict[str, object]],
) -> None:
    ref = _text(claim.get("evidence_ref"))
    if ref and ref not in store_refs:
        findings.append(_finding("EVIDENCE_REF_MISSING", {"evidence_ref": ref}))


def _validate_one_claim_tool(
    claim: dict[str, Any],
    tool_names: set[str],
    findings: list[dict[str, object]],
) -> None:
    source_tool = _text(claim.get("source_tool"))
    if source_tool and source_tool not in tool_names:
        findings.append(_finding("EVIDENCE_TOOL_TRACE_MISSING", {"source_tool": source_tool}))


def _validate_one_claim_freshness(
    claim: dict[str, Any],
    max_age: int,
    findings: list[dict[str, object]],
) -> None:
    if max_age <= 0:
        return
    if _optional_int(claim.get("age_minutes")) > max_age:
        findings.append(_finding("EVIDENCE_STALE", {"evidence_ref": _text(claim.get("evidence_ref"))}))


def _validate_verifier_runtime(facts: dict[str, Any], findings: list[dict[str, object]]) -> None:
    verifier = facts.get("verifier")
    if not isinstance(verifier, dict):
        return
    timeout_ms = _optional_int(verifier.get("timeout_ms"))
    if timeout_ms > 0 and _optional_int(verifier.get("duration_ms")) > timeout_ms:
        findings.append(_finding("VERIFIER_TIMEOUT_EXCEEDED"))
    if verifier.get("requires_llm") is True:
        findings.append(_finding("CORE_VERIFIER_LLM_DEPENDENCY"))


def _successful_tool_names(value: object) -> set[str]:
    tools: set[str] = set()
    for item in _dict_items(value):
        ok = item.get("ok") is True
        tool = _text(item.get("tool"))
        if ok and tool:
            tools.add(tool)
    return tools


def _max_evidence_age(facts: dict[str, Any]) -> int:
    freshness = facts.get("evidence_freshness")
    if not isinstance(freshness, dict):
        return 0
    return _optional_int(freshness.get("max_age_minutes"))


def _dict_items(value: object) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(item for item in value if isinstance(item, dict))


def _string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple, set)):
        return ()
    return tuple(text for item in value for text in (_text(item),) if text)


def _optional_int(value: object) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _finding(code: str, extra: dict[str, object] | None = None) -> dict[str, object]:
    return {"code": code, **(extra or {})}

__all__ = ["OfflineVerifierIntegrityValidation", "validate_verifier_integrity"]
