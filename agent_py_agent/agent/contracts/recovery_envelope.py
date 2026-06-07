
from __future__ import annotations

from typing import Any

from .recovery_actions import RecoveryAction
from .recovery_classification import (
    action_status,
    next_status,
    recommended_action,
    recovery_category,
)
from .recovery_models import RecoveryEnvelope, RecoveryEnvelopeRequest


def recovery_envelope_from_gate_payload(request: RecoveryEnvelopeRequest) -> RecoveryEnvelope | None:
    if request.allowed:
        return None
    normalized = [_finding_payload(item) for item in request.findings if isinstance(item, dict)]
    if not normalized:
        normalized = [{"code": _default_code(request.status), "severity": "P1", "message": "", "evidence": {}}]
    actions = tuple(_recovery_action(request.gate, request.status, item) for item in normalized)
    envelope_status = _envelope_status(request.status, actions)
    return RecoveryEnvelope(
        status=envelope_status,
        can_auto_repair=envelope_status == "repair_required",
        requires_user=envelope_status == "needs_user_input",
        terminal=envelope_status == "blocked",
        next_status=next_status(envelope_status),
        recommended_action=request.recommended_action or _first_action(actions),
        finding_codes=tuple(dict.fromkeys(str(item["code"]) for item in actions if str(item.get("code") or ""))),
        actions=actions,
        message_zh=_envelope_message(request.gate, envelope_status, actions),
        evidence=dict(request.evidence or {}),
    )


def recovery_actions_from_gate_decisions(decisions: list[Any] | tuple[Any, ...]) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for payload in _decision_payloads(decisions):
        _append_recovery_actions(actions, seen, payload)
    return actions


def _decision_payloads(decisions: list[Any] | tuple[Any, ...]) -> list[dict[str, Any]]:
    return [payload for decision in decisions if isinstance(payload := _decision_payload(decision), dict)]


def _decision_payload(decision: Any) -> dict[str, Any] | None:
    payload = decision.to_dict() if hasattr(decision, "to_dict") else decision
    return payload if isinstance(payload, dict) else None


def _append_recovery_actions(
    actions: list[dict[str, Any]],
    seen: set[tuple[str, str, str]],
    payload: dict[str, Any],
) -> None:
    recovery = payload.get("recovery")
    raw_actions = recovery.get("actions") if isinstance(recovery, dict) else None
    for action in raw_actions if isinstance(raw_actions, list) else []:
        if isinstance(action, dict):
            _append_unique_action(actions, seen, action)


def _append_unique_action(
    actions: list[dict[str, Any]],
    seen: set[tuple[str, str, str]],
    action: dict[str, Any],
) -> None:
    identity = (
        str(action.get("source_gate") or ""),
        str(action.get("code") or ""),
        str(action.get("recommended_action") or ""),
    )
    if identity in seen:
        return
    seen.add(identity)
    actions.append(dict(action))


def _finding_payload(item: dict[str, Any]) -> dict[str, Any]:
    evidence = item.get("evidence")
    return {
        "code": str(item.get("code") or "").strip() or "CONTRACT_FINDING",
        "severity": str(item.get("severity") or "P1").strip(),
        "message": str(item.get("message") or "").strip(),
        "evidence": dict(evidence) if isinstance(evidence, dict) else {},
    }


def _recovery_action(gate: str, status: str, finding: dict[str, Any]) -> dict[str, Any]:
    original_code = str(finding.get("code") or "CONTRACT_FINDING").upper()
    evidence = dict(finding.get("evidence") or {})
    code = _primary_code(original_code, evidence)
    category = recovery_category(code)
    classified = action_status(status, code)
    action = recommended_action(code, classified)
    return {
        "code": code,
        "source_code": original_code,
        "source_gate": gate,
        "category": category,
        "retryable": classified == "repair_required",
        "can_auto_repair": classified == "repair_required",
        "requires_user": classified == "needs_user_input",
        "terminal": classified == "blocked",
        "next_status": next_status(classified),
        "recommended_action": action,
        "recovery_hint": _hint_zh(category, classified, action),
        "message_zh": _action_message_zh(gate, code, classified, action),
        "finding_codes": [code] if code == original_code else [original_code, code],
        "evidence": evidence,
    }


def _primary_code(code: str, evidence: dict[str, Any]) -> str:
    child_code = str(evidence.get("child_code") or "").strip().upper()
    if child_code and code.startswith(("FINAL_CLOSEOUT_", "ACCEPTANCE_")):
        return child_code
    return code


def _envelope_status(status: str, actions: tuple[dict[str, Any], ...]) -> str:
    statuses = {str(item.get("next_status") or "") for item in actions}
    if "WAITING_APPROVAL" in statuses or "WAITING_USER" in statuses:
        return "needs_user_input"
    if "REPAIRING" in statuses:
        return "repair_required"
    if "RECOVERING" in statuses:
        return "recovering"
    normalized = str(status or "").strip().upper()
    if normalized == "NEED_APPROVAL":
        return "needs_user_input"
    if normalized == "RECOVERING":
        return "recovering"
    if normalized == "NEED_REPAIR":
        return "repair_required"
    return "blocked"


def _first_action(actions: tuple[dict[str, Any], ...]) -> str:
    for action in actions:
        value = str(action.get("recommended_action") or "").strip()
        if value:
            return value
    return RecoveryAction.REPORT_BLOCKER.value


def _default_code(status: str) -> str:
    normalized = str(status or "").strip()
    if normalized == "NEED_REPAIR":
        return "CONTRACT_REPAIR_REQUIRED"
    if normalized == "NEED_APPROVAL":
        return "APPROVAL_REQUIRED"
    if normalized == "RECOVERING":
        return "RECOVERY_REQUIRED"
    return "CONTRACT_BLOCKED"


def _envelope_message(gate: str, status: str, actions: tuple[dict[str, Any], ...]) -> str:
    codes = ", ".join(str(item.get("code") or "") for item in actions if str(item.get("code") or ""))
    if status == "repair_required":
        return f"合同门 {gate} 未通过，可自动返工；请按结构化动作修复后重新验收。finding={codes}"
    if status == "needs_user_input":
        return f"合同门 {gate} 需要用户确认或审批；不能自动继续执行。finding={codes}"
    if status == "recovering":
        return f"合同门 {gate} 需要先恢复账本或 checkpoint；恢复完成后再继续。finding={codes}"
    return f"合同门 {gate} 已阻断；当前不能安全自动继续。finding={codes}"


def _action_message_zh(gate: str, code: str, status: str, action: str) -> str:
    if status == "repair_required":
        return f"{gate} 发现 {code}，请执行 {action} 后重新验收。"
    if status == "needs_user_input":
        return f"{gate} 发现 {code}，需要用户确认或审批后才能继续。"
    if status == "recovering":
        return f"{gate} 发现 {code}，需要先从 checkpoint 或账本恢复。"
    return f"{gate} 发现 {code}，已停止自动继续。"


def _hint_zh(category: str, status: str, action: str) -> str:
    if status == "needs_user_input":
        return "该问题不能由模型自行绕过；请通过用户输入、审批记录或权限变更解决。"
    if status == "recovering":
        return "先恢复结构化状态、runlog、tool trace 或 checkpoint，恢复成功后再重新验收。"
    if status == "blocked":
        return "当前继续执行有风险或缺少不可替代事实；请记录阻塞原因并停止自动重试。"
    if category == "evidence":
        return "补齐结构化 source_refs、claims、时间窗口、字段来源或目标语言字段，再重新验收。"
    if category == "artifact":
        return "修复或重建目标产物，并确保产物路径、格式、内容和 provenance 都满足合同。"
    if category == "tool":
        return "按工具 schema、可用工具清单和运行时门修正工具调用后再执行。"
    if category == "path":
        return "把路径修正到允许工作区或已授权 root 内，不要使用越界、符号链接逃逸或未授权路径。"
    if category == "contract":
        return "修复有效合同的结构化字段，确保合同 schema、规则和产物声明可执行。"
    return f"按 {action} 修复结构化 finding 后重新验收。"


__all__ = [
    "RecoveryEnvelope",
    "RecoveryEnvelopeRequest",
    "recovery_actions_from_gate_decisions",
    "recovery_envelope_from_gate_payload",
]
