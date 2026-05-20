# LLM: Offline defense/audit/observability contracts validate generic safety and audit facts.
# 模块用途: 校验防御动作、审计字段/脱敏/关联和可观测指标/卡住告警。

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


# LLM: validate_defense_audit_observability checks structured safety, audit, and metric facts.
# 函数用途: 用 defense_actions、audit_records、metrics、stuck_tasks 校验通用底座，不接真实 CMDB。
def validate_defense_audit_observability(facts: dict[str, Any]) -> OfflineContractValidation:
    findings: list[dict[str, object]] = []
    _validate_defense_actions(facts, findings)
    _validate_audit_records(facts, findings)
    _validate_observability(facts, findings)
    return validation_report(findings)


# LLM: _validate_defense_actions blocks risky generic defense decisions.
# 函数用途: 白名单、过宽 CIDR、风险评分、证据数量和封禁有效期都用结构字段判断。
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


# LLM: _validate_audit_records requires complete, linked, redacted high-risk audit facts.
# 函数用途: 审计字段缺失、敏感字段未脱敏、缺少 tool/evidence 关联都会失败。
def _validate_audit_records(facts: dict[str, Any], findings: list[dict[str, object]]) -> None:
    for record in dict_items(facts.get("audit_records")):
        if any(not text(record.get(field)) for field in REQUIRED_AUDIT_FIELDS):
            findings.append(finding("AUDIT_FIELD_MISSING"))
        if _has_secret_leak(record):
            findings.append(finding("AUDIT_SECRET_LEAK"))
        if not text(record.get("tool_result_ref")) or not record.get("evidence_refs"):
            findings.append(finding("AUDIT_LINK_MISSING"))


# LLM: _validate_observability requires core metrics and stuck-task alerts.
# 函数用途: 缺少核心指标返回 METRIC_FIELD_MISSING，超过阈值未告警返回 STUCK_TASK_ALERT_REQUIRED。
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


# LLM: _has_secret_leak scans structured audit fields for raw secret values.
# 函数用途: 对敏感字段名做精确检查，值不是脱敏占位则为泄露。
def _has_secret_leak(value: dict[str, Any]) -> bool:
    for key, child in value.items():
        if key.lower() in SECRET_FIELD_NAMES and text(child).lower() not in REDACTED_VALUES:
            return True
    return False


__all__ = ["validate_defense_audit_observability"]
