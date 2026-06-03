
from __future__ import annotations

from typing import Any

from .offline_contract_report import (
    OfflineContractValidation,
    dict_items,
    finding,
    positive_int,
    string_tuple,
    text,
    validation_report,
)


def validate_dry_run_mainline(facts: dict[str, Any]) -> OfflineContractValidation:
    findings: list[dict[str, object]] = []
    _validate_inputs(facts, findings)
    _validate_required_tools(facts, findings)
    _validate_artifacts(facts, findings)
    _validate_evidence(facts, findings)
    _validate_dry_run_actions(facts, findings)
    return validation_report(findings)


def _validate_inputs(facts: dict[str, Any], findings: list[dict[str, object]]) -> None:
    inputs = facts.get("inputs") if isinstance(facts.get("inputs"), dict) else {}
    missing = tuple(name for name in string_tuple(facts.get("required_inputs")) if not text(inputs.get(name)))
    if missing:
        findings.append(finding("DRY_RUN_REQUIRED_INPUT_MISSING", {"fields": missing}))


def _validate_required_tools(facts: dict[str, Any], findings: list[dict[str, object]]) -> None:
    successful = {text(item.get("tool")) for item in dict_items(facts.get("tool_results")) if _tool_result_ok(item)}
    missing = tuple(name for name in string_tuple(facts.get("required_tools")) if name not in successful)
    if missing:
        findings.append(finding("DRY_RUN_REQUIRED_TOOL_FAILED", {"tools": missing}))


def _validate_artifacts(facts: dict[str, Any], findings: list[dict[str, object]]) -> None:
    if any(text(item.get("ref")) and positive_int(item.get("bytes")) > 0 for item in dict_items(facts.get("artifact_refs"))):
        return
    findings.append(finding("DRY_RUN_ARTIFACT_EMPTY"))


def _validate_evidence(facts: dict[str, Any], findings: list[dict[str, object]]) -> None:
    for item in dict_items(facts.get("evidence_refs")):
        if text(item.get("source_type")) and text(item.get("source_ref")):
            continue
        findings.append(finding("DRY_RUN_EVIDENCE_SOURCE_MISSING", {"evidence_id": text(item.get("evidence_id"))}))
        return
    if not dict_items(facts.get("evidence_refs")):
        findings.append(finding("DRY_RUN_EVIDENCE_SOURCE_MISSING"))


def _validate_dry_run_actions(facts: dict[str, Any], findings: list[dict[str, object]]) -> None:
    if any(text(item.get("mode")) not in {"", "dry_run"} for item in dict_items(facts.get("action_intents"))):
        findings.append(finding("DRY_RUN_INTENT_NOT_DRY_RUN"))
    if any(text(item.get("mode")) == "real_run" or item.get("real_run") is True for item in dict_items(facts.get("executed_actions"))):
        findings.append(finding("DRY_RUN_REAL_ACTION_EXECUTED"))


def _tool_result_ok(item: dict[str, Any]) -> bool:
    return item.get("ok") is True and text(item.get("mode")) != "real_run" and bool(text(item.get("operation_id")))


__all__ = ["validate_dry_run_mainline"]
