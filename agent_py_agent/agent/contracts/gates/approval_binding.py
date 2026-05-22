# LLM: Approval binding gates ensure approvals cannot be replayed onto different tool actions.
# 模块用途: 校验 approval 与 tool/run/operation/idempotency/args_hash 的绑定关系，防止审批对象被替换。

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from .models import GateDecision


# LLM: ApprovalBindingFacts carries the action identity that a dangerous real call must match.
# 类用途: 保存真实高危动作与可信 approved_actions 进行 exact-match 所需的结构字段。
@dataclass(frozen=True)
class ApprovalBindingFacts:
    tool_name: str
    run_id: str
    operation_id: str
    idempotency_key: str
    args_hash: str
    approved_actions: tuple[object, ...] = ()


# LLM: evaluate_approval_binding_gate finds an exact approved action record for one tool call.
# 函数用途: 审批必须绑定 tool/run/operation/idempotency/args_hash；任一字段不同都不能执行。
def evaluate_approval_binding_gate(facts: ApprovalBindingFacts | Mapping[str, object]) -> GateDecision:
    item = _binding_facts(facts)
    for action in item.approved_actions:
        if not isinstance(action, Mapping):
            continue
        if not _base_identity_matches(action, item):
            continue
        if _binding_identity(action) == _binding_identity_from_facts(item):
            return GateDecision.allow(
                "approval_binding",
                evidence={"approval_id": str(action.get("approval_id") or action.get("id") or "")},
            )
        return GateDecision.deny("approval_binding", "APPROVAL_BINDING_MISMATCH", evidence=_safe_action_evidence(action))
    return GateDecision.need_approval("approval_binding", "APPROVAL_REQUIRED", evidence={"tool_name": item.tool_name})


# LLM: approval_id_for_binding returns the approval id only when the full binding gate allows.
# 函数用途: 给 tool_effects 复用审批绑定逻辑，避免只按自然语言或单字段 approval_id 放行。
def approval_id_for_binding(facts: ApprovalBindingFacts | Mapping[str, object]) -> str:
    decision = evaluate_approval_binding_gate(facts)
    if decision.allowed:
        return str(decision.evidence.get("approval_id") or "")
    return ""


# LLM: _base_identity_matches narrows records before exact binding comparison.
# 函数用途: 先按 tool 和 idempotency_key 找候选审批，减少错误匹配范围。
def _base_identity_matches(action: Mapping[object, object], facts: ApprovalBindingFacts) -> bool:
    status = str(action.get("status") or "").strip().upper()
    if status and status != "APPROVED":
        return False
    tool = str(action.get("tool") or action.get("tool_name") or "").strip()
    key = str(action.get("idempotency_key") or "").strip()
    return bool(tool == facts.tool_name and key and key == facts.idempotency_key)


# LLM: _binding_identity extracts the four non-tool fields that make approval non-transferable.
# 函数用途: 从 trusted approved_action 里读取绑定字段，缺字段会自然导致 mismatch。
def _binding_identity(action: Mapping[object, object]) -> tuple[str, str, str, str]:
    return (
        str(action.get("run_id") or "").strip(),
        str(action.get("operation_id") or "").strip(),
        str(action.get("idempotency_key") or "").strip(),
        str(action.get("args_hash") or "").strip(),
    )


# LLM: _binding_identity_from_facts keeps approval comparison exact and deterministic.
# 函数用途: 从当前工具调用事实生成绑定元组。
def _binding_identity_from_facts(facts: ApprovalBindingFacts) -> tuple[str, str, str, str]:
    return (
        str(facts.run_id or "").strip(),
        str(facts.operation_id or "").strip(),
        str(facts.idempotency_key or "").strip(),
        str(facts.args_hash or "").strip(),
    )


# LLM: _binding_facts normalizes mapping input for tool registry adapters.
# 函数用途: 支持 dict 输入，但后续只处理 ApprovalBindingFacts。
def _binding_facts(value: ApprovalBindingFacts | Mapping[str, object]) -> ApprovalBindingFacts:
    if isinstance(value, ApprovalBindingFacts):
        return value
    actions = value.get("approved_actions")
    return ApprovalBindingFacts(
        tool_name=str(value.get("tool_name") or value.get("tool") or ""),
        run_id=str(value.get("run_id") or ""),
        operation_id=str(value.get("operation_id") or ""),
        idempotency_key=str(value.get("idempotency_key") or ""),
        args_hash=str(value.get("args_hash") or ""),
        approved_actions=tuple(actions if isinstance(actions, Iterable) and not isinstance(actions, (str, bytes)) else ()),
    )


# LLM: _safe_action_evidence avoids leaking raw command args into gate output.
# 函数用途: 只输出审批绑定字段，不把完整高危参数写入 prompt。
def _safe_action_evidence(action: Mapping[object, object]) -> dict[str, object]:
    return {
        "approval_id": str(action.get("approval_id") or action.get("id") or ""),
        "tool_name": str(action.get("tool") or action.get("tool_name") or ""),
        "run_id": str(action.get("run_id") or ""),
        "operation_id": str(action.get("operation_id") or ""),
        "idempotency_key": str(action.get("idempotency_key") or ""),
        "args_hash": str(action.get("args_hash") or ""),
    }


__all__ = ["ApprovalBindingFacts", "approval_id_for_binding", "evaluate_approval_binding_gate"]
