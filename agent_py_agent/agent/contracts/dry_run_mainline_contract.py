# LLM: Dry-run mainline contracts prove a task can inspect, evidence, and report without real side effects.
# 模块用途: 校验主代理 dry-run 主线的输入、工具、产物、证据和动作模式，避免假完成或真实变更。

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


# LLM: validate_dry_run_mainline checks structured task facts for any dry-run workflow.
# 函数用途: 只读取 inputs/tool_results/artifact_refs/evidence_refs/action_intents/executed_actions 等结构字段。
def validate_dry_run_mainline(facts: dict[str, Any]) -> OfflineContractValidation:
    findings: list[dict[str, object]] = []
    _validate_inputs(facts, findings)
    _validate_required_tools(facts, findings)
    _validate_artifacts(facts, findings)
    _validate_evidence(facts, findings)
    _validate_dry_run_actions(facts, findings)
    return validation_report(findings)


# LLM: _validate_inputs requires each declared input to be present as a structured value.
# 函数用途: required_inputs 中的字段必须在 inputs 里有非空值。
def _validate_inputs(facts: dict[str, Any], findings: list[dict[str, object]]) -> None:
    inputs = facts.get("inputs") if isinstance(facts.get("inputs"), dict) else {}
    missing = tuple(name for name in string_tuple(facts.get("required_inputs")) if not text(inputs.get(name)))
    if missing:
        findings.append(finding("DRY_RUN_REQUIRED_INPUT_MISSING", {"fields": missing}))


# LLM: _validate_required_tools requires successful read-only or dry-run results for declared tools.
# 函数用途: required_tools 中每个工具必须有 ok=true 且 mode 不是 real_run 的 tool_result。
def _validate_required_tools(facts: dict[str, Any], findings: list[dict[str, object]]) -> None:
    successful = {text(item.get("tool")) for item in dict_items(facts.get("tool_results")) if _tool_result_ok(item)}
    missing = tuple(name for name in string_tuple(facts.get("required_tools")) if name not in successful)
    if missing:
        findings.append(finding("DRY_RUN_REQUIRED_TOOL_FAILED", {"tools": missing}))


# LLM: _validate_artifacts requires at least one non-empty artifact ref for dry-run output.
# 函数用途: 产物必须是结构化 ref 且 bytes 大于 0。
def _validate_artifacts(facts: dict[str, Any], findings: list[dict[str, object]]) -> None:
    if any(text(item.get("ref")) and positive_int(item.get("bytes")) > 0 for item in dict_items(facts.get("artifact_refs"))):
        return
    findings.append(finding("DRY_RUN_ARTIFACT_EMPTY"))


# LLM: _validate_evidence requires evidence to point at tool traces, artifacts, or external evidence IDs.
# 函数用途: evidence_refs 不能只有标题或自然语言声明，必须有 source_type/source_ref。
def _validate_evidence(facts: dict[str, Any], findings: list[dict[str, object]]) -> None:
    for item in dict_items(facts.get("evidence_refs")):
        if text(item.get("source_type")) and text(item.get("source_ref")):
            continue
        findings.append(finding("DRY_RUN_EVIDENCE_SOURCE_MISSING", {"evidence_id": text(item.get("evidence_id"))}))
        return
    if not dict_items(facts.get("evidence_refs")):
        findings.append(finding("DRY_RUN_EVIDENCE_SOURCE_MISSING"))


# LLM: _validate_dry_run_actions separates planned dry-run intents from forbidden real executions.
# 函数用途: dry-run 主线里 intent 必须 mode=dry_run，executed_actions 不能出现 real_run。
def _validate_dry_run_actions(facts: dict[str, Any], findings: list[dict[str, object]]) -> None:
    if any(text(item.get("mode")) not in {"", "dry_run"} for item in dict_items(facts.get("action_intents"))):
        findings.append(finding("DRY_RUN_INTENT_NOT_DRY_RUN"))
    if any(text(item.get("mode")) == "real_run" or item.get("real_run") is True for item in dict_items(facts.get("executed_actions"))):
        findings.append(finding("DRY_RUN_REAL_ACTION_EXECUTED"))


# LLM: _tool_result_ok accepts only explicit successful non-real tool results.
# 函数用途: 工具结果 ok=true 且 mode 不等于 real_run 才算满足 dry-run 合同。
def _tool_result_ok(item: dict[str, Any]) -> bool:
    return item.get("ok") is True and text(item.get("mode")) != "real_run" and bool(text(item.get("operation_id")))


__all__ = ["validate_dry_run_mainline"]
