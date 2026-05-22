# LLM: Delivery closeout gate recovery aggregates failed gate envelopes into executable repair actions.
# 模块用途: 将 run/runtime/quality/final gate 的失败统一写入 contract_recovery 和 recovery_actions。

from __future__ import annotations

from typing import Any

from ..contracts.recovery_envelope import recovery_actions_from_gate_decisions
from .main_agent_delivery_closeout_quality import delivery_quality_payload_ref


def attach_contract_recovery(report: dict[str, Any], decisions: list[Any], *, contract: dict[str, Any]) -> None:
    actions = [action for action in (_enrich_gate_recovery_action(item, contract) for item in recovery_actions_from_gate_decisions(decisions)) if action]
    if not actions:
        return
    report["contract_recovery"] = {
        "status": contract_recovery_status(actions),
        "actions": actions,
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
    return payload


def _attach_quality_repair_refs(payload: dict[str, Any], contract: dict[str, Any]) -> None:
    ref = delivery_quality_payload_ref(contract)
    if ref:
        payload.setdefault("checkpoint_ref", ref)
        payload.setdefault("writer_tool", "write_structured_json")
        payload.setdefault("write_tools", ["write_structured_json"])
    required = _quality_required_fields(contract)
    if required:
        payload.setdefault("required_fields", required)
        payload.setdefault("required_columns", required)


def _quality_required_fields(contract: dict[str, Any]) -> list[str]:
    quality = _quality_contract(contract)
    fields: list[str] = []
    evidence = quality.get("evidence_contract")
    if isinstance(evidence, dict):
        fields.extend(_string_list(evidence.get("required_fields")))
    metrics = quality.get("metric_contracts")
    if isinstance(metrics, list):
        fields.extend(str(item.get("field") or "").strip() for item in metrics if isinstance(item, dict))
    language = quality.get("language_contract")
    if isinstance(language, dict):
        fields.extend(_string_list(language.get("fields")))
    return list(dict.fromkeys(item for item in fields if item))


def _quality_contract(contract: dict[str, Any]) -> dict[str, Any]:
    for key in ("delivery_quality_contract", "quality_contract", "data_contract"):
        value = contract.get(key)
        if isinstance(value, dict):
            return value
    return {}


def _string_list(value: object) -> list[str]:
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


__all__ = ["attach_contract_recovery", "contract_recovery_status", "failed_gate_payloads", "merge_recovery_actions"]
