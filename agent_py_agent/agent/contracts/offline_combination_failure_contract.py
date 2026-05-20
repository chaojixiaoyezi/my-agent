# LLM: Offline combination-failure contracts verify multi-factor false-success paths.
# 模块用途: 组合校验工具失败、产物缺工具证据、审批篡改、compact 重复和父子任务收口。

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


# LLM: validate_combination_failures checks cross-contract failure combinations.
# 函数用途: 用结构化事实组合验证假完成路径，不针对某个真实任务写专项规则。
def validate_combination_failures(facts: dict[str, Any]) -> OfflineContractValidation:
    findings: list[dict[str, object]] = []
    _validate_tool_failed_claimed_success(facts, findings)
    _validate_artifact_tool_evidence(facts, findings)
    _validate_approval_binding(facts, findings)
    _validate_compact_repeat(facts, findings)
    _validate_parent_child_closeout(facts, findings)
    return validation_report(findings)


# LLM: _validate_tool_failed_claimed_success blocks model success claims after failed tools.
# 函数用途: tool_results 有 ok=false 且 final_claim.claimed_success=true 时返回 finding。
def _validate_tool_failed_claimed_success(facts: dict[str, Any], findings: list[dict[str, object]]) -> None:
    has_failed_tool = any(item.get("ok") is False for item in dict_items(facts.get("tool_results")))
    final_claim = facts.get("final_claim")
    if has_failed_tool and isinstance(final_claim, dict) and final_claim.get("claimed_success") is True:
        findings.append(finding("TOOL_FAILED_MODEL_CLAIMED_SUCCESS"))


# LLM: _validate_artifact_tool_evidence requires required tools to appear in successful tool results.
# 函数用途: 产物存在但 required_tools 未成功调用时返回 ARTIFACT_WITHOUT_REQUIRED_TOOL。
def _validate_artifact_tool_evidence(facts: dict[str, Any], findings: list[dict[str, object]]) -> None:
    if not any(item.get("exists") is True for item in dict_items(facts.get("artifacts"))):
        return
    required = set(string_tuple(facts.get("required_tools")))
    successful = {text(item.get("tool")) for item in dict_items(facts.get("tool_results")) if item.get("ok") is True}
    if required and not required <= successful:
        findings.append(finding("ARTIFACT_WITHOUT_REQUIRED_TOOL"))


# LLM: _validate_approval_binding rejects execution args that differ from approved args.
# 函数用途: approved_args_hash 与 executed_args_hash 不一致时返回 APPROVAL_ARGS_CHANGED。
def _validate_approval_binding(facts: dict[str, Any], findings: list[dict[str, object]]) -> None:
    approval = facts.get("approval")
    if not isinstance(approval, dict):
        return
    if text(approval.get("approved_args_hash")) != text(approval.get("executed_args_hash")):
        findings.append(finding("APPROVAL_ARGS_CHANGED"))


# LLM: _validate_compact_repeat blocks known no-progress loops after compaction.
# 函数用途: compact.after_compact_repeated_action=true 返回 COMPACT_REPEAT_AFTER_COMPACT。
def _validate_compact_repeat(facts: dict[str, Any], findings: list[dict[str, object]]) -> None:
    compact = facts.get("compact")
    if isinstance(compact, dict) and compact.get("after_compact_repeated_action") is True:
        findings.append(finding("COMPACT_REPEAT_AFTER_COMPACT"))


# LLM: _validate_parent_child_closeout requires parent artifacts even when children succeeded.
# 函数用途: 子任务成功但 parent_artifact.required=true 且 exists=false 时返回 finding。
def _validate_parent_child_closeout(facts: dict[str, Any], findings: list[dict[str, object]]) -> None:
    children_succeeded = any(text(item.get("status")) == "SUCCEEDED" for item in dict_items(facts.get("child_tasks")))
    parent = facts.get("parent_artifact")
    if children_succeeded and isinstance(parent, dict) and parent.get("required") is True and parent.get("exists") is not True:
        findings.append(finding("CHILD_OK_PARENT_ARTIFACT_MISSING"))


__all__ = ["validate_combination_failures"]
