# LLM: Offline prompt/context contracts ensure machine rules survive assembly and truncation.
# 模块用途: 校验合同字段、工具 schema、非可信输入和记忆不能覆盖机器合同。

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


# LLM: validate_prompt_context checks structured context assembly facts.
# 函数用途: 用 required_contract_fields、assembled_context、tool_schemas 等字段校验上下文，不读 prompt 文本。
def validate_prompt_context(facts: dict[str, Any]) -> OfflineContractValidation:
    findings: list[dict[str, object]] = []
    _validate_required_fields(facts, findings)
    _validate_tool_schemas(facts, findings)
    _validate_untrusted_sources(facts, findings)
    return validation_report(findings)


# LLM: _validate_required_fields ensures required contract facts are present and not truncated.
# 函数用途: 必需字段缺失返回 CONTEXT_CONTRACT_FIELD_MISSING，被截断返回 CONTEXT_CONTRACT_TRUNCATED。
def _validate_required_fields(facts: dict[str, Any], findings: list[dict[str, object]]) -> None:
    context = facts.get("assembled_context") if isinstance(facts.get("assembled_context"), dict) else {}
    truncated = set(string_tuple(facts.get("truncated_fields")))
    for field in string_tuple(facts.get("required_contract_fields")):
        if field not in context:
            findings.append(finding("CONTEXT_CONTRACT_FIELD_MISSING", {"field": field}))
    if truncated & set(string_tuple(facts.get("required_contract_fields"))):
        findings.append(finding("CONTEXT_CONTRACT_TRUNCATED", {"fields": tuple(sorted(truncated))}))


# LLM: _validate_tool_schemas requires visible schemas for all allowed tools.
# 函数用途: allowed_tools 中的工具必须出现在 tool_schemas。
def _validate_tool_schemas(facts: dict[str, Any], findings: list[dict[str, object]]) -> None:
    schemas = facts.get("tool_schemas") if isinstance(facts.get("tool_schemas"), dict) else {}
    missing = sorted(set(string_tuple(facts.get("allowed_tools"))) - {text(item) for item in schemas})
    if missing:
        findings.append(finding("CONTEXT_TOOL_SCHEMA_MISSING", {"tools": tuple(missing)}))


# LLM: _validate_untrusted_sources blocks user/tool/memory text from mutating contracts.
# 函数用途: untrusted_inputs 或 memory_entries 被标记为改写合同事实时返回 finding。
def _validate_untrusted_sources(facts: dict[str, Any], findings: list[dict[str, object]]) -> None:
    if any(item.get("applied_to_contract") is True for item in dict_items(facts.get("untrusted_inputs"))):
        findings.append(finding("UNTRUSTED_INPUT_OVERRIDES_CONTRACT"))
    if any(item.get("attempted_contract_override") is True for item in dict_items(facts.get("memory_entries"))):
        findings.append(finding("MEMORY_OVERRIDES_CONTRACT"))


__all__ = ["validate_prompt_context"]
