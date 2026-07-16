
from __future__ import annotations

from typing import Any

from .offline_contract_report import OfflineContractValidation, finding, text, validation_report

REQUIRED_ENTRYPOINTS = (
    "state_machine",
    "tool_executor",
    "runlog",
    "tooltrace",
    "approval_gate",
    "effective_contract",
)


def validate_main_agent_core_entrypoints(contract: dict[str, Any]) -> OfflineContractValidation:
    findings: list[dict[str, object]] = []
    entrypoints = contract.get("entrypoints") if isinstance(contract.get("entrypoints"), dict) else {}
    invariants = contract.get("invariants") if isinstance(contract.get("invariants"), dict) else {}
    _validate_required_entrypoints(entrypoints, findings)
    _validate_invariants(invariants, findings)
    return validation_report(findings)


def _validate_required_entrypoints(entrypoints: dict[str, Any], findings: list[dict[str, object]]) -> None:
    missing = tuple(name for name in REQUIRED_ENTRYPOINTS if not text(entrypoints.get(name)))
    if missing:
        findings.append(finding("MAIN_CORE_ENTRYPOINT_MISSING", {"entrypoints": missing}))


def _validate_invariants(invariants: dict[str, Any], findings: list[dict[str, object]]) -> None:
    if invariants.get("success_requires_verification") is not True:
        findings.append(finding("MAIN_CORE_SUCCESS_WITHOUT_VERIFICATION"))
    if invariants.get("tool_calls_require_executor") is not True:
        findings.append(finding("MAIN_CORE_TOOL_EXECUTOR_BYPASS"))
    if text(invariants.get("state_mutation_mode")) != "event_only":
        findings.append(finding("MAIN_CORE_STATE_MUTATION_NOT_EVENT_ONLY"))
    if text(invariants.get("machine_facts_source")) != "structured_fields":
        findings.append(finding("MAIN_CORE_NATURAL_LANGUAGE_FACT_SOURCE"))


__all__ = ["REQUIRED_ENTRYPOINTS", "validate_main_agent_core_entrypoints"]
