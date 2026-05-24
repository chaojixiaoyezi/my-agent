# LLM: Staged checkpoint quality actions are shared by delivery recovery helpers.
# 模块用途: 将 JSON checkpoint 的结构质量问题转成通用恢复动作，避免 closeout recovery 文件继续膨胀。

from __future__ import annotations

import json
from typing import Any

from ..contracts.error_taxonomy import error_contract
from ..contracts.staged_checkpoint_acceptance import json_checkpoint_status
from .main_agent_delivery_closeout_recovery_models import CheckpointQualityActionRequest


# LLM: append_checkpoint_quality_action promotes staged JSON quality issues into structured recovery actions.
# 函数用途: 当 checkpoint 文件已存在但 JSON 为空、损坏或表格形状不合同时，生成可执行恢复动作。
def append_checkpoint_quality_action(request: CheckpointQualityActionRequest) -> None:
    status = json_checkpoint_status(
        request.checkpoint_path,
        required_columns=request.required_columns,
        required_sheets_min=request.required_sheets_min,
    )
    action_code = str(status.get("code") or "")
    if action_code == "OK" or action_code in request.ledger.seen:
        return
    request.ledger.seen.add(action_code)
    action = _checkpoint_quality_action(request, status, error_contract(action_code))
    request.ledger.actions.append(action)


# LLM: checkpoint_writer_fields exposes a generic writer tool for machine JSON checkpoints.
# 函数用途: 让 staged recovery 推荐 write_structured_json，而不是诱导模型手写大 JSON 字符串。
def checkpoint_writer_fields(ref_text: str) -> dict[str, object]:
    if ref_text.lower().endswith(".json"):
        return {"writer_tool": "write_structured_json", "write_tools": ["write_structured_json", "api_json_collection"]}
    return {}


# LLM: required_sheets_min normalizes optional workbook-like sheet count contracts.
# 函数用途: 从 validation_contract.required_sheets_min 读取最小 sheet 数，供阶段 JSON 和最终 workbook 同步校验。
def required_sheets_min(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


# LLM: _checkpoint_quality_action serializes checkpoint validation status into a recovery action.
# 函数用途: 将 parse_error、missing_columns、required_columns 等机器字段带入恢复动作。
def _checkpoint_quality_action(
    request: CheckpointQualityActionRequest,
    status: dict[str, str],
    contract: Any,
) -> dict[str, object]:
    action = {
        "code": contract.code,
        "category": contract.category,
        "retryable": contract.retryable,
        "recommended_action": contract.recommended_action,
        "recovery_hint": contract.recovery_hint,
        "checkpoint_ref": request.checkpoint_ref,
    }
    action.update(_checkpoint_optional_fields(request, status))
    return action


# LLM: _checkpoint_optional_fields keeps optional quality metadata compact and reusable.
# 函数用途: 只在字段真实存在时写入 required_columns、shape_hint、parse_error、missing_columns。
def _checkpoint_optional_fields(
    request: CheckpointQualityActionRequest,
    status: dict[str, str],
) -> dict[str, object]:
    fields: dict[str, object] = {}
    if request.required_columns:
        fields["required_columns"] = request.required_columns
    if request.required_sheets_min:
        fields["required_sheets_min"] = request.required_sheets_min
    if request.checkpoint_shape_hint:
        fields["checkpoint_shape_hint"] = request.checkpoint_shape_hint
    for key in ("parse_error", "missing_columns", "sheet_count"):
        if value := str(status.get(key) or ""):
            fields[key] = value
    fields.update(_checkpoint_writer_fields(request))
    return fields


# LLM: Collection source checkpoints must route to source collection first, not manual JSON patching.
# 函数用途: 当坏掉的 checkpoint 是 collection_contract 声明的 source_json_ref 时，优先暴露 api_json_collection 采集门。
def _checkpoint_writer_fields(request: CheckpointQualityActionRequest) -> dict[str, object]:
    if _shape_hint_has_generated_rows(request.checkpoint_shape_hint):
        return {
            "collection_contract": _compact_collection_contract(
                request.validation_contract.get("collection_contract", {})
                if isinstance(request.validation_contract, dict)
                else {}
            ),
            "writer_tool": "write_structured_json",
            "write_tools": ["write_structured_json", "api_json_collection"],
        }
    validation_contract = request.validation_contract if isinstance(request.validation_contract, dict) else {}
    collection = validation_contract.get("collection_contract")
    if isinstance(collection, dict) and _same_path_ref(request.checkpoint_ref, str(collection.get("source_json_ref") or "")):
        return {
            "collection_contract": _compact_collection_contract(collection),
            "writer_tool": "api_json_collection",
            "write_tools": ["api_json_collection", "write_structured_json"],
        }
    return checkpoint_writer_fields(request.checkpoint_ref)


def _compact_collection_contract(collection: dict[str, object]) -> dict[str, object]:
    keys = (
        "source_json_ref",
        "groups_path",
        "items_path",
        "min_groups",
        "min_items_per_group",
        "required_item_fields",
        "required_item_evidence_fields",
        "llm_generated_fields",
        "require_completion_evidence",
        "require_item_evidence",
        "completion_evidence_path",
        "api_request",
    )
    return {key: collection[key] for key in keys if key in collection}


def _same_path_ref(path: str, ref: str) -> bool:
    normalized_path = str(path or "").strip().replace("\\", "/").strip("/")
    normalized_ref = str(ref or "").strip().replace("\\", "/").strip("/")
    return bool(normalized_ref) and (
        normalized_path == normalized_ref
        or normalized_path.endswith(f"/{normalized_ref}")
        or normalized_ref.endswith(f"/{normalized_path}")
    )


def _shape_hint_has_generated_rows(value: str) -> bool:
    try:
        parsed = json.loads(str(value or ""))
    except json.JSONDecodeError:
        return False
    return isinstance(parsed, dict) and isinstance(parsed.get("generated_rows"), dict)


__all__ = ["append_checkpoint_quality_action", "checkpoint_writer_fields", "required_sheets_min"]
