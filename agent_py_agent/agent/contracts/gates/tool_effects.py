# LLM: Tool-effect gates enforce side-effect policy from structured tool facts.
# 模块用途: 校验工具 effect/mode/approval/idempotency，供工具入口阻断危险或不可回放的调用。

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .approval_binding import ApprovalBindingFacts, evaluate_approval_binding_gate
from .idempotency_ledger import IdempotencyLedgerFacts, evaluate_idempotency_ledger_gate
from .models import GateDecision


# LLM: ToolEffectFacts bundles side-effect facts to keep gate signatures small.
# 类用途: 保存一个工具调用的副作用类型、执行模式、幂等键和审批号。
@dataclass(frozen=True)
class ToolEffectFacts:
    tool_name: str
    effect: object = ""
    mode: object = ""
    idempotency_key: object = ""
    approval_id: object = ""


# LLM: ToolGatePolicy is the trusted side-effect policy passed to tool-call gates.
# 类用途: 保存工具 effect/mode 映射和已批准动作，不从 prompt 文本推断权限。
@dataclass(frozen=True)
class ToolGatePolicy:
    tool_effects: Mapping[str, object] = field(default_factory=dict)
    tool_modes: Mapping[str, object] = field(default_factory=dict)
    approved_actions: tuple[object, ...] = ()
    idempotency_ledger: tuple[object, ...] = ()
    run_id: str = ""


# LLM: evaluate_tool_effect_gate rejects unsafe side-effect calls before execution.
# 函数用途: 根据结构化 effect/mode/idempotency/approval 字段判断工具调用是否可执行。
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


# LLM: tool_effect_decision builds a ToolEffectFacts object from normalized call data.
# 函数用途: 只读取 policy 映射、payload 结构字段和 protocol 幂等键，返回可选副作用门结果。
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


# LLM: mode_for_call resolves execution mode from trusted mapping or structured payload fields.
# 函数用途: 读取 tool_modes、mode/execution_mode/apply 字段，避免靠自然语言猜 dry-run/real-run。
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


# LLM: normalized_tool_mode maps known aliases to the stable gate vocabulary.
# 函数用途: 把 plan/preview/execute/apply 等结构化模式别名归一到 read_only/dry_run/real。
def normalized_tool_mode(mode: object, effect: object) -> str:
    value = str(mode or "").strip().lower()
    if value in {"plan", "preview", "dry-run"}:
        return "dry_run"
    if value in {"execute", "apply", "real_run"}:
        return "real"
    if value:
        return value
    return "read_only" if str(effect or "").strip().lower() == "read_only" else "real"


# LLM: approved_action_id matches an approved action by tool and idempotency key.
# 函数用途: 防止模型伪造 approval 文本；只有可信 approved_actions 里的匹配项才能放行 real dangerous action。
def approved_action_id(facts: ApprovalBindingFacts) -> str:
    for item in facts.approved_actions:
        if _approved_action_matches(item, facts):
            return str(item.get("approval_id") or item.get("id") or "").strip()
    return ""


# LLM: args_hash_for_call produces the approval/idempotency hash from normalized tool input.
# 函数用途: 对 ToolCallEnvelope.input 做稳定 JSON hash，避免审批和幂等比较读取自然语言。
def args_hash_for_call(input_payload: object) -> str:
    encoded = json.dumps(input_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


# LLM: _effect_facts normalizes mapping and dataclass inputs for the public gate.
# 函数用途: 兼容测试和调用方传入 dict，但输出统一 ToolEffectFacts。
def _effect_facts(value: ToolEffectFacts | Mapping[str, object]) -> ToolEffectFacts:
    if isinstance(value, ToolEffectFacts):
        return value
    return ToolEffectFacts(
        tool_name=str(value.get("tool_name") or value.get("tool") or ""),
        effect=value.get("effect", ""),
        mode=value.get("mode", ""),
        idempotency_key=value.get("idempotency_key", ""),
        approval_id=value.get("approval_id", ""),
    )


# LLM: _explicit_payload_mode reads only stable structured mode fields.
# 函数用途: 从 payload.mode 或 payload.execution_mode 读取模式，不扫描命令或说明文本。
def _explicit_payload_mode(payload: dict[str, object]) -> object:
    for key in ("mode", "execution_mode"):
        if str(payload.get(key) or "").strip():
            return payload.get(key)
    return ""


# LLM: _approved_action_matches checks one trusted approval record.
# 函数用途: 校验审批状态、工具名和幂等键都匹配，避免审批对象被替换。
def _approved_action_matches(
    item: object,
    facts: ApprovalBindingFacts,
) -> bool:
    if not isinstance(item, Mapping):
        return False
    status = str(item.get("status") or "").strip().upper()
    if status and status != "APPROVED":
        return False
    if str(item.get("tool") or item.get("tool_name") or "").strip() != facts.tool_name:
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


# LLM: _run_id_for_call prefers structured payload scope then policy scope.
# 函数用途: 从 payload.run_id 或 ToolGatePolicy.run_id 获取审批绑定的 run_id。
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
