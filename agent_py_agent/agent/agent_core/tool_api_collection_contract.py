# LLM: API collection contract guard checks tool params against staged delivery contracts before network calls.
# 模块用途: api_json_collection 执行前校验 required_columns/min_groups，避免失败后退化成手写事实。

from __future__ import annotations

from datetime import date
from typing import Any

from ..tools import ToolExecutionResult
from ._runtime_params import ToolLoopExecuteParams
from .tool_api_collection_claim_contract import claim_contract_findings
from .tool_api_collection_request_reserved import missing_request_reserved_metadata


def api_collection_contract_result(
    params: ToolLoopExecuteParams,
    payload: dict[str, object],
) -> ToolExecutionResult | None:
    tool = str(payload.get("tool") or "").strip()
    if tool == "write_structured_json":
        return _structured_source_checkpoint_result(params, payload)
    if tool != "api_json_collection":
        return None
    path = str(payload.get("path") or "").strip()
    if not path:
        return None
    contract = _matching_validation_contract(_delivery_contract(params), path)
    if not contract:
        return None
    findings = [
        *_missing_field_mappings(payload, contract),
        *_too_few_planned_groups(payload, contract),
        *missing_request_reserved_metadata(payload, contract),
    ]
    if not findings:
        return None
    return ToolExecutionResult(
        "api_json_collection",
        False,
        "TOOL_INVALID_ARGUMENTS: api_json_collection does not satisfy staged collection contract; "
        + "; ".join(findings),
        error_code="TOOL_INVALID_ARGUMENTS",
        recommended_action="repair_tool_arguments",
    )


# LLM: API collection calls may be incomplete when the active delivery contract already owns request facts.
# 函数用途: 从 collection_contract.api_request 合并缺失的工具参数；合同字段是机器事实，模型字段只是补充。
def normalize_api_collection_payload(
    params: ToolLoopExecuteParams,
    payload: dict[str, object],
) -> dict[str, object]:
    if str(payload.get("tool") or "").strip() != "api_json_collection":
        return payload
    path = str(payload.get("path") or "").strip()
    if not path:
        return payload
    contract = _matching_validation_contract(_delivery_contract(params), path)
    request = _declared_api_request(contract)
    if not request:
        return payload
    normalized = dict(payload)
    _merge_api_request_collections(normalized, request)
    _merge_api_request_metadata(normalized, request)
    _merge_api_request_fields(normalized, request)
    return normalized


def _structured_source_checkpoint_result(
    params: ToolLoopExecuteParams,
    payload: dict[str, object],
) -> ToolExecutionResult | None:
    path = str(payload.get("path") or "").strip()
    contract = _matching_validation_contract(_delivery_contract(params), path)
    if not contract or not _requires_source_evidence(contract) or bool(payload.get("merge_existing")):
        return None
    findings = _source_checkpoint_findings(payload, contract, params)
    if not findings:
        return None
    return ToolExecutionResult(
        "write_structured_json",
        False,
        "TOOL_INVALID_ARGUMENTS: source checkpoint does not satisfy staged source evidence contract; "
        + "; ".join(findings),
        error_code="TOOL_INVALID_ARGUMENTS",
        recommended_action="write_auditable_source_checkpoint",
    )


def _matching_validation_contract(contract: dict[str, Any], path: str) -> dict[str, object]:
    for artifact in contract.get("artifacts", []) if isinstance(contract.get("artifacts"), list) else []:
        validation = artifact.get("validation_contract") if isinstance(artifact, dict) else None
        if isinstance(validation, dict) and _matches_source_checkpoint(path, validation):
            return validation
    return {}


def _declared_api_request(contract: dict[str, object]) -> dict[str, object]:
    collection = contract.get("collection_contract")
    api_request = collection.get("api_request") if isinstance(collection, dict) else None
    return dict(api_request) if isinstance(api_request, dict) else {}


def _merge_api_request_collections(payload: dict[str, object], request: dict[str, object]) -> None:
    for key in ("request_ranges", "requests", "source_artifacts"):
        declared_items = request.get(key)
        payload_items = payload.get(key)
        if isinstance(payload_items, list) and payload_items:
            payload[key] = _merge_request_items(payload_items, declared_items)
        elif isinstance(declared_items, list) and declared_items:
            payload[key] = [dict(item) for item in declared_items if isinstance(item, dict)]


def _merge_request_items(payload_items: list[object], declared_items: object) -> list[object]:
    declared = [item for item in declared_items if isinstance(item, dict)] if isinstance(declared_items, list) else []
    if not declared:
        return list(payload_items)
    result: list[object] = []
    for index, item in enumerate(payload_items):
        if not isinstance(item, dict):
            result.append(item)
            continue
        template = declared[index] if index < len(declared) else declared[-1]
        result.append(_merge_request_item(item, template))
    return result


def _merge_request_item(item: dict[str, object], declared: dict[str, object]) -> dict[str, object]:
    merged = {key: value for key, value in declared.items() if key != "reserved"}
    merged.update(item)
    declared_reserved = declared.get("reserved")
    item_reserved = item.get("reserved")
    if isinstance(declared_reserved, dict) or isinstance(item_reserved, dict):
        merged["reserved"] = _deep_merge_dicts(
            item_reserved if isinstance(item_reserved, dict) else {},
            declared_reserved if isinstance(declared_reserved, dict) else {},
        )
    return merged


def _merge_api_request_metadata(payload: dict[str, object], request: dict[str, object]) -> None:
    for key in ("item_path", "limit_per_request", "request_delay_seconds", "url_template"):
        if key in request and payload.get(key) in (None, "", []):
            payload[key] = request[key]
    if isinstance(request.get("completion_evidence"), dict):
        payload["completion_evidence"] = _deep_merge_dicts(
            payload.get("completion_evidence") if isinstance(payload.get("completion_evidence"), dict) else {},
            request["completion_evidence"],
        )
    if isinstance(request.get("evidence_fields"), list) and request["evidence_fields"]:
        payload["evidence_fields"] = list(request["evidence_fields"])


def _merge_api_request_fields(payload: dict[str, object], request: dict[str, object]) -> None:
    request_fields = request.get("fields")
    if not isinstance(request_fields, dict) or not request_fields:
        return
    payload_fields = payload.get("fields")
    payload["fields"] = {
        **(payload_fields if isinstance(payload_fields, dict) else {}),
        **request_fields,
    }
    if payload.get("columns") in (None, "", []):
        payload["columns"] = list(payload["fields"])


def _deep_merge_dicts(base: dict[str, object], authoritative: dict[str, object]) -> dict[str, object]:
    merged = dict(base)
    for key, value in authoritative.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge_dicts(merged[key], value)
        else:
            merged[key] = value
    return merged


# LLM: Source checkpoints may be declared by collection contracts or as JSON staging refs.
# 函数用途: 让 source_index.json 这类前置证据文件和 source_data.json 用同一个来源绑定门。
def _matches_source_checkpoint(path: str, validation: dict[str, object]) -> bool:
    return any(_same_path_ref(path, ref) for ref in _source_checkpoint_refs(validation))


def _source_checkpoint_refs(validation: dict[str, object]) -> list[str]:
    refs: list[str] = []
    collection = validation.get("collection_contract")
    if isinstance(collection, dict):
        refs.append(str(collection.get("source_json_ref") or ""))
    staging = validation.get("staging_contract")
    if isinstance(staging, dict):
        refs.append(str(staging.get("source_json_ref") or ""))
        checkpoint_refs = staging.get("checkpoint_refs")
        if isinstance(checkpoint_refs, list):
            refs.extend(str(item or "") for item in checkpoint_refs if str(item or "").lower().endswith(".json"))
    return [ref for ref in refs if ref.strip()]


def _missing_field_mappings(payload: dict[str, object], contract: dict[str, object]) -> list[str]:
    required = _required_columns(contract)
    fields = payload.get("fields")
    field_names = set(fields) if isinstance(fields, dict) else set()
    missing = [field for field in required if field not in field_names]
    return [
        "missing field mappings for columns: "
        f"{', '.join(missing[:8])}; use path/template/default/default_template, including template/default_template for derived columns"
    ] if missing else []


def _required_columns(contract: dict[str, object]) -> list[str]:
    values = _string_list(contract.get("required_columns"))
    collection = contract.get("collection_contract")
    if isinstance(collection, dict):
        values.extend(_string_list(collection.get("required_item_fields")))
    return sorted(set(values))


def _requires_source_evidence(contract: dict[str, object]) -> bool:
    collection = contract.get("collection_contract")
    evidence = contract.get("evidence_contract")
    return isinstance(evidence, dict) or (
        isinstance(collection, dict)
        and (bool(collection.get("require_item_evidence")) or bool(collection.get("required_item_evidence_fields")))
    )


def _payload_has_evidence(payload: dict[str, object]) -> bool:
    return _payload_has_source_refs(payload) and _payload_has_claims(payload)


def _source_checkpoint_findings(payload: dict[str, object], contract: dict[str, object], params: ToolLoopExecuteParams) -> list[str]:
    findings: list[str] = []
    if not _payload_has_evidence(payload):
        findings.append(
            "source checkpoint requires auditable source_refs and claims; "
            "use api_json_collection for API data or include artifact-backed source_refs and row-scoped claims"
        )
    else:
        findings.extend(_source_binding_findings(payload, params))
        findings.extend(claim_contract_findings(payload, contract))
    if _requires_completion_evidence(contract) and not _payload_has_completion_evidence(payload):
        findings.append("completion_evidence is required for staged collection checkpoints")
    return findings


def _source_binding_findings(payload: dict[str, object], params: ToolLoopExecuteParams) -> list[str]:
    if not _payload_has_bound_source_refs(payload):
        return ["source_refs for write_structured_json must include artifact_ref or reserved.tool_call_id"]
    unrecorded = _unrecorded_source_tool_call_ids(payload, params)
    if not unrecorded:
        return []
    return ["source_refs reserved.tool_call_id must match previous archive_tool_calls: " + ", ".join(unrecorded[:8])]


def _payload_has_source_refs(payload: dict[str, object]) -> bool:
    return any(_has_structured_value(holder.get("source_refs")) for holder in _payload_holders(payload))


def _payload_has_claims(payload: dict[str, object]) -> bool:
    return any(_has_structured_value(holder.get("claims")) for holder in _payload_holders(payload))


def _payload_has_bound_source_refs(payload: dict[str, object]) -> bool:
    return any(_source_refs_have_bound_refs(holder.get("source_refs")) for holder in _payload_holders(payload))


def _unrecorded_source_tool_call_ids(payload: dict[str, object], params: ToolLoopExecuteParams) -> list[str]:
    required = _source_ref_tool_call_ids(payload)
    if not required:
        return []
    recorded = _recorded_tool_call_ids(getattr(params, "archive_tool_calls", []))
    return [item for item in required if item not in recorded]


def _source_ref_tool_call_ids(payload: dict[str, object]) -> list[str]:
    ids: list[str] = []
    for holder in _payload_holders(payload):
        refs = holder.get("source_refs")
        ids.extend(_tool_call_ids_from_source_refs(refs))
    return ids


def _tool_call_ids_from_source_refs(refs: object) -> list[str]:
    if not isinstance(refs, list):
        return []
    return [
        text
        for item in refs
        if isinstance(item, dict)
        for text in [_source_ref_tool_call_id(item)]
        if text
    ]


def _source_ref_tool_call_id(item: dict[str, object]) -> str:
    reserved = item.get("reserved")
    if not isinstance(reserved, dict):
        return ""
    return str(reserved.get("tool_call_id") or "").strip()


def _recorded_tool_call_ids(records: object) -> set[str]:
    result: set[str] = set()
    for item in records if isinstance(records, list) else []:
        result.update(_recorded_tool_call_ids_from_item(item))
    return result


def _recorded_tool_call_ids_from_item(item: object) -> set[str]:
    if not isinstance(item, dict) or item.get("ok") is not True:
        return set()
    values = {_text_field(item, key) for key in ("id", "call_id", "scoped_call_id", "operation_id")}
    protocol = item.get("tool_protocol_v2")
    if isinstance(protocol, dict):
        values.add(_text_field(protocol, "operation_id"))
    return {value for value in values if value}


def _text_field(item: dict[str, object], key: str) -> str:
    return str(item.get(key) or "").strip()


def _payload_has_completion_evidence(payload: dict[str, object]) -> bool:
    return any(isinstance(holder.get("completion_evidence"), dict) and bool(holder.get("completion_evidence")) for holder in _payload_holders(payload))


def _payload_holders(payload: dict[str, object]) -> list[dict[str, object]]:
    holders = [payload]
    data = payload.get("data")
    if isinstance(data, dict):
        holders.append(data)
    return holders


def _source_refs_have_bound_refs(value: object) -> bool:
    if not isinstance(value, list):
        return False
    return any(_source_ref_has_write_binding(item) for item in value if isinstance(item, dict))


def _source_ref_has_write_binding(item: dict[str, object]) -> bool:
    if str(item.get("artifact_ref") or "").strip():
        return True
    reserved = item.get("reserved")
    return isinstance(reserved, dict) and bool(str(reserved.get("tool_call_id") or "").strip())


def _requires_completion_evidence(contract: dict[str, object]) -> bool:
    collection = contract.get("collection_contract")
    return isinstance(collection, dict) and bool(collection.get("require_completion_evidence"))


def _has_structured_value(value: object) -> bool:
    return isinstance(value, (dict, list)) and bool(value)


def _too_few_planned_groups(payload: dict[str, object], contract: dict[str, object]) -> list[str]:
    required = _required_group_count(contract)
    planned = _planned_group_count(payload)
    if required and planned and planned < required:
        return [f"planned groups {planned} below required {required}"]
    return []


def _required_group_count(contract: dict[str, object]) -> int:
    collection = contract.get("collection_contract")
    values = [contract.get("required_sheets_min")]
    if isinstance(collection, dict):
        values.append(collection.get("min_groups"))
    return max([_positive_int(item) for item in values], default=0)


def _planned_group_count(payload: dict[str, object]) -> int:
    count = len(payload.get("requests")) if isinstance(payload.get("requests"), list) else 0
    ranges = payload.get("request_ranges")
    if isinstance(ranges, list):
        count += sum(_range_group_count(item) for item in ranges)
    return count


def _range_group_count(item: object) -> int:
    if not isinstance(item, dict):
        return 0
    try:
        start = date.fromisoformat(str(item.get("start_date") or ""))
        end = date.fromisoformat(str(item.get("end_date") or ""))
    except ValueError:
        return 0
    step = _positive_int(item.get("step_days")) or 7
    return max(0, ((end - start).days // step) + 1) if end >= start else 0


def _delivery_contract(params: ToolLoopExecuteParams) -> dict[str, Any]:
    if isinstance(params.delivery_contract, dict):
        return params.delivery_contract
    attrs = params.task_attributes if isinstance(params.task_attributes, dict) else {}
    return attrs.get("delivery_contract") if isinstance(attrs.get("delivery_contract"), dict) else {}


def _string_list(value: object) -> list[str]:
    return [text for item in value if (text := str(item).strip())] if isinstance(value, list) else []


def _positive_int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _same_path_ref(path: str, ref: str) -> bool:
    normalized_path = str(path or "").strip().replace("\\", "/").rstrip("/")
    normalized_ref = str(ref or "").strip().replace("\\", "/").strip("/")
    return bool(normalized_ref) and (
        normalized_path == normalized_ref
        or normalized_path.endswith(f"/{normalized_ref}")
        or normalized_ref.endswith(f"/{normalized_path}")
    )


__all__ = ["api_collection_contract_result", "normalize_api_collection_payload"]
