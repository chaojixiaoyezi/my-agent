
from __future__ import annotations

from typing import Any

from .offline_contract_report import (
    OfflineContractValidation,
    dict_items,
    finding,
    string_tuple,
    text,
    validation_report,
)


def validate_combination_failures(facts: dict[str, Any]) -> OfflineContractValidation:
    findings: list[dict[str, object]] = []
    _validate_tool_failed_claimed_success(facts, findings)
    _validate_artifact_tool_evidence(facts, findings)
    _validate_approval_binding(facts, findings)
    _validate_compact_repeat(facts, findings)
    _validate_parent_child_closeout(facts, findings)
    return validation_report(findings)


def _validate_tool_failed_claimed_success(facts: dict[str, Any], findings: list[dict[str, object]]) -> None:
    has_failed_tool = any(item.get("ok") is False for item in dict_items(facts.get("tool_results")))
    final_claim = facts.get("final_claim")
    if has_failed_tool and isinstance(final_claim, dict) and final_claim.get("claimed_success") is True:
        findings.append(finding("TOOL_FAILED_MODEL_CLAIMED_SUCCESS"))


def _validate_artifact_tool_evidence(facts: dict[str, Any], findings: list[dict[str, object]]) -> None:
    if not any(item.get("exists") is True for item in dict_items(facts.get("artifacts"))):
        return
    required = set(string_tuple(facts.get("required_tools")))
    successful = {text(item.get("tool")) for item in dict_items(facts.get("tool_results")) if item.get("ok") is True}
    if required and not required <= successful:
        findings.append(finding("ARTIFACT_WITHOUT_REQUIRED_TOOL"))


def _validate_approval_binding(facts: dict[str, Any], findings: list[dict[str, object]]) -> None:
    approval = facts.get("approval")
    if not isinstance(approval, dict):
        return
    if text(approval.get("approved_args_hash")) != text(approval.get("executed_args_hash")):
        findings.append(finding("APPROVAL_ARGS_CHANGED"))


def _validate_compact_repeat(facts: dict[str, Any], findings: list[dict[str, object]]) -> None:
    compact = facts.get("compact")
    if isinstance(compact, dict) and compact.get("after_compact_repeated_action") is True:
        findings.append(finding("COMPACT_REPEAT_AFTER_COMPACT"))


def _validate_parent_child_closeout(facts: dict[str, Any], findings: list[dict[str, object]]) -> None:
    children_succeeded = any(text(item.get("status")) == "DONE" for item in dict_items(facts.get("child_tasks")))
    parent = facts.get("parent_artifact")
    if children_succeeded and isinstance(parent, dict) and parent.get("required") is True and parent.get("exists") is not True:
        findings.append(finding("CHILD_OK_PARENT_ARTIFACT_MISSING"))


__all__ = ["validate_combination_failures"]
