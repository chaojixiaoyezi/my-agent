
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


def validate_prompt_context(facts: dict[str, Any]) -> OfflineContractValidation:
    findings: list[dict[str, object]] = []
    _validate_required_fields(facts, findings)
    _validate_tool_schemas(facts, findings)
    _validate_untrusted_sources(facts, findings)
    return validation_report(findings)


def _validate_required_fields(facts: dict[str, Any], findings: list[dict[str, object]]) -> None:
    context = facts.get("assembled_context") if isinstance(facts.get("assembled_context"), dict) else {}
    truncated = set(string_tuple(facts.get("truncated_fields")))
    for field in string_tuple(facts.get("required_contract_fields")):
        if field not in context:
            findings.append(finding("CONTEXT_CONTRACT_FIELD_MISSING", {"field": field}))
    if truncated & set(string_tuple(facts.get("required_contract_fields"))):
        findings.append(finding("CONTEXT_CONTRACT_TRUNCATED", {"fields": tuple(sorted(truncated))}))


def _validate_tool_schemas(facts: dict[str, Any], findings: list[dict[str, object]]) -> None:
    schemas = facts.get("tool_schemas") if isinstance(facts.get("tool_schemas"), dict) else {}
    missing = sorted(set(string_tuple(facts.get("allowed_tools"))) - {text(item) for item in schemas})
    if missing:
        findings.append(finding("CONTEXT_TOOL_SCHEMA_MISSING", {"tools": tuple(missing)}))


def _validate_untrusted_sources(facts: dict[str, Any], findings: list[dict[str, object]]) -> None:
    if any(item.get("applied_to_contract") is True for item in dict_items(facts.get("untrusted_inputs"))):
        findings.append(finding("UNTRUSTED_INPUT_OVERRIDES_CONTRACT"))
    if any(item.get("attempted_contract_override") is True for item in dict_items(facts.get("memory_entries"))):
        findings.append(finding("MEMORY_OVERRIDES_CONTRACT"))


__all__ = ["validate_prompt_context"]
