# LLM: Long-task recovery contracts gate compact/resume/checkpoint continuity before large real tasks.
# 模块用途: 校验长任务恢复包是否保留运行范围、checkpoint、compact/resume、恢复状态和副作用去重信息。

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


# LLM: validate_long_task_recovery is the single-agent long-task recovery gate.
# 函数用途: 校验长任务恢复包的 scope、checkpoint、compact/resume、恢复状态和副作用账本。
def validate_long_task_recovery(facts: dict[str, Any]) -> OfflineContractValidation:
    findings: list[dict[str, object]] = []
    _validate_identity(facts, findings)
    _validate_checkpoints(facts, findings)
    _validate_compact_cycles(facts, findings)
    _validate_latest_resume_packet(facts, findings)
    _validate_side_effect_ledger(facts, findings)
    return validation_report(findings)


# LLM: _validate_identity requires task, run, and scope refs before recovery.
# 函数用途: 确认恢复包绑定明确 task/run 和 RunScope。
def _validate_identity(facts: dict[str, Any], findings: list[dict[str, object]]) -> None:
    if not text(facts.get("task_id")) or not text(facts.get("run_id")):
        findings.append(finding("LONG_TASK_IDENTITY_MISSING"))
    if not text(facts.get("run_scope_ref")):
        findings.append(finding("LONG_TASK_RUN_SCOPE_MISSING"))


# LLM: _validate_checkpoints requires recoverable state and artifact refs.
# 函数用途: 校验 checkpoint_ref、state_ref 和 artifact_refs，避免只有摘要无法恢复。
def _validate_checkpoints(facts: dict[str, Any], findings: list[dict[str, object]]) -> None:
    checkpoints = dict_items(facts.get("checkpoints"))
    if not checkpoints:
        findings.append(finding("LONG_TASK_CHECKPOINT_MISSING"))
        return
    for checkpoint in checkpoints:
        if not text(checkpoint.get("checkpoint_ref")) or not text(checkpoint.get("state_ref")):
            findings.append(finding("LONG_TASK_CHECKPOINT_REF_MISSING"))
        if not string_tuple(checkpoint.get("artifact_refs")):
            findings.append(finding("LONG_TASK_CHECKPOINT_ARTIFACT_REF_MISSING"))


# LLM: _validate_compact_cycles ties compact apply output to resume packets.
# 函数用途: 校验每次 compact 都同时保留 bundle/apply/resume refs。
def _validate_compact_cycles(facts: dict[str, Any], findings: list[dict[str, object]]) -> None:
    for cycle in dict_items(facts.get("compact_cycles")):
        if not (text(cycle.get("bundle_ref")) and text(cycle.get("apply_ref")) and text(cycle.get("resume_ref"))):
            findings.append(finding("LONG_TASK_COMPACT_RESUME_REF_MISSING", {"cycle_id": text(cycle.get("cycle_id"))}))


# LLM: _validate_latest_resume_packet checks the concrete continuation handoff.
# 函数用途: 校验最新 resume packet 是否能恢复状态并给出下一步动作 refs。
def _validate_latest_resume_packet(facts: dict[str, Any], findings: list[dict[str, object]]) -> None:
    packet = facts.get("latest_resume_packet") if isinstance(facts.get("latest_resume_packet"), dict) else {}
    if not text(packet.get("packet_ref")):
        findings.append(finding("LONG_TASK_RESUME_PACKET_MISSING"))
    if not string_tuple(packet.get("restored_state_refs")):
        findings.append(finding("LONG_TASK_RESTORED_STATE_REF_MISSING"))
    if not string_tuple(packet.get("next_action_refs")):
        findings.append(finding("LONG_TASK_NEXT_ACTION_REF_MISSING"))


# LLM: _validate_side_effect_ledger prevents replaying previous side effects after resume.
# 函数用途: 校验幂等状态存在，并阻断恢复后重放已执行动作。
def _validate_side_effect_ledger(facts: dict[str, Any], findings: list[dict[str, object]]) -> None:
    ledger = facts.get("side_effect_ledger") if isinstance(facts.get("side_effect_ledger"), dict) else {}
    if not text(ledger.get("idempotency_state_ref")):
        findings.append(finding("LONG_TASK_IDEMPOTENCY_STATE_MISSING"))
    if string_tuple(ledger.get("replayed_action_ids")):
        findings.append(finding("LONG_TASK_SIDE_EFFECT_REPLAYED"))


__all__ = ["validate_long_task_recovery"]
