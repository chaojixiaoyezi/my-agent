
from __future__ import annotations

from typing import Any

from .offline_contract_report import (
    OfflineContractValidation,
    dict_items,
    finding,
    text,
    validation_report,
)
from .shadow_mode_contract import validate_shadow_mode_run

VALID_PROBE_MODES = {"read_only", "dry_run"}
VALID_EXECUTOR_REFS = {"tool_registry.execute_call", "ToolRegistry.execute_call", "registry.execute_call"}


def validate_shadow_mode_runtime(facts: dict[str, Any]) -> OfflineContractValidation:
    findings: list[dict[str, object]] = []
    _validate_runtime_identity(facts, findings)
    _validate_phase5_probe_refs(facts, findings)
    _validate_comparison_artifacts(facts, findings)
    _validate_nested_shadow_facts(facts, findings)
    _validate_runtime_no_execution(facts, findings)
    return validation_report(findings)


def _validate_runtime_identity(facts: dict[str, Any], findings: list[dict[str, object]]) -> None:
    if text(facts.get("stage")) != "shadow_mode" or text(facts.get("mode")) != "shadow":
        findings.append(finding("SHADOW_RUNTIME_STAGE_INVALID", _run_extra(facts)))
    if not text(facts.get("run_id")):
        findings.append(finding("SHADOW_RUNTIME_RUN_ID_MISSING", _run_extra(facts)))


def _validate_phase5_probe_refs(facts: dict[str, Any], findings: list[dict[str, object]]) -> None:
    probes = dict_items(facts.get("real_tool_probe_refs"))
    if not probes:
        findings.append(finding("SHADOW_RUNTIME_PHASE5_PROBE_MISSING", _run_extra(facts)))
        return
    if not any(_valid_phase5_probe(probe) for probe in probes):
        findings.append(finding("SHADOW_RUNTIME_PHASE5_PROBE_INVALID", _run_extra(facts)))


def _valid_phase5_probe(probe: dict[str, Any]) -> bool:
    return bool(
        text(probe.get("probe_id"))
        and text(probe.get("contract_ref"))
        and probe.get("validation_ok") is True
        and text(probe.get("mode")) in VALID_PROBE_MODES
        and text(probe.get("tool_executor_ref")) in VALID_EXECUTOR_REFS
    )


def _validate_comparison_artifacts(facts: dict[str, Any], findings: list[dict[str, object]]) -> None:
    artifacts = dict_items(facts.get("comparison_artifacts"))
    if any(text(item.get("artifact_ref")) and item.get("exists") is True for item in artifacts):
        return
    findings.append(finding("SHADOW_RUNTIME_COMPARISON_ARTIFACT_MISSING", _run_extra(facts)))


def _validate_nested_shadow_facts(facts: dict[str, Any], findings: list[dict[str, object]]) -> None:
    shadow_facts = facts.get("shadow_facts")
    if not isinstance(shadow_facts, dict):
        findings.append(finding("SHADOW_RUNTIME_FACTS_MISSING", _run_extra(facts)))
        return
    findings.extend(validate_shadow_mode_run(shadow_facts).findings)


def _validate_runtime_no_execution(facts: dict[str, Any], findings: list[dict[str, object]]) -> None:
    if dict_items(facts.get("executed_actions")):
        findings.append(finding("SHADOW_RUNTIME_SIDE_EFFECT_EXECUTED", _run_extra(facts)))


def _run_extra(facts: dict[str, Any]) -> dict[str, object]:
    return {"run_id": text(facts.get("run_id"))}


__all__ = ["validate_shadow_mode_runtime"]
