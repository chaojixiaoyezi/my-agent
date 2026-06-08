
from __future__ import annotations

from pathlib import Path
from typing import Any

from ...contracts.error_taxonomy import error_contract
from ...contracts.recovery_envelope import recovery_actions_from_gate_decisions
from ...contracts.staged_checkpoint_acceptance import json_checkpoint_status
from .artifact_repair import (
    append_artifact_finding_repair_actions,
    failed_findings,
)
from .builder_repair import (
    BuilderRepairRequest,
    append_failed_builder_output_actions,
)
from .collection_repair import append_collection_value_repair_actions
from .evidence import fact_evidence_contract, fact_evidence_payload_ref
from .quality import delivery_quality_payload_ref
from .recovery_models import RecoveryActionLedger
from .staging_recovery import (
    append_contract_staging_recovery_actions,
    staging_action_context,
)

_PATH_KEYS = (
    "path",
    "file_path",
    "target_path",
    "output_path",
    "artifact_ref",
    "source_ref",
)

_JSON_WRITER_TOOLS = {"write_file"}
_CLOSEOUT_GATE_KEYS = (
    "run_contract_gate",
    "runtime_gate",
    "state_gate",
    "delivery_quality_gate",
    "fact_evidence_gate",
    "target_coverage_projection_gate",
    "source_fact_consistency_gate",
    "task_progress_closeout_gate",
    "acceptance_gate",
    "final_closeout_gate",
)


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


def merge_recovery_actions(existing: object, added: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for action in [*(existing if isinstance(existing, list) else []), *added]:
        if isinstance(action, dict):
            _append_unique_recovery_action(merged, seen, action)
    return merged


def contract_recovery_status(actions: list[dict[str, Any]]) -> str:
    if any(action.get("terminal") is True for action in actions):
        return "blocked"
    if any(action.get("requires_user") is True for action in actions):
        return "needs_user_input"
    if any(str(action.get("next_status") or "") == "RECOVERING" for action in actions):
        return "recovering"
    return "repair_required"


def _recovery_actions(
    report: dict[str, Any],
    *,
    contract: dict[str, Any],
    workspace_root: Path,
) -> list[dict[str, object]]:
    ledger = RecoveryActionLedger(actions=[], seen=set())
    ledger.actions.extend(_contract_recovery_actions(contract, workspace_root=workspace_root, seen=ledger.seen))
    _append_failure_recovery_actions(report, ledger, contract=contract, workspace_root=workspace_root)
    if ledger.actions:
        return ledger.actions
    return [_generic_recovery_action("ACCEPTANCE_FAILED")]


def _append_failure_recovery_actions(
    report: dict[str, Any],
    ledger: RecoveryActionLedger,
    *,
    contract: dict[str, Any],
    workspace_root: Path,
) -> None:
    append_collection_value_repair_actions(report, contract, ledger)
    append_artifact_finding_repair_actions(report, ledger)
    append_failed_builder_output_actions(
        BuilderRepairRequest(
            report=report,
            contract=contract,
            workspace_root=workspace_root,
            ledger=ledger,
            staging_context=staging_action_context,
        )
    )
    for finding in failed_findings(report):
        recovery_contract = error_contract(_recovery_error_code(str(finding.get("code") or "")))
        if recovery_contract.code in ledger.seen:
            continue
        ledger.seen.add(recovery_contract.code)
        ledger.actions.append(_recovery_contract_action(recovery_contract))


def _recovery_error_code(code: str) -> str:
    upper = str(code or "").upper()
    if upper in {"ARTIFACT_MISSING", "ARTIFACT_EMPTY"}:
        return "ARTIFACT_MISSING"
    if upper.startswith(("STAGED_", "PATH_", "SPREADSHEET_SOURCE_", "EVIDENCE_")):
        return upper
    if upper.startswith(("XLSX_", "CSV_", "JSON_", "PDF_", "HTML_")):
        return "ACCEPTANCE_FAILED"
    return "ACCEPTANCE_FAILED"


def _generic_recovery_action(code: str) -> dict[str, object]:
    return _recovery_contract_action(error_contract(code))


def _recovery_contract_action(contract) -> dict[str, object]:
    return {
        "code": contract.code,
        "category": contract.category,
        "retryable": contract.retryable,
        "recommended_action": contract.recommended_action,
        "recovery_hint": contract.recovery_hint,
    }


def _contract_recovery_actions(
    contract: dict[str, Any],
    *,
    workspace_root: Path,
    seen: set[str],
) -> list[dict[str, object]]:
    return append_contract_staging_recovery_actions(contract, workspace_root=workspace_root, seen=seen)


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
    value = contract.get("delivery_quality_contract")
    if isinstance(value, dict):
        return value
    return {}


def _required_fields_list(value: object) -> list[str]:
    return [text for item in value if (text := str(item).strip())] if isinstance(value, list) else []


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


def attach_tool_failure_recovery_actions(
    report: dict[str, Any],
    archive_tool_calls: list[Any],
    workspace_root: Path,
) -> dict[str, Any]:
    if report.get("ok") is True:
        return report
    progress = report.get("delivery_progress")
    if not isinstance(progress, dict):
        return report
    progress["recovery_actions"] = _merge_recovery_actions(
        _action_list(progress.get("recovery_actions")),
        tool_failure_recovery_actions(archive_tool_calls, workspace_root=workspace_root),
    )
    return report


def tool_failure_recovery_actions(
    records: list[Any],
    *,
    workspace_root: Path,
) -> list[dict[str, object]]:
    actions: list[dict[str, object]] = []
    seen: set[tuple[str, str, str]] = set()
    usable_records = [record for record in records if isinstance(record, dict)]
    for index, record in enumerate(usable_records):
        if record.get("ok") is not False:
            continue
        error_code = str(record.get("error_code") or "").strip()
        if not error_code or _failure_resolved(record, usable_records[index + 1 :], workspace_root):
            continue
        action = _action_from_failed_record(record, error_code, workspace_root)
        identity = (
            str(action.get("code") or ""),
            str(action.get("recommended_action") or ""),
            str(action.get("checkpoint_ref") or action.get("artifact_ref") or ""),
        )
        if identity in seen:
            continue
        seen.add(identity)
        actions.append(action)
    return actions


def _action_from_failed_record(
    record: dict[str, Any],
    error_code: str,
    workspace_root: Path,
) -> dict[str, object]:
    contract = error_contract(error_code)
    action: dict[str, object] = {
        "code": contract.code,
        "category": contract.category,
        "retryable": contract.retryable,
        "recommended_action": contract.recommended_action,
        "recovery_hint": contract.recovery_hint,
        "failed_tool": str(record.get("tool") or ""),
        "source_tool_call_id": str(record.get("scoped_call_id") or record.get("call_id") or record.get("id") or ""),
    }
    target_ref = _target_ref(record, workspace_root)
    if target_ref:
        action["artifact_ref"] = target_ref
    if _is_json_checkpoint_action(record, target_ref):
        action["checkpoint_ref"] = target_ref
        action["writer_tool"] = "write_file"
        action["write_tools"] = ["write_file"]
    return action


def _failure_resolved(
    record: dict[str, Any],
    later_records: list[dict[str, Any]],
    workspace_root: Path,
) -> bool:
    target_ref = _target_ref(record, workspace_root)
    if target_ref and _later_success_for_target(record, later_records, target_ref, workspace_root):
        return True
    target_path = _target_path(record, workspace_root)
    if target_path is None or not target_path.exists() or target_path.suffix.lower() != ".json":
        return False
    return json_checkpoint_status(target_path)["code"] == "OK"


def _later_success_for_target(
    failed_record: dict[str, Any],
    later_records: list[dict[str, Any]],
    target_ref: str,
    workspace_root: Path,
) -> bool:
    failed_tool = str(failed_record.get("tool") or "").strip()
    for record in later_records:
        if record.get("ok") is not True:
            continue
        if failed_tool and str(record.get("tool") or "").strip() != failed_tool:
            continue
        if _same_ref(_target_ref(record, workspace_root), target_ref):
            return True
    return False


def _is_json_checkpoint_action(record: dict[str, Any], target_ref: str) -> bool:
    tool_name = str(record.get("tool") or "").strip()
    return tool_name in _JSON_WRITER_TOOLS or target_ref.lower().endswith(".json")


def _target_ref(record: dict[str, Any], workspace_root: Path) -> str:
    for source in (record, _mapping(record.get("parameters")), _mapping(record.get("tool_result_envelope"))):
        if ref := _source_target_ref(source, workspace_root):
            return ref
    return ""


def _source_target_ref(source: dict[str, Any], workspace_root: Path) -> str:
    for key in _PATH_KEYS:
        if ref := _workspace_ref(source.get(key), workspace_root):
            return ref
    return ""


def _target_path(record: dict[str, Any], workspace_root: Path) -> Path | None:
    ref = _target_ref(record, workspace_root)
    if not ref:
        return None
    path = Path(ref).expanduser()
    candidate = path.resolve(strict=False) if path.is_absolute() else (workspace_root / path).resolve(strict=False)
    try:
        candidate.relative_to(workspace_root)
    except ValueError:
        return None
    return candidate


def _workspace_ref(value: object, workspace_root: Path) -> str:
    if isinstance(value, dict):
        return _workspace_ref_from_mapping(value, workspace_root)
    text = str(value or "").strip().replace("\\", "/")
    if not text:
        return ""
    path = Path(text).expanduser()
    if not path.is_absolute():
        return text.strip("/")
    resolved = path.resolve(strict=False)
    try:
        return str(resolved.relative_to(workspace_root)).replace("\\", "/")
    except ValueError:
        return ""


def _workspace_ref_from_mapping(value: dict[str, object], workspace_root: Path) -> str:
    for key in ("raw", "path", "artifact_ref", "resolved"):
        if ref := _workspace_ref(value.get(key), workspace_root):
            return ref
    return ""


def _same_ref(left: str, right: str) -> bool:
    left_text = str(left or "").strip().replace("\\", "/").strip("/")
    right_text = str(right or "").strip().replace("\\", "/").strip("/")
    return bool(left_text and right_text) and left_text == right_text


def _mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _merge_recovery_actions(
    current: list[dict[str, object]],
    added: list[dict[str, object]],
) -> list[dict[str, object]]:
    merged: list[dict[str, object]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for action in [*current, *added]:
        identity = (
            str(action.get("code") or ""),
            str(action.get("recommended_action") or ""),
            str(action.get("checkpoint_ref") or ""),
            str(action.get("artifact_ref") or ""),
        )
        if identity in seen:
            continue
        seen.add(identity)
        merged.append(dict(action))
    return merged


def _action_list(value: object) -> list[dict[str, object]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []
