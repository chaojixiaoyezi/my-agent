# LLM: API JSON collection builder fetches declared sources and emits sourced table checkpoints.
# 模块用途: 将结构化 requests/fields 映射为 sheets/source_refs/claims/completion_evidence。

from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .api_json_collection_http import fetch_json


# LLM: build_checkpoint fetches every group and assembles one auditable source-data object.
# 函数用途: 把 API 响应映射为 sheets/source_refs/claims/completion_evidence。
def build_checkpoint(request: dict[str, Any], *, timeout: int) -> dict[str, object]:
    sheets: list[dict[str, object]] = []
    source_refs: list[dict[str, object]] = []
    claims: list[dict[str, object]] = []
    request_specs = list(request["requests"])
    for group_index, spec in enumerate(request_specs):
        response = _load_json(spec, timeout=timeout)
        source_refs.append(_source_ref(spec, response))
        context = {"claims": claims, "group_index": group_index, "request": request, "spec": spec}
        sheets.append({"name": spec["name"], "columns": request["columns"], "rows": _rows_from_response(response["json"], context)})
        _sleep_between_requests(request, group_index, len(request_specs))
    return {
        "claims": claims,
        "completion_evidence": request["completion_evidence"],
        "sheets": sheets,
        "source_refs": source_refs,
    }


def validate_checkpoint(value: dict[str, object], *, request: dict[str, Any] | None = None) -> None:
    sheets = value.get("sheets")
    if not isinstance(sheets, list) or not any(_sheet_has_rows(sheet) for sheet in sheets):
        raise ValueError("API_JSON_NO_ROWS: API collection produced no rows")
    if isinstance(request, dict):
        _validate_evidence_fields(sheets, request)


def _validate_evidence_fields(sheets: object, request: dict[str, Any]) -> None:
    fields = [str(item) for item in request.get("evidence_fields", []) if str(item).strip()]
    for row, sheet_index, row_index in _iter_sheet_rows(sheets):
        _validate_row_evidence_fields(row, fields, sheet_index, row_index)


def _iter_sheet_rows(sheets: object):
    for sheet_index, sheet in enumerate(sheets if isinstance(sheets, list) else []):
        yield from _iter_one_sheet_rows(sheet, sheet_index)


def _iter_one_sheet_rows(sheet: object, sheet_index: int):
    rows = sheet.get("rows", []) if isinstance(sheet, dict) and isinstance(sheet.get("rows"), list) else []
    for row_index, row in enumerate(rows):
        if isinstance(row, dict):
            yield row, sheet_index, row_index


def _validate_row_evidence_fields(
    row: dict[str, object],
    fields: list[str],
    sheet_index: int,
    row_index: int,
) -> None:
    missing = [field for field in fields if not _has_value(row.get(field))]
    if missing:
        raise ValueError(
            "API_JSON_EMPTY_EVIDENCE_FIELD: "
            f"field {missing[0]} is empty at sheet_index={sheet_index} row_index={row_index}; "
            "add default/default_template or map to a non-empty source path"
        )


def _sleep_between_requests(request: dict[str, Any], group_index: int, request_count: int) -> None:
    delay = float(request.get("request_delay_seconds") or 0.0)
    if delay > 0 and group_index < request_count - 1:
        time.sleep(delay)


# LLM: _load_json selects remote fetch or artifact replay from explicit machine fields.
# 函数用途: 根据结构化 spec 读取 JSON 来源；不会从普通自然语言输出里猜路径或来源。
def _load_json(spec: dict[str, Any], *, timeout: int) -> dict[str, object]:
    if str(spec.get("artifact_path") or "").strip():
        return _load_artifact_json(str(spec["artifact_path"]))
    return fetch_json(spec["url"], timeout=timeout)


# LLM: _load_artifact_json reuses archived tool-output JSON without replaying side effects.
# 函数用途: 读取已归档 JSON artifact，支持 fetch_url 的 content header/body 包装。
def _load_artifact_json(path: str) -> dict[str, object]:
    artifact_path = Path(path)
    try:
        raw = artifact_path.read_bytes()
        value = json.loads(raw.decode("utf-8", "replace"))
    except OSError as exc:
        raise ValueError(f"API_JSON_ARTIFACT_MISSING: {artifact_path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"API_JSON_ARTIFACT_INVALID: {exc.msg}") from exc
    payload = _artifact_payload(value)
    if not isinstance(payload, (dict, list)):
        raise ValueError("API_JSON_ARTIFACT_INVALID: artifact JSON payload must be object or array")
    return {"json": payload, "status": 0, "sha256": hashlib.sha256(raw).hexdigest()}


def _artifact_payload(value: object) -> object:
    if isinstance(value, dict) and isinstance(value.get("content"), str):
        return _json_from_artifact_content(value["content"])
    return value


def _json_from_artifact_content(text: str) -> object:
    candidates = [text]
    if "\n\n" in text:
        candidates.insert(0, text.split("\n\n", 1)[1])
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    raise ValueError("API_JSON_ARTIFACT_INVALID: artifact content is not valid JSON")


# LLM: _rows_from_response extracts rows from the configured item_path.
# 函数用途: 从响应对象中提取条目，并为每行生成字段和行级来源绑定。
def _rows_from_response(response: object, context: dict[str, Any]) -> list[dict[str, object]]:
    spec = context["spec"]
    request = context["request"]
    item_path = str(spec.get("item_path") or request["item_path"])
    items = _items_at_path(response, item_path)
    rows: list[dict[str, object]] = []
    for item_index, item in enumerate(items[: int(spec.get("limit") or request["limit_per_request"])]):
        if not isinstance(item, dict):
            continue
        row = _row_from_item(item, request["fields"], request["evidence_fields"], str(spec["source_id"]))
        rows.append(row)
        _append_claims(row, context, item_index)
    return rows


def _row_from_item(
    item: dict[str, object],
    fields: dict[str, object],
    evidence_fields: list[str],
    source_id: str,
) -> dict[str, object]:
    row = {field: _field_value(rule, item) for field, rule in fields.items()}
    row["field_source_ids"] = {field: [source_id] for field in evidence_fields if _has_value(row.get(field))}
    return row


def _append_claims(row: dict[str, object], context: dict[str, Any], item_index: int) -> None:
    for field in context["request"]["evidence_fields"]:
        if _has_value(row.get(field)):
            context["claims"].append(_claim(field, row[field], context, item_index))


def _claim(
    field: str,
    value: object,
    context: dict[str, Any],
    item_index: int,
) -> dict[str, object]:
    spec = context["spec"]
    source_id = str(spec["source_id"])
    return {
        "claim_id": f"{source_id}:{item_index}:{field}",
        "field": field,
        "source_ids": [source_id],
        "value": value,
        "verification_status": "VERIFIED",
        "value_type": "exact",
        "reserved": {"group_index": context["group_index"], "group_name": spec["name"], "item_index": item_index},
    }


def _field_value(rule: object, item: dict[str, object]) -> object:
    if isinstance(rule, str):
        return _lookup_path(item, rule)
    if not isinstance(rule, dict):
        return rule
    return _field_value_from_rule(rule, item)


def _field_value_from_rule(rule: dict[str, object], item: dict[str, object]) -> object:
    if "value" in rule:
        return rule.get("value")
    if "template" in rule and "path" not in rule:
        return _format_template(str(rule.get("template") or ""), item)
    if "path" in rule:
        value = _lookup_path(item, str(rule.get("path") or ""))
        return value if _has_value(value) else _fallback_value(rule, item)
    raise ValueError("TOOL_INVALID_ARGUMENTS: fields rules support only path/value/template")


def _fallback_value(rule: dict[str, object], item: dict[str, object]) -> object:
    if "default" in rule:
        return rule.get("default")
    if "default_template" in rule:
        return _format_template(str(rule.get("default_template") or ""), item)
    return ""


def _format_template(template: str, item: dict[str, object]) -> str:
    class _Safe(dict[str, object]):
        def __missing__(self, key: str) -> str:
            return ""

    values = _Safe({key: _string_value(value) for key, value in item.items()})
    return template.format_map(values)


def _source_ref(spec: dict[str, Any], response: dict[str, object]) -> dict[str, object]:
    if str(spec.get("artifact_ref") or "").strip():
        return {
            "artifact_ref": str(spec["artifact_ref"]),
            "content_sha256": str(response.get("sha256") or ""),
            "retrieved_at": _now(),
            "source_id": str(spec["source_id"]),
            "source_type": "artifact_json",
            "status": "AVAILABLE",
            "uri": str(spec["artifact_ref"]),
            "reserved": {"group_name": spec["name"]},
        }
    return {
        "content_sha256": str(response.get("sha256") or ""),
        "retrieved_at": _now(),
        "source_id": str(spec["source_id"]),
        "source_type": "api_json",
        "status": "AVAILABLE",
        "uri": str(spec["url"]),
        "reserved": {"http_status": response.get("status"), "group_name": spec["name"]},
    }


def _items_at_path(value: object, path: str) -> list[object]:
    if isinstance(value, list) and path in {"", "items", "$"}:
        return list(value)
    current = value
    for part in [item for item in path.split(".") if item and item != "$"]:
        if not isinstance(current, dict):
            return []
        current = current.get(part)
    return list(current) if isinstance(current, list) else []


def _lookup_path(value: object, path: str) -> object:
    current = value
    for part in [item for item in path.split(".") if item]:
        if not isinstance(current, dict):
            return ""
        current = current.get(part)
    return current if current is not None else ""


def _sheet_has_rows(value: object) -> bool:
    return isinstance(value, dict) and isinstance(value.get("rows"), list) and bool(value["rows"])


def _has_value(value: object) -> bool:
    return bool(value.strip()) if isinstance(value, str) else value is not None


def _string_value(value: object) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return "" if value is None else str(value)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = ["build_checkpoint", "validate_checkpoint"]
