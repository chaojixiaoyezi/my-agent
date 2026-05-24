# LLM: Offline model-adapter contracts normalize provider quirks into bounded machine facts.
# 模块用途: 校验 tool_call_id、流式半截 JSON、模型错误重试预算和模型切换 schema。

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


# LLM: validate_model_adapter_facts checks provider-adapter facts without calling a model.
# 函数用途: 用 tool_calls、stream、model_errors、retry_limit、model_switch 校验模型适配边界。
def validate_model_adapter_facts(facts: dict[str, Any]) -> OfflineContractValidation:
    findings: list[dict[str, object]] = []
    _validate_tool_call_ids(facts, findings)
    _validate_stream(facts, findings)
    _validate_retry_budget(facts, findings)
    _validate_model_switch(facts, findings)
    return validation_report(findings)


# LLM: _validate_tool_call_ids requires provider or generated ids.
# 函数用途: tool_calls 缺 tool_call_id 且缺 generated_tool_call_id 时返回 TOOL_CALL_ID_MISSING。
def _validate_tool_call_ids(facts: dict[str, Any], findings: list[dict[str, object]]) -> None:
    for call in dict_items(facts.get("tool_calls")):
        if not text(call.get("tool_call_id")) and not text(call.get("generated_tool_call_id")):
            findings.append(finding("TOOL_CALL_ID_MISSING", {"tool": text(call.get("tool_name"))}))
            return


# LLM: _validate_stream rejects incomplete streaming JSON fragments.
# 函数用途: stream.complete=false 且 partial_json=true 返回 STREAMING_JSON_INCOMPLETE。
def _validate_stream(facts: dict[str, Any], findings: list[dict[str, object]]) -> None:
    stream = facts.get("stream")
    if isinstance(stream, dict) and stream.get("complete") is False and stream.get("partial_json") is True:
        findings.append(finding("STREAMING_JSON_INCOMPLETE"))


# LLM: _validate_retry_budget bounds retryable upstream model failures.
# 函数用途: retryable 错误数超过 retry_limit 时返回 MODEL_RETRY_LIMIT_EXCEEDED。
def _validate_retry_budget(facts: dict[str, Any], findings: list[dict[str, object]]) -> None:
    retry_limit = positive_int(facts.get("retry_limit")) if "retry_limit" in facts else 1
    if retry_limit <= 0:
        return
    retryable_count = sum(1 for item in dict_items(facts.get("model_errors")) if item.get("retryable") is True)
    if retryable_count > retry_limit:
        findings.append(finding("MODEL_RETRY_LIMIT_EXCEEDED", {"attempts": retryable_count}))


# LLM: _validate_model_switch checks schema compatibility when providers change mid-run.
# 函数用途: from_schema 与 to_schema 不一致时返回 MODEL_SWITCH_SCHEMA_MISMATCH。
def _validate_model_switch(facts: dict[str, Any], findings: list[dict[str, object]]) -> None:
    switch = facts.get("model_switch")
    if not isinstance(switch, dict):
        return
    if text(switch.get("from_schema")) != text(switch.get("to_schema")):
        findings.append(finding("MODEL_SWITCH_SCHEMA_MISMATCH"))


__all__ = ["validate_model_adapter_facts"]
