
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..settings.defaults import default_agent_config
from .offline_contract_report import (
    OfflineContractValidation,
    dict_items,
    finding,
    positive_int,
    string_tuple,
    text,
    validation_report,
)

ALLOWED_COMPLEXITY = {"small", "medium"}
ALLOWED_EFFECTS = {"read_only", "dry_run"}
ALLOWED_TOOL_MODES = {"read_only", "dry_run"}


@dataclass(frozen=True)
class _AllowedValuesCheck:
    key: str
    allowed: set[str]
    code: str


def validate_small_real_acceptance_gate(
    gate: dict[str, Any],
    *,
    config: object | None = None,
) -> OfflineContractValidation:
    findings: list[dict[str, object]] = []
    cases = dict_items(gate.get("cases"))
    if not cases:
        findings.append(finding("SMALL_REAL_CASES_MISSING"))
        return validation_report(findings)
    max_runtime_seconds = _max_runtime_seconds(config)
    for case in cases:
        _validate_case(case, findings, max_runtime_seconds=max_runtime_seconds)
    return validation_report(findings)


def _validate_case(
    case: dict[str, Any],
    findings: list[dict[str, object]],
    *,
    max_runtime_seconds: int,
) -> None:
    if text(case.get("complexity")) not in ALLOWED_COMPLEXITY:
        findings.append(finding("SMALL_REAL_CASE_NOT_BOUNDED", _case_extra(case)))
    if case.get("isolation_ok") is not True or not text(case.get("workspace_ref")):
        findings.append(finding("SMALL_REAL_ISOLATION_MISSING", _case_extra(case)))
    if case.get("real_execution_allowed") is True:
        findings.append(finding("SMALL_REAL_REAL_EXECUTION_ENABLED", _case_extra(case)))
    _validate_allowed_values(case, _AllowedValuesCheck("allowed_effects", ALLOWED_EFFECTS, "SMALL_REAL_EFFECT_NOT_ALLOWED"), findings)
    _validate_allowed_values(case, _AllowedValuesCheck("tool_modes", ALLOWED_TOOL_MODES, "SMALL_REAL_TOOL_MODE_NOT_ALLOWED"), findings)
    if not dict_items(case.get("expected_artifacts")):
        findings.append(finding("SMALL_REAL_ARTIFACT_ACCEPTANCE_MISSING", _case_extra(case)))
    if not string_tuple(case.get("verification_refs")):
        findings.append(finding("SMALL_REAL_VERIFICATION_REF_MISSING", _case_extra(case)))
    if case.get("replay_capture_enabled") is not True:
        findings.append(finding("SMALL_REAL_REPLAY_CAPTURE_MISSING", _case_extra(case)))
    if max_runtime_seconds > 0 and positive_int(case.get("max_runtime_seconds")) > max_runtime_seconds:
        findings.append(finding("SMALL_REAL_RUNTIME_TOO_LARGE", _case_extra(case)))


def _max_runtime_seconds(config: object | None) -> int:
    if config is None:
        config = default_agent_config()
    try:
        return max(0, int(config.small_real_acceptance_max_runtime_seconds))
    except (TypeError, ValueError):
        return 0


def _validate_allowed_values(
    case: dict[str, Any],
    check: _AllowedValuesCheck,
    findings: list[dict[str, object]],
) -> None:
    values = string_tuple(case.get(check.key))
    if not values or any(value not in check.allowed for value in values):
        findings.append(finding(check.code, _case_extra(case)))


def _case_extra(case: dict[str, Any]) -> dict[str, object]:
    return {"case_id": text(case.get("case_id"))}


__all__ = ["validate_small_real_acceptance_gate"]
