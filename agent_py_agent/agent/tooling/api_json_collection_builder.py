# LLM: API JSON collection builder fetches declared sources and emits sourced table checkpoints.
# 模块用途: 将结构化 requests/fields 映射为 sheets/source_refs/claims/completion_evidence。

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Iterable
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from ..contracts.gates import NetworkResolver
from .api_json_collection_http import fetch_json

_ISO_DATE_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})[-_/](0[1-9]|1[0-2])[-_/]([0-3]\d)(?!\d)")
_YEAR_MONTH_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})[-_/](0[1-9]|1[0-2])(?![-_/]\d)")
_ARXIV_NEW_ID_RE = re.compile(r"/(?:abs|pdf)/([0-9]{2})(0[1-9]|1[0-2])\.\d{4,6}(?:v\d+)?(?:$|[?#/])")
_YEAR_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")


# LLM: build_checkpoint fetches every group and assembles one auditable source-data object.
# 函数用途: 把 API 响应映射为 sheets/source_refs/claims/completion_evidence。
def build_checkpoint(
    request: dict[str, Any],
    *,
    timeout: int,
    resolver: NetworkResolver | None = None,
    allowed_private_hosts: Iterable[str] = (),
    allow_private_resolution: bool | None = None,
) -> dict[str, object]:
    sheets: list[dict[str, object]] = []
    source_refs: list[dict[str, object]] = []
    claims: list[dict[str, object]] = []
    request_specs = list(request["requests"])
    for group_index, spec in enumerate(request_specs):
        response = _load_json(
            spec,
            timeout=timeout,
            resolver=resolver,
            allowed_private_hosts=allowed_private_hosts,
            allow_private_resolution=allow_private_resolution,
        )
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
            "add default/default_template/date_from_url or map to a non-empty source path"
        )


def _sleep_between_requests(request: dict[str, Any], group_index: int, request_count: int) -> None:
    delay = float(request.get("request_delay_seconds") or 0.0)
    if delay > 0 and group_index < request_count - 1:
        time.sleep(delay)


# LLM: _load_json selects remote fetch or artifact replay from explicit machine fields.
# 函数用途: 根据结构化 spec 读取 JSON 来源；不会从普通自然语言输出里猜路径或来源。
def _load_json(
    spec: dict[str, Any],
    *,
    timeout: int,
    resolver: NetworkResolver | None = None,
    allowed_private_hosts: Iterable[str] = (),
    allow_private_resolution: bool | None = None,
) -> dict[str, object]:
    if str(spec.get("artifact_path") or "").strip():
        return _load_artifact_json(str(spec["artifact_path"]))
    return fetch_json(
        spec["url"],
        timeout=timeout,
        resolver=resolver,
        allowed_private_hosts=allowed_private_hosts,
        allow_private_resolution=allow_private_resolution,
    )


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
        if not _row_within_date_bounds(row, request.get("item_date_bounds")):
            continue
        if request.get("drop_incomplete_items") is True and not _row_has_evidence_fields(row, request["evidence_fields"]):
            continue
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


def _row_has_evidence_fields(row: dict[str, object], evidence_fields: list[str]) -> bool:
    return all(_has_value(row.get(field)) for field in evidence_fields)


def _row_within_date_bounds(row: dict[str, object], bounds: object) -> bool:
    if not isinstance(bounds, dict) or not bounds:
        return True
    field = str(bounds.get("field") or "date").strip()
    actual = _parse_iso_date(row.get(field))
    if actual is None:
        return False
    min_date = _parse_iso_date(bounds.get("min"))
    if min_date is not None and actual < min_date:
        return False
    max_date = _parse_iso_date(bounds.get("max"))
    return not (max_date is not None and actual > max_date)


def _parse_iso_date(value: object) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


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
        return _field_output_value(rule.get("value"))
    if "template" in rule and "path" not in rule and "paths" not in rule:
        return _field_output_value(_format_template(str(rule.get("template") or ""), item))
    if "path" in rule or "paths" in rule:
        value = _first_path_value(rule, item)
        if _has_value(value):
            return value
        derived = _derived_field_value(rule, item)
        return derived if _has_value(derived) else _fallback_value(rule, item)
    derived = _derived_field_value(rule, item)
    if _has_value(derived):
        return derived
    raise ValueError("TOOL_INVALID_ARGUMENTS: fields rules support only path/paths/value/template/date_from_url")


def _first_path_value(rule: dict[str, object], item: dict[str, object]) -> object:
    paths = []
    if "path" in rule:
        paths.append(str(rule.get("path") or ""))
    raw_paths = rule.get("paths")
    if isinstance(raw_paths, list):
        paths.extend(str(path or "") for path in raw_paths)
    for path in paths:
        value = _lookup_path(item, path)
        if _has_value(value):
            return value
    return ""


def _derived_field_value(rule: dict[str, object], item: dict[str, object]) -> object:
    if rule.get("date_from_url") is True:
        return _date_from_url(_first_text_value(item, ("url", "uri", "link", "html_url")))
    return ""


def _fallback_value(rule: dict[str, object], item: dict[str, object]) -> object:
    if "default" in rule:
        return _field_output_value(rule.get("default"))
    if "default_template" in rule:
        return _field_output_value(_format_template(str(rule.get("default_template") or ""), item))
    return ""


def _field_output_value(value: object) -> object:
    if isinstance(value, str) and _is_placeholder_value(value):
        return ""
    return value


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
            "reserved": _source_reserved(spec, {"group_name": spec["name"]}),
        }
    return {
        "content_sha256": str(response.get("sha256") or ""),
        "retrieved_at": _now(),
        "source_id": str(spec["source_id"]),
        "source_type": "api_json",
        "status": "AVAILABLE",
        "uri": str(spec["url"]),
        "reserved": _source_reserved(spec, {"group_name": spec["name"], "http_status": response.get("status")}),
    }


def _source_reserved(spec: dict[str, Any], extra: dict[str, object]) -> dict[str, object]:
    reserved = dict(spec["reserved"]) if isinstance(spec.get("reserved"), dict) else {}
    reserved.update(extra)
    return reserved


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
    return bool(value.strip()) and not _is_placeholder_value(value) if isinstance(value, str) else value is not None


def _is_placeholder_value(value: str) -> bool:
    text = value.strip()
    upper = text.upper()
    return (
        upper.startswith("__FILL")
        or upper in {"TODO", "TBD", "N/A", "NA", "UNKNOWN", "NONE", "NULL"}
        or text in {"...", "待补充", "待定", "未知", "暂无", "无"}
    )


def _first_text_value(item: dict[str, object], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _date_from_url(url: str) -> str:
    if not url:
        return ""
    if match := _ISO_DATE_RE.search(url):
        return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
    if match := _YEAR_MONTH_RE.search(url):
        return f"{match.group(1)}-{match.group(2)}-01"
    if match := _ARXIV_NEW_ID_RE.search(url):
        return f"20{match.group(1)}-{match.group(2)}-01"
    if match := _YEAR_RE.search(url):
        return f"{match.group(1)}-01-01"
    return ""


def _string_value(value: object) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return "" if value is None else str(value)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = ["build_checkpoint", "validate_checkpoint"]
