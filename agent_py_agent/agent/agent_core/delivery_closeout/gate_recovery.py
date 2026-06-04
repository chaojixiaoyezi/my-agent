
from __future__ import annotations

from typing import Any

from ...contracts.recovery_envelope import recovery_actions_from_gate_decisions
from ..main_agent_delivery_fact_evidence import fact_evidence_contract, fact_evidence_payload_ref
from .quality import delivery_quality_payload_ref


def attach_contract_recovery(report: dict[str, Any], decisions: list[Any], *, contract: dict[str, Any]) -> None:
    actions = [action for action in (_enrich_gate_recovery_action(item, contract) for item in recovery_actions_from_gate_decisions(decisions)) if action]
    if not actions:
        return
    report["contract_recovery"] = {
        "status": contract_recovery_status(actions),
        "actions": actions,
        "rework_loop": _rework_loop_payload(actions),
        "message_zh": "合同门未通过；可修复项会打回返工，需要用户确认的项会进入等待用户，硬阻断项会停止自动继续。",
    }
    progress = report.setdefault("delivery_progress", {})
    if isinstance(progress, dict):
        progress["recovery_actions"] = merge_recovery_actions(progress.get("recovery_actions"), actions)


def failed_gate_payloads(report: dict[str, Any]) -> list[dict[str, Any]]:
    failed: list[dict[str, Any]] = []
    for key in _CLOSEOUT_GATE_KEYS:
        payload = report.get(key)
        if isinstance(payload, dict) and payload.get("allowed") is not True:
            failed.append(_failed_gate_payload(key, payload))
    return failed


_CLOSEOUT_GATE_KEYS = (
    "run_contract_gate",
    "runtime_gate",
    "state_gate",
    "delivery_quality_gate",
    "fact_evidence_gate",
    "task_progress_closeout_gate",
    "acceptance_gate",
    "final_closeout_gate",
)


def _failed_gate_payload(key: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "gate": payload.get("gate") or key,
        "status": payload.get("status") or "",
        "findings": payload.get("findings") if isinstance(payload.get("findings"), list) else [],
        "recovery": payload.get("recovery") if isinstance(payload.get("recovery"), dict) else {},
    }


def _enrich_gate_recovery_action(action: dict[str, Any], contract: dict[str, Any]) -> dict[str, Any]:
    payload = dict(action)
    if payload.get("source_gate") == "delivery_quality":
        _attach_quality_repair_refs(payload, contract)
    if payload.get("source_gate") == "fact_evidence":
        _attach_fact_evidence_repair_refs(payload, contract)
    return payload


def _attach_quality_repair_refs(payload: dict[str, Any], contract: dict[str, Any]) -> None:
    ref = delivery_quality_payload_ref(contract)
    if ref:
        payload.setdefault("checkpoint_ref", ref)
        payload.setdefault("writer_tool", "write_file")
        payload.setdefault("write_tools", ["write_file"])
    required = _quality_required_fields(contract)
    if required:
        payload.setdefault("required_fields", required)
        payload.setdefault("required_columns", required)


def _quality_required_fields(contract: dict[str, Any]) -> list[str]:
    quality = _quality_contract(contract)
    fields: list[str] = []
    evidence = quality.get("evidence_contract")
    if isinstance(evidence, dict):
        fields.extend(_required_fields_list(evidence.get("required_fields")))
    metrics = quality.get("metric_contracts")
    if isinstance(metrics, list):
        fields.extend(str(item.get("field") or "").strip() for item in metrics if isinstance(item, dict))
    language = quality.get("language_contract")
    if isinstance(language, dict):
        fields.extend(_required_fields_list(language.get("fields")))
    return list(dict.fromkeys(item for item in fields if item))


def _attach_fact_evidence_repair_refs(payload: dict[str, Any], contract: dict[str, Any]) -> None:
    ref = fact_evidence_payload_ref(contract)
    if ref:
        payload.setdefault("checkpoint_ref", ref)
        payload.setdefault("writer_tool", "write_file")
        payload.setdefault("write_tools", ["write_file"])
    payload.setdefault("required_structured_fields", ["source_refs", "claims"])
    if required := _fact_required_fields(contract):
        payload.setdefault("required_fields", required)
        payload.setdefault("required_columns", required)


def _fact_required_fields(contract: dict[str, Any]) -> list[str]:
    fact = fact_evidence_contract(contract)
    evidence = fact.get("evidence_contract") if isinstance(fact, dict) else None
    if isinstance(evidence, dict):
        return _required_fields_list(evidence.get("required_fields"))
    return _required_fields_list(fact.get("required_fields")) if isinstance(fact, dict) else []


def _quality_contract(contract: dict[str, Any]) -> dict[str, Any]:
    for key in ("delivery_quality_contract", "quality_contract", "data_contract"):
        value = contract.get(key)
        if isinstance(value, dict):
            return value
    return {}


def _required_fields_list(value: object) -> list[str]:
    return [text for item in value if (text := str(item).strip())] if isinstance(value, list) else []


def merge_recovery_actions(existing: object, added: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for action in [*(existing if isinstance(existing, list) else []), *added]:
        if isinstance(action, dict):
            _append_unique_recovery_action(merged, seen, action)
    return merged


def _append_unique_recovery_action(
    merged: list[dict[str, Any]],
    seen: set[tuple[str, str, str, str]],
    action: dict[str, Any],
) -> None:
    identity = (
        str(action.get("source_gate") or ""),
        str(action.get("code") or ""),
        str(action.get("recommended_action") or ""),
        str(action.get("checkpoint_ref") or action.get("artifact_path") or ""),
    )
    if identity in seen:
        return
    seen.add(identity)
    merged.append(dict(action))


def contract_recovery_status(actions: list[dict[str, Any]]) -> str:
    if any(action.get("terminal") is True for action in actions):
        return "blocked"
    if any(action.get("requires_user") is True for action in actions):
        return "needs_user_input"
    if any(str(action.get("next_status") or "") == "RECOVERING" for action in actions):
        return "recovering"
    return "repair_required"


def _rework_loop_payload(actions: list[dict[str, Any]]) -> dict[str, Any]:
    status = contract_recovery_status(actions)
    mode, message = _rework_loop_mode(status)
    return {
        "mode": mode,
        "message_zh": message,
        "retryable_action_count": sum(1 for action in actions if action.get("retryable") is not False and not action.get("terminal")),
        "requires_user": any(action.get("requires_user") is True for action in actions),
        "terminal": any(action.get("terminal") is True for action in actions),
    }


def _rework_loop_mode(status: str) -> tuple[str, str]:
    modes = {
        "blocked": ("stop_and_report", "存在不可自动修复的硬阻断，请停止自动继续并报告阻断原因。"),
        "needs_user_input": ("ask_user_then_continue", "需要用户确认或审批；拿到明确回复后再继续，不要假装已经完成。"),
        "recovering": ("recover_then_revalidate", "先按恢复动作恢复运行状态，再重新验收交付物。"),
    }
    return modes.get(
        status,
        ("repair_then_revalidate", "按 actions 修复产物或证据，然后重新跑同一套合同验收；不要因为一次失败就结束任务。"),
    )


__all__ = ["attach_contract_recovery", "contract_recovery_status", "failed_gate_payloads", "merge_recovery_actions"]
