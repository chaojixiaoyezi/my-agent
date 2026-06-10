
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .models import GateDecision
from .tool_approval_binding import ApprovalBindingFacts, evaluate_approval_binding_gate
from .tool_idempotency_ledger import IdempotencyLedgerFacts, evaluate_idempotency_ledger_gate


@dataclass(frozen=True)
class ToolEffectFacts:
    tool_name: str
    effect: object = ""
    mode: object = ""
    idempotency_key: object = ""
    approval_id: object = ""


@dataclass(frozen=True)
class ToolGatePolicy:
    tool_effects: Mapping[str, object] = field(default_factory=dict)
    tool_modes: Mapping[str, object] = field(default_factory=dict)
    approved_actions: tuple[object, ...] = ()
    idempotency_ledger: tuple[object, ...] = ()
    run_id: str = ""


def evaluate_tool_effect_gate(facts: ToolEffectFacts | Mapping[str, object]) -> GateDecision:
    item = _effect_facts(facts)
    name = str(item.tool_name or "").strip()
    effect = str(item.effect or "").strip().lower()
    if not effect:
        return GateDecision.deny("tool_effect", "TOOL_EFFECT_MISSING", evidence={"tool_name": name})
    if effect not in {"read_only", "mutating", "dangerous"}:
        return GateDecision.deny("tool_effect", "TOOL_EFFECT_INVALID", evidence={"tool_name": name, "effect": effect})
    mode = normalized_tool_mode(item.mode, effect)
    if mode not in {"read_only", "dry_run", "real"}:
        return GateDecision.deny("tool_effect", "TOOL_MODE_INVALID", evidence={"tool_name": name, "mode": mode})
    key = str(item.idempotency_key or "").strip()
    if effect in {"mutating", "dangerous"} and not key:
        return GateDecision.deny("tool_effect", "TOOL_IDEMPOTENCY_KEY_MISSING", evidence={"tool_name": name})
    approval = str(item.approval_id or "").strip()
    if effect == "dangerous" and mode == "real" and not approval:
        return GateDecision.need_approval("tool_effect", evidence={"tool_name": name, "idempotency_key": key, "mode": mode})
    return GateDecision.allow(
        "tool_effect",
        evidence={"tool_name": name, "effect": effect, "mode": mode, "idempotency_key": key, "approval_id": approval},
    )


def tool_effect_decision(payload: object, call: Any, policy: ToolGatePolicy | None) -> GateDecision | None:
    if policy is None:
        return None
    effect = policy.tool_effects.get(call.tool_name)
    if effect is None:
        return None
    mode = mode_for_call(payload, call.tool_name, policy.tool_modes, effect)
    args_hash = args_hash_for_call(call.input)
    approval_id = ""
    if str(effect or "").strip().lower() == "dangerous" and normalized_tool_mode(mode, effect) == "real":
        approval_decision = evaluate_approval_binding_gate(
            ApprovalBindingFacts(
                tool_name=call.tool_name,
                run_id=_run_id_for_call(payload, policy),
                operation_id=call.operation_id,
                idempotency_key=call.idempotency_key,
                args_hash=args_hash,
                approved_actions=policy.approved_actions,
            )
        )
        if approval_decision.status == "DENY":
            return approval_decision
        if approval_decision.allowed:
            approval_id = str(approval_decision.evidence.get("approval_id") or "")
    effect_decision = evaluate_tool_effect_gate(
        ToolEffectFacts(
            tool_name=call.tool_name,
            effect=effect,
            mode=mode,
            idempotency_key=call.idempotency_key,
            approval_id=approval_id,
        )
    )
    if not effect_decision.allowed:
        return effect_decision
    return evaluate_idempotency_ledger_gate(
        IdempotencyLedgerFacts(
            tool_name=call.tool_name,
            effect=effect,
            idempotency_key=call.idempotency_key,
            args_hash=args_hash,
            operation_id=call.operation_id,
            ledger_records=policy.idempotency_ledger,
        )
    )


def mode_for_call(payload: object, tool_name: str, tool_modes: Mapping[str, object], effect: object) -> object:
    if tool_name in tool_modes:
        return tool_modes[tool_name]
    if not isinstance(payload, dict):
        return normalized_tool_mode("", str(effect or ""))
    explicit = _explicit_payload_mode(payload)
    if explicit:
        return explicit
    if isinstance(payload.get("apply"), bool):
        return "real" if payload.get("apply") is True else "dry_run"
    return normalized_tool_mode("", str(effect or ""))


def normalized_tool_mode(mode: object, effect: object) -> str:
    value = str(mode or "").strip().lower()
    if value in {"read_only", "dry_run", "real"}:
        return value
    if value:
        return value
    return "read_only" if str(effect or "").strip().lower() == "read_only" else "real"


def approved_action_id(facts: ApprovalBindingFacts) -> str:
    for item in facts.approved_actions:
        if _approved_action_matches(item, facts):
            return str(item.get("approval_id") or "").strip()
    return ""


def args_hash_for_call(input_payload: object) -> str:
    encoded = json.dumps(input_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _effect_facts(value: ToolEffectFacts | Mapping[str, object]) -> ToolEffectFacts:
    if isinstance(value, ToolEffectFacts):
        return value
    return ToolEffectFacts(
        tool_name=str(value.get("tool_name") or ""),
        effect=value.get("effect", ""),
        mode=value.get("mode", ""),
        idempotency_key=value.get("idempotency_key", ""),
        approval_id=value.get("approval_id", ""),
    )


def _explicit_payload_mode(payload: dict[str, object]) -> object:
    execution_mode = str(payload.get("execution_mode") or "").strip()
    if execution_mode:
        return payload.get("execution_mode")
    mode = str(payload.get("mode") or "").strip()
    if mode and _looks_like_execution_mode(mode):
        return payload.get("mode")
    return ""


def _looks_like_execution_mode(value: object) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    return normalized_tool_mode(text, "mutating") in {"read_only", "dry_run", "real"}


def _approved_action_matches(
    item: object,
    facts: ApprovalBindingFacts,
) -> bool:
    if not isinstance(item, Mapping):
        return False
    status = str(item.get("status") or "").strip()
    if status and status != "APPROVED":
        return False
    if str(item.get("tool_name") or "").strip() != facts.tool_name:
        return False
    key = str(item.get("idempotency_key") or "").strip()
    if not key or key != facts.idempotency_key:
        return False
    if facts.run_id or item.get("run_id"):
        if str(item.get("run_id") or "").strip() != facts.run_id:
            return False
    if facts.operation_id or item.get("operation_id"):
        if str(item.get("operation_id") or "").strip() != facts.operation_id:
            return False
    if facts.args_hash or item.get("args_hash"):
        if str(item.get("args_hash") or "").strip() != facts.args_hash:
            return False
    return True


def _run_id_for_call(payload: object, policy: ToolGatePolicy) -> str:
    if isinstance(payload, Mapping):
        value = payload.get("run_id")
        if value:
            return str(value)
    return str(policy.run_id or "")


__all__ = [
    "ToolEffectFacts",
    "ToolGatePolicy",
    "approved_action_id",
    "args_hash_for_call",
    "evaluate_tool_effect_gate",
    "mode_for_call",
    "normalized_tool_mode",
    "tool_effect_decision",
]
