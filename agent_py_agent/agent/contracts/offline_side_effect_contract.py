# LLM: Offline side-effect contracts separate read-only, mutating, dangerous, dry-run, and replay modes.
# 模块用途: 校验工具副作用声明、幂等键、只读写入、replay 阻断和 dry-run/real-run 隔离。

from __future__ import annotations

from typing import Any

from .offline_contract_report import OfflineContractValidation, finding, text, validation_report

VALID_EFFECTS = {"read_only", "mutating", "dangerous"}
SIDE_EFFECTS = {"mutating", "dangerous"}


# LLM: validate_side_effects checks structured tool registration/call/result facts.
# 函数用途: 根据 effect、idempotency_key、mode、replay_mode 等字段校验副作用边界。
def validate_side_effects(events: tuple[dict[str, Any], ...]) -> OfflineContractValidation:
    findings: list[dict[str, object]] = []
    for event in events:
        event_type = text(event.get("type"))
        if event_type == "tool_registration":
            _validate_registration(event, findings)
        if event_type == "tool_call":
            _validate_call(event, findings)
        if event_type == "tool_result":
            _validate_result(event, findings)
    return validation_report(findings)


# LLM: _validate_registration requires every tool to declare an effect class.
# 函数用途: 注册事件缺少 read_only/mutating/dangerous 时返回 TOOL_EFFECT_MISSING。
def _validate_registration(event: dict[str, Any], findings: list[dict[str, object]]) -> None:
    if text(event.get("effect")) not in VALID_EFFECTS:
        findings.append(finding("TOOL_EFFECT_MISSING", {"tool": text(event.get("tool"))}))


# LLM: _validate_call rejects read-only writes and non-idempotent side effects.
# 函数用途: read_only 写路径、mutating/dangerous 缺幂等键、replay 中执行副作用都失败。
def _validate_call(event: dict[str, Any], findings: list[dict[str, object]]) -> None:
    effect = text(event.get("effect"))
    if effect == "read_only" and event.get("wrote_paths"):
        findings.append(finding("READ_ONLY_TOOL_SIDE_EFFECT", {"tool": text(event.get("tool"))}))
    if effect in SIDE_EFFECTS and event.get("replay_mode") is True and event.get("executed") is True:
        findings.append(finding("SIDE_EFFECT_REPLAY_BLOCKED", {"tool": text(event.get("tool"))}))
        return
    if effect in SIDE_EFFECTS and not text(event.get("idempotency_key")):
        findings.append(finding("SIDE_EFFECT_IDEMPOTENCY_MISSING", {"tool": text(event.get("tool"))}))


# LLM: _validate_result rejects dry-run results claimed as real execution.
# 函数用途: mode=dry_run 且 claimed_real_execution=true 返回 DRY_RUN_CLAIMED_REAL。
def _validate_result(event: dict[str, Any], findings: list[dict[str, object]]) -> None:
    if text(event.get("mode")) == "dry_run" and event.get("claimed_real_execution") is True:
        findings.append(finding("DRY_RUN_CLAIMED_REAL", {"tool": text(event.get("tool"))}))


__all__ = ["validate_side_effects"]
