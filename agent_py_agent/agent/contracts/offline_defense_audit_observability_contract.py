
from __future__ import annotations

from typing import Any

from .offline_contract_report import (
    OfflineContractValidation,
    dict_items,
    finding,
    positive_int,
    text,
    validation_report,
)

SECRET_FIELD_NAMES = {"api_key", "authorization", "cookie", "password", "secret", "token"}
REQUIRED_AUDIT_FIELDS = ("who", "when", "what", "why", "approval_id")
REQUIRED_METRICS = ("task_duration", "tool_call_count", "failed_tool_count")
REDACTED_VALUES = {"[redacted]", "<redacted>", "***", "redacted"}


def validate_defense_audit_observability(facts: dict[str, Any]) -> OfflineContractValidation:
    findings: list[dict[str, object]] = []
    _validate_defense_actions(facts, findings)
    _validate_audit_records(facts, findings)
    _validate_observability(facts, findings)
    return validation_report(findings)


def _validate_defense_actions(facts: dict[str, Any], findings: list[dict[str, object]]) -> None:
    for action in dict_items(facts.get("defense_actions")):
        if action.get("target_in_whitelist") is True:
            findings.append(finding("DEFENSE_WHITELIST_BLOCK_FORBIDDEN"))
        if 0 < positive_int(action.get("cidr_prefix")) < positive_int(action.get("min_cidr_prefix")):
            findings.append(finding("DEFENSE_CIDR_TOO_BROAD"))
        if text(action.get("decision")) == "auto_block" and positive_int(action.get("risk_score")) < 85:
            findings.append(finding("DEFENSE_RISK_DECISION_INVALID"))
        if positive_int(action.get("evidence_count")) < positive_int(action.get("min_evidence_count")):
            findings.append(finding("DEFENSE_EVIDENCE_INSUFFICIENT"))
        if text(action.get("action")) == "block_ip" and not text(action.get("expire_at")):
            findings.append(finding("DEFENSE_BLOCK_EXPIRY_MISSING"))


def _validate_audit_records(facts: dict[str, Any], findings: list[dict[str, object]]) -> None:
    for record in dict_items(facts.get("audit_records")):
        if any(not text(record.get(field)) for field in REQUIRED_AUDIT_FIELDS):
            findings.append(finding("AUDIT_FIELD_MISSING"))
        if _has_secret_leak(record):
            findings.append(finding("AUDIT_SECRET_LEAK"))
        if not text(record.get("tool_result_ref")) or not record.get("evidence_refs"):
            findings.append(finding("AUDIT_LINK_MISSING"))


def _validate_observability(facts: dict[str, Any], findings: list[dict[str, object]]) -> None:
    if "metrics" not in facts and "stuck_tasks" not in facts:
        return
    metrics = facts.get("metrics") if isinstance(facts.get("metrics"), dict) else {}
    if any(field not in metrics for field in REQUIRED_METRICS):
        findings.append(finding("METRIC_FIELD_MISSING"))
    for task in dict_items(facts.get("stuck_tasks")):
        age = positive_int(task.get("age_ms"))
        threshold = positive_int(task.get("stuck_threshold_ms"))
        if age > threshold > 0 and task.get("alert_emitted") is not True:
            findings.append(finding("STUCK_TASK_ALERT_REQUIRED"))


def _has_secret_leak(value: dict[str, Any]) -> bool:
    for key, child in value.items():
        if key.lower() in SECRET_FIELD_NAMES and text(child).lower() not in REDACTED_VALUES:
            return True
    return False


__all__ = ["validate_defense_audit_observability"]
