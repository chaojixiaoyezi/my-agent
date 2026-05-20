# LLM: Offline persistence contracts validate state/log/tooltrace/artifact consistency.
# 模块用途: 校验状态变更、工具执行、产物写入和 finalizer 恢复的一致性事实。

from __future__ import annotations

from typing import Any

from .offline_contract_report import (
    OfflineContractValidation,
    dict_items,
    finding,
    text,
    validation_report,
)


# LLM: validate_persistence_consistency checks persistence facts without touching real storage.
# 函数用途: 用 state_updates、runlog_entries、tool_executions、tool_trace、artifact_writes 校验一致性。
def validate_persistence_consistency(facts: dict[str, Any]) -> OfflineContractValidation:
    findings: list[dict[str, object]] = []
    _validate_state_runlog(facts, findings)
    _validate_tool_trace(facts, findings)
    _validate_artifact_writes(facts, findings)
    _validate_finalizer_recovery(facts, findings)
    return validation_report(findings)


# LLM: _validate_state_runlog requires every state event to have a runlog row.
# 函数用途: state_updates[].event_id 不在 runlog_entries[].event_id 时返回 PERSISTENCE_LOG_MISSING。
def _validate_state_runlog(facts: dict[str, Any], findings: list[dict[str, object]]) -> None:
    logged = {text(item.get("event_id")) for item in dict_items(facts.get("runlog_entries"))}
    for item in dict_items(facts.get("state_updates")):
        if text(item.get("event_id")) not in logged:
            findings.append(finding("PERSISTENCE_LOG_MISSING"))
            return


# LLM: _validate_tool_trace requires trace rows for executed tools.
# 函数用途: 已执行 operation_id 未出现在 tool_trace 时返回 TOOL_EXECUTED_TRACE_MISSING。
def _validate_tool_trace(facts: dict[str, Any], findings: list[dict[str, object]]) -> None:
    traced = {text(item.get("operation_id")) for item in dict_items(facts.get("tool_trace"))}
    for item in dict_items(facts.get("tool_executions")):
        if item.get("executed") is True and text(item.get("operation_id")) not in traced:
            findings.append(finding("TOOL_EXECUTED_TRACE_MISSING"))
            return


# LLM: _validate_artifact_writes rejects non-atomic or partial artifact writes.
# 函数用途: artifact_writes[].atomic_complete=false 返回 ARTIFACT_PARTIAL_WRITE。
def _validate_artifact_writes(facts: dict[str, Any], findings: list[dict[str, object]]) -> None:
    for item in dict_items(facts.get("artifact_writes")):
        if item.get("atomic_complete") is False:
            findings.append(finding("ARTIFACT_PARTIAL_WRITE", {"path": text(item.get("path"))}))
            return


# LLM: _validate_finalizer_recovery requires revalidation after finalizer crashes.
# 函数用途: finalizer 崩溃后未 revalidated 时返回 FINALIZER_REVALIDATION_REQUIRED。
def _validate_finalizer_recovery(facts: dict[str, Any], findings: list[dict[str, object]]) -> None:
    finalizer = facts.get("finalizer")
    if not isinstance(finalizer, dict):
        return
    if finalizer.get("crashed_after_verify") is True and finalizer.get("revalidated_after_recovery") is not True:
        findings.append(finding("FINALIZER_REVALIDATION_REQUIRED"))


__all__ = ["validate_persistence_consistency"]
