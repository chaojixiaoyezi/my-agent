from __future__ import annotations

from typing import Any

from ...contracts.recovery_actions import RecoveryAction

_MAX_FINDING_VALUES_PER_ACTION = 64


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


__all__ = [
    "collection_count_repair_action",
    "collection_date_repair_action",
    "collection_mapping_repair_action",
    "collection_placeholder_repair_action",
    "collection_value_repair_action",
]
