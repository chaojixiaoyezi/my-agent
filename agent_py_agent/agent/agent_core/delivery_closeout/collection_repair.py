from __future__ import annotations

import json
from typing import Any

from ...contracts.recovery_actions import RecoveryAction
from .artifact_repair import _artifact_validation_contract, artifact_findings
from .recovery_models import RecoveryActionLedger

_MAX_FINDING_VALUES_PER_ACTION = 64


def append_collection_value_repair_actions(
    report: dict[str, Any],
    contract: dict[str, Any],
    ledger: RecoveryActionLedger,
) -> None:
    for action in _collection_value_repair_actions(report, contract):
        action_key = f"{action['code']}:{action['checkpoint_ref']}"
        if action_key in ledger.seen:
            continue
        ledger.seen.add(action_key)
        ledger.actions.append(action)


def _collection_value_repair_actions(
    report: dict[str, Any],
    contract: dict[str, Any],
):
    for item in report.get("artifacts", []):
        yield from _collection_value_repair_actions_for_item(item, contract)


def _collection_value_repair_actions_for_item(item: object, contract: dict[str, Any]):
    if not isinstance(item, dict) or item.get("ok"):
        return
    collection_contract = _collection_contract_for_item(item, contract)
    if not collection_contract:
        return
    for checkpoint_ref, updates in _collection_updates_by_ref(artifact_findings(item), collection_contract).items():
        yield collection_value_repair_action(checkpoint_ref, updates, collection_contract)
    for checkpoint_ref in _collection_placeholder_refs(artifact_findings(item), collection_contract):
        yield collection_placeholder_repair_action(checkpoint_ref, collection_contract)
    for code, checkpoint_ref in _collection_count_refs(artifact_findings(item), collection_contract):
        yield collection_count_repair_action(code, checkpoint_ref, collection_contract)
    for code, checkpoint_ref in _collection_date_refs(artifact_findings(item), collection_contract):
        yield collection_date_repair_action(code, checkpoint_ref, collection_contract)
    if action := collection_mapping_repair_action(artifact_findings(item), collection_contract):
        yield action


def collection_value_repair_action(
    checkpoint_ref: str,
    updates: list[dict[str, object]],
    collection_contract: dict[str, object],
) -> dict[str, object]:
    action: dict[str, object] = {
        "code": "COLLECTION_ITEM_VALUE_MISMATCH",
        "category": "artifact",
        "retryable": True,
        "recommended_action": RecoveryAction.REPAIR_ARTIFACT_AGAINST_FINDINGS.value,
        "checkpoint_ref": checkpoint_ref,
        "writer_tool": "write_file",
        "write_tools": ["write_file"],
        "items_path": str(collection_contract.get("items_path") or "rows"),
        "collection_item_updates": updates,
        "recovery_hint": "集合 JSON 的行级机器字段不符合合同；按 collection_item_updates 更新 checkpoint 后重新验收。",
    }
    groups_path = str(collection_contract.get("groups_path") or "").strip()
    if groups_path:
        action["groups_path"] = groups_path
    return action


def collection_placeholder_repair_action(
    checkpoint_ref: str,
    collection_contract: dict[str, object],
) -> dict[str, object]:
    action: dict[str, object] = {
        "code": "COLLECTION_ITEM_PLACEHOLDER_VALUE",
        "category": "artifact",
        "retryable": True,
        "recommended_action": RecoveryAction.REPAIR_STRUCTURED_CHECKPOINT_JSON.value,
        "checkpoint_ref": checkpoint_ref,
        "writer_tool": "write_file",
        "write_tools": ["write_file"],
        "items_path": str(collection_contract.get("items_path") or "rows"),
        "collection_contract": dict(collection_contract),
        "required_columns": collection_required_columns(collection_contract),
        "recovery_hint": "集合 JSON 的必填字段仍含模板占位值；重新采集或重写来源 checkpoint，不能保留 __FILL__/TODO 这类占位符。",
    }
    groups_path = str(collection_contract.get("groups_path") or "").strip()
    if groups_path:
        action["groups_path"] = groups_path
    return action


def collection_count_repair_action(
    code: str,
    checkpoint_ref: str,
    collection_contract: dict[str, object],
) -> dict[str, object]:
    action: dict[str, object] = {
        "code": code,
        "category": "artifact",
        "retryable": True,
        "recommended_action": RecoveryAction.REPAIR_STRUCTURED_CHECKPOINT_JSON.value,
        "checkpoint_ref": checkpoint_ref,
        "writer_tool": "write_file",
        "write_tools": ["write_file"],
        "items_path": str(collection_contract.get("items_path") or "rows"),
        "collection_contract": dict(collection_contract),
        "required_columns": collection_required_columns(collection_contract),
        "recovery_hint": "集合 JSON 的数量没有达到合同要求；继续采集或补齐来源 checkpoint，保持 source_refs/claims/completion_evidence 可审计。",
    }
    groups_path = str(collection_contract.get("groups_path") or "").strip()
    if groups_path:
        action["groups_path"] = groups_path
    return action


def collection_date_repair_action(
    code: str,
    checkpoint_ref: str,
    collection_contract: dict[str, object],
) -> dict[str, object]:
    action = collection_count_repair_action(code, checkpoint_ref, collection_contract)
    action["recovery_hint"] = "集合 JSON 的日期字段不满足时间窗口合同；重新采集或过滤来源 checkpoint，保留满足 item_date_bounds 的可审计条目。"
    return action


def collection_required_columns(collection_contract: dict[str, object]) -> list[str]:
    fields = collection_contract.get("required_item_fields")
    return [str(item).strip() for item in fields if str(item).strip()] if isinstance(fields, list) else []


def collection_mapping_repair_action(
    findings: Any,
    collection_contract: dict[str, object],
) -> dict[str, object]:
    finding_values = [
        str(finding.get("value") or "").strip()
        for finding in findings
        if isinstance(finding, dict) and str(finding.get("code") or "") == "ARTIFACT_MAPPING_MISSING"
    ]
    finding_values = [value for value in finding_values if value]
    mapping = collection_contract.get("mapping")
    artifact_ref = str(mapping.get("artifact_ref") or "").strip() if isinstance(mapping, dict) else ""
    source_ref = str(collection_contract.get("source_json_ref") or "").strip()
    if not finding_values or not artifact_ref or not source_ref:
        return {}
    return {
        "category": "artifact",
        "code": "ARTIFACT_MAPPING_MISSING",
        "recommended_action": RecoveryAction.REPAIR_ARTIFACT_AGAINST_FINDINGS.value,
        "artifact_path": artifact_ref,
        "checkpoint_ref": source_ref,
        "finding_codes": ["ARTIFACT_MAPPING_MISSING"],
        "finding_values": finding_values[:_MAX_FINDING_VALUES_PER_ACTION],
        "repair_targets": [artifact_ref],
        "retryable": True,
        "source_ref": source_ref,
        "write_tools": ["write_file", "apply_patch", "write_file"],
        "recovery_hint": "集合映射产物缺少 source_json_ref 中的条目；按结构化来源生成或修复目标文档后重新验收。",
    }


def _collection_contract_for_item(item: dict[str, Any], contract: dict[str, Any]) -> dict[str, object]:
    validation_contract = _artifact_validation_contract(item, contract)
    collection_contract = validation_contract.get("collection_contract")
    return dict(collection_contract) if isinstance(collection_contract, dict) else {}


def _collection_updates_by_ref(
    findings: Any,
    collection_contract: dict[str, object],
) -> dict[str, list[dict[str, object]]]:
    updates_by_ref: dict[str, list[dict[str, object]]] = {}
    seen: set[tuple[str, int, str]] = set()
    for finding in findings:
        update = _collection_update_from_finding(finding, collection_contract)
        if not update:
            continue
        checkpoint_ref = str(update.pop("checkpoint_ref"))
        key = (checkpoint_ref, int(update["item_index"]), str(update["field_path"]))
        if key in seen:
            continue
        seen.add(key)
        updates_by_ref.setdefault(checkpoint_ref, []).append(update)
    return updates_by_ref


def _collection_placeholder_refs(
    findings: Any,
    collection_contract: dict[str, object],
) -> list[str]:
    refs: list[str] = []
    declared_ref = str(collection_contract.get("source_json_ref") or "").strip()
    for finding in findings:
        if str(finding.get("code") or "") != "COLLECTION_ITEM_PLACEHOLDER_VALUE":
            continue
        location = _parse_collection_location(str(finding.get("location") or ""))
        if not location:
            continue
        checkpoint_ref = str(location["checkpoint_ref"])
        if declared_ref and _normalized_ref(checkpoint_ref) != _normalized_ref(declared_ref):
            continue
        if checkpoint_ref not in refs:
            refs.append(checkpoint_ref)
    return refs


def _collection_count_refs(
    findings: Any,
    collection_contract: dict[str, object],
) -> list[tuple[str, str]]:
    refs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    declared_ref = str(collection_contract.get("source_json_ref") or "").strip()
    for finding in findings:
        code = str(finding.get("code") or "")
        if code not in {"COLLECTION_TOO_FEW_ITEMS", "COLLECTION_TOO_FEW_GROUPS", "COLLECTION_GROUP_TOO_FEW_ITEMS"}:
            continue
        checkpoint_ref = _collection_finding_checkpoint_ref(finding, declared_ref)
        if not checkpoint_ref:
            continue
        key = (code, checkpoint_ref)
        if key in seen:
            continue
        seen.add(key)
        refs.append(key)
    return refs


def _collection_date_refs(
    findings: Any,
    collection_contract: dict[str, object],
) -> list[tuple[str, str]]:
    refs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    declared_ref = str(collection_contract.get("source_json_ref") or "").strip()
    for finding in findings:
        code = str(finding.get("code") or "")
        if code not in {"COLLECTION_ITEM_DATE_INVALID", "COLLECTION_ITEM_DATE_BEFORE_MIN", "COLLECTION_ITEM_DATE_AFTER_MAX"}:
            continue
        checkpoint_ref = _collection_finding_checkpoint_ref(finding, declared_ref)
        if not checkpoint_ref:
            continue
        key = (code, checkpoint_ref)
        if key in seen:
            continue
        seen.add(key)
        refs.append(key)
    return refs


def _collection_finding_checkpoint_ref(finding: dict[str, Any], declared_ref: str) -> str:
    location = _file_ref_head(str(finding.get("location") or ""))
    if declared_ref and location and _normalized_ref(location) != _normalized_ref(declared_ref):
        return ""
    return location or declared_ref


def _collection_update_from_finding(
    finding: dict[str, Any],
    collection_contract: dict[str, object],
) -> dict[str, object]:
    if str(finding.get("code") or "") != "COLLECTION_ITEM_VALUE_MISMATCH":
        return {}
    location = _parse_collection_location(str(finding.get("location") or ""))
    expected = _expected_value(finding.get("value"))
    if not location or expected is _NO_EXPECTED_VALUE:
        return {}
    checkpoint_ref = str(location["checkpoint_ref"])
    declared_ref = str(collection_contract.get("source_json_ref") or "").strip()
    if declared_ref and _normalized_ref(checkpoint_ref) != _normalized_ref(declared_ref):
        return {}
    return {
        "checkpoint_ref": checkpoint_ref,
        "item_index": location["item_index"],
        "field_path": location["field_path"],
        "value": expected,
    }


def _parse_collection_location(location: str) -> dict[str, object]:
    checkpoint_ref, marker, tail = location.partition("#")
    if not marker:
        return {}
    index_text, sep, field_path = tail.partition(":")
    if not sep:
        return {}
    try:
        item_index = int(index_text)
    except ValueError:
        return {}
    if item_index < 0 or not field_path.strip():
        return {}
    return {
        "checkpoint_ref": checkpoint_ref.strip().replace("\\", "/"),
        "item_index": item_index,
        "field_path": field_path.strip(),
    }


_NO_EXPECTED_VALUE = object()


def _expected_value(value: object) -> object:
    if not isinstance(value, str):
        return _NO_EXPECTED_VALUE
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return _NO_EXPECTED_VALUE
    if not isinstance(parsed, dict) or "expected" not in parsed:
        return _NO_EXPECTED_VALUE
    return parsed["expected"]


def _normalized_ref(value: str) -> str:
    return value.strip().replace("\\", "/")


def _file_ref_head(value: str) -> str:
    head = value.split(":", 1)[0].split("#", 1)[0].strip()
    return head.replace("\\", "/")


__all__ = ["append_collection_value_repair_actions"]
