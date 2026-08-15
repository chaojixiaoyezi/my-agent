
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


def validate_model_adapter_facts(facts: dict[str, Any]) -> OfflineContractValidation:
    findings: list[dict[str, object]] = []
    _validate_tool_call_ids(facts, findings)
    _validate_stream(facts, findings)
    _validate_retry_budget(facts, findings)
    _validate_model_switch(facts, findings)
    return validation_report(findings)


def _validate_tool_call_ids(facts: dict[str, Any], findings: list[dict[str, object]]) -> None:
    for call in dict_items(facts.get("tool_calls")):
        if not text(call.get("call_id")):
            findings.append(finding("TOOL_CALL_ID_MISSING", {"tool": text(call.get("tool_name"))}))
            return


def _validate_stream(facts: dict[str, Any], findings: list[dict[str, object]]) -> None:
    stream = facts.get("stream")
    if isinstance(stream, dict) and stream.get("complete") is False and stream.get("partial_json") is True:
        findings.append(finding("STREAMING_JSON_INCOMPLETE"))


def _validate_retry_budget(facts: dict[str, Any], findings: list[dict[str, object]]) -> None:
    retry_limit = positive_int(facts.get("retry_limit")) if "retry_limit" in facts else 1
    if retry_limit <= 0:
        return
    retryable_count = sum(1 for item in dict_items(facts.get("model_errors")) if item.get("retryable") is True)
    if retryable_count > retry_limit:
        findings.append(finding("MODEL_RETRY_LIMIT_EXCEEDED", {"attempts": retryable_count}))


def _validate_model_switch(facts: dict[str, Any], findings: list[dict[str, object]]) -> None:
    switch = facts.get("model_switch")
    if not isinstance(switch, dict):
        return
    if text(switch.get("from_schema")) != text(switch.get("to_schema")):
        findings.append(finding("MODEL_SWITCH_SCHEMA_MISMATCH"))


__all__ = ["validate_model_adapter_facts"]
