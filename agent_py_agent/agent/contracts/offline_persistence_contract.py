
from __future__ import annotations

from typing import Any

from .offline_contract_report import (
    OfflineContractValidation,
    dict_items,
    finding,
    text,
    validation_report,
)


def validate_persistence_consistency(facts: dict[str, Any]) -> OfflineContractValidation:
    findings: list[dict[str, object]] = []
    _validate_state_runlog(facts, findings)
    _validate_tool_trace(facts, findings)
    _validate_artifact_writes(facts, findings)
    _validate_finalizer_recovery(facts, findings)
    return validation_report(findings)


def _validate_state_runlog(facts: dict[str, Any], findings: list[dict[str, object]]) -> None:
    logged = {text(item.get("event_id")) for item in dict_items(facts.get("runlog_entries"))}
    for item in dict_items(facts.get("state_updates")):
        if text(item.get("event_id")) not in logged:
            findings.append(finding("PERSISTENCE_LOG_MISSING"))
            return


def _validate_tool_trace(facts: dict[str, Any], findings: list[dict[str, object]]) -> None:
    traced = {text(item.get("operation_id")) for item in dict_items(facts.get("tool_trace"))}
    for item in dict_items(facts.get("tool_executions")):
        if item.get("executed") is True and text(item.get("operation_id")) not in traced:
            findings.append(finding("TOOL_EXECUTED_TRACE_MISSING"))
            return


def _validate_artifact_writes(facts: dict[str, Any], findings: list[dict[str, object]]) -> None:
    for item in dict_items(facts.get("artifact_writes")):
        if item.get("atomic_complete") is False:
            findings.append(finding("ARTIFACT_PARTIAL_WRITE", {"path": text(item.get("path"))}))
            return


def _validate_finalizer_recovery(facts: dict[str, Any], findings: list[dict[str, object]]) -> None:
    finalizer = facts.get("finalizer")
    if not isinstance(finalizer, dict):
        return
    if finalizer.get("crashed_after_verify") is True and finalizer.get("revalidated_after_recovery") is not True:
        findings.append(finding("FINALIZER_REVALIDATION_REQUIRED"))


__all__ = ["validate_persistence_consistency"]
