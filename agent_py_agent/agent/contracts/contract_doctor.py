
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..common.value_parsing import text_value as _text
from .contract_validation_recovery import recovery_for_findings

CURRENT_VERSION = 2
DEFAULT_KNOWN_VERIFIERS = ("artifact_acceptance", "tool_trace", "approval_gate")
IMPOSSIBLE_MIN_SIZE = 100_000_000_000


@dataclass(frozen=True)
class ContractDoctorReport:
    ok: bool
    error_codes: tuple[str, ...]
    findings: tuple[dict[str, object], ...]
    recovery: dict[str, object] | None = None


def lint_contract(
    contract: dict[str, Any],
    *,
    known_verifiers: tuple[str, ...] = DEFAULT_KNOWN_VERIFIERS,
) -> ContractDoctorReport:
    findings: list[dict[str, object]] = []
    _validate_version(contract, findings)
    _validate_schema(contract, findings)
    _validate_field_types(contract, findings)
    _validate_unknown_rules(contract, set(known_verifiers), findings)
    _validate_conflicts(contract, findings)
    _validate_impossible_artifacts(contract, findings)
    return ContractDoctorReport(
        ok=not findings,
        error_codes=tuple(dict.fromkeys(_text(item.get("code")) for item in findings)),
        findings=tuple(findings),
        recovery=recovery_for_findings("contract_doctor", findings),
    )


def _validate_version(contract: dict[str, Any], findings: list[dict[str, object]]) -> None:
    version = contract.get("version", CURRENT_VERSION)
    if version != CURRENT_VERSION:
        findings.append(_finding("CONTRACT_VERSION_UNSUPPORTED", {"version": version}))


def _validate_schema(contract: dict[str, Any], findings: list[dict[str, object]]) -> None:
    if contract.get("version", CURRENT_VERSION) != CURRENT_VERSION:
        return
    artifacts = contract.get("artifacts")
    if "artifact_path" in contract or (artifacts is not None and not isinstance(artifacts, dict)):
        findings.append(_finding("CONTRACT_SCHEMA_INVALID"))


def _validate_field_types(contract: dict[str, Any], findings: list[dict[str, object]]) -> None:
    if "max_steps" in contract and not isinstance(contract.get("max_steps"), int):
        findings.append(_finding("CONTRACT_FIELD_TYPE_INVALID", {"field": "max_steps"}))
    for field in ("required_tools", "forbidden_tools", "rules"):
        value = contract.get(field)
        if value is not None and not _is_string_sequence(value):
            findings.append(_finding("CONTRACT_FIELD_TYPE_INVALID", {"field": field}))


def _validate_unknown_rules(
    contract: dict[str, Any],
    known_verifiers: set[str],
    findings: list[dict[str, object]],
) -> None:
    for rule in _string_tuple(contract.get("rules")):
        if rule not in known_verifiers:
            findings.append(_finding("UNKNOWN_VERIFIER", {"rule": rule}))


def _validate_conflicts(contract: dict[str, Any], findings: list[dict[str, object]]) -> None:
    required = set(_string_tuple(contract.get("required_tools")))
    forbidden = set(_string_tuple(contract.get("forbidden_tools")))
    overlap = sorted(required & forbidden)
    if overlap:
        findings.append(_finding("CONTRACT_RULE_CONFLICT", {"tools": tuple(overlap)}))


def _validate_impossible_artifacts(contract: dict[str, Any], findings: list[dict[str, object]]) -> None:
    for artifact in _required_artifacts(contract):
        min_size = _optional_int(artifact.get("min_size"))
        required_sections = set(_string_tuple(artifact.get("required_sections")))
        forbidden_words = set(_string_tuple(artifact.get("forbidden_words")))
        if min_size > IMPOSSIBLE_MIN_SIZE or required_sections & forbidden_words:
            findings.append(_finding("CONTRACT_IMPOSSIBLE", {"path": _text(artifact.get("path"))}))
            return


def _required_artifacts(contract: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    artifacts = contract.get("artifacts")
    if not isinstance(artifacts, dict):
        return ()
    required = artifacts.get("required")
    if not isinstance(required, (list, tuple)):
        return ()
    return tuple(item for item in required if isinstance(item, dict))


def _is_string_sequence(value: object) -> bool:
    return isinstance(value, (list, tuple)) and all(isinstance(item, str) for item in value)


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

__all__ = ["ContractDoctorReport", "lint_contract"]
