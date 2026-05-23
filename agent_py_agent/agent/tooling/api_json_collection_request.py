# LLM: API JSON collection request parsing is separated from fetch/build logic.
# 模块用途: 校验并归一 api_json_collection 的结构化参数，不从自然语言推断事实。

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from ._filesystem_helpers import _text_param
from .web import _normalize_url

_MAX_REQUESTS = 64
_MAX_ITEMS_PER_REQUEST = 100
_BATCH_REQUEST_DELAY_FLOOR_SECONDS = 6.5
_BATCH_REQUEST_DELAY_FLOOR_COUNT = 10
_SHELL_PLACEHOLDER_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(:[^}]+)?\}")


@dataclass(frozen=True)
class _RangeWindow:
    index: int
    start: date
    end: date


# LLM: collection_request normalizes model parameters into a bounded machine spec.
# 函数用途: 校验 requests/fields/columns 等结构字段，供构建层直接执行。
def collection_request(params: dict[str, Any]) -> dict[str, Any]:
    requests = _all_requests(params)
    fields = params.get("fields")
    if not requests:
        raise ValueError("TOOL_INVALID_ARGUMENTS: requests must be a non-empty array")
    if len(requests) > _MAX_REQUESTS:
        raise ValueError(f"TOOL_INVALID_ARGUMENTS: requests max is {_MAX_REQUESTS}")
    if not isinstance(fields, dict) or not fields:
        raise ValueError("TOOL_INVALID_ARGUMENTS: fields must be a non-empty object")
    llm_generated_fields = _field_names(params.get("llm_generated_fields"))
    fields = _fields_without_llm_generated(fields, llm_generated_fields)
    columns = _columns(params.get("columns"), fields, llm_generated_fields)
    _validate_column_mappings(columns, fields, llm_generated_fields)
    return {
        "columns": columns,
        "completion_evidence": _completion_evidence(params.get("completion_evidence")),
        "drop_incomplete_items": _bool_param(params.get("drop_incomplete_items")),
        "evidence_fields": _evidence_fields(params.get("evidence_fields"), fields, llm_generated_fields),
        "fields": {str(key): value for key, value in fields.items() if str(key).strip()},
        "item_date_bounds": _item_date_bounds(params.get("item_date_bounds")),
        "item_path": _text_param(params.get("item_path", "items"), name="item_path", max_chars=160, strip=True),
        "limit_per_request": _bounded_limit(params.get("limit_per_request")),
        "llm_generated_fields": llm_generated_fields,
        "request_delay_seconds": _request_delay_seconds(params.get("request_delay_seconds"), len(requests)),
        "requests": [_request_spec(item, index) for index, item in enumerate(requests, start=1)],
    }


def _all_requests(params: dict[str, Any]) -> list[object]:
    explicit = params.get("requests")
    requests = list(explicit) if isinstance(explicit, list) else []
    source_artifacts = params.get("source_artifacts")
    if isinstance(source_artifacts, list):
        requests.extend(source_artifacts)
    ranges = params.get("request_ranges")
    if isinstance(ranges, list):
        for item in ranges:
            requests.extend(_range_requests(item, params))
    return requests


def _range_requests(item: object, defaults: dict[str, Any]) -> list[dict[str, object]]:
    if not isinstance(item, dict):
        raise ValueError("TOOL_INVALID_ARGUMENTS: request_ranges items must be objects")
    start = _parse_date(item.get("start_date"), "start_date")
    end = _parse_date(item.get("end_date"), "end_date")
    if end < start:
        raise ValueError("TOOL_INVALID_ARGUMENTS: end_date must be on or after start_date")
    step_days = _range_step_days(item.get("step_days"))
    return [
        _range_request(item, defaults, _RangeWindow(index, current, min(current + timedelta(days=step_days - 1), end)))
        for index, current in enumerate(_date_steps(start, end, step_days), start=1)
    ]


def _range_request(
    item: dict[str, object],
    defaults: dict[str, Any],
    window: _RangeWindow,
) -> dict[str, object]:
    url_template = _text_param(
        item.get("url_template") or defaults.get("url_template"),
        name="url_template",
        max_chars=1000,
        strip=True,
    )
    values = _range_template_values(window.index, window.start, window.end)
    return {
        "item_path": str(item.get("item_path") or "").strip(),
        "limit": item.get("limit"),
        "name": _render_range_template(item.get("name_template"), f"Group{window.index}", values),
        "reserved": _range_reserved(item, defaults, values),
        "source_id": _render_range_template(item.get("source_id_template"), f"src-{window.index:03d}", values),
        "url": _render_range_template(url_template, "", values),
    }


def _date_steps(start: date, end: date, step_days: int) -> list[date]:
    values: list[date] = []
    current = start
    while current <= end:
        values.append(current)
        current += timedelta(days=step_days)
    return values


def _range_template_values(index: int, start: date, end: date) -> dict[str, object]:
    week = f"{index:02d}"
    return {
        "YYYY": str(start.year),
        "end": end.isoformat(),
        "end_date": end.isoformat(),
        "index": index,
        "start": start.isoformat(),
        "start_date": start.isoformat(),
        "week": week,
        "ww": week,
        "year": str(start.year),
        "yyyy": str(start.year),
    }


def _range_reserved(
    item: dict[str, object],
    defaults: dict[str, Any],
    values: dict[str, object],
) -> dict[str, object]:
    reserved: dict[str, object] = {}
    default_reserved = defaults.get("request_reserved")
    if isinstance(default_reserved, dict):
        reserved.update(_render_reserved_templates(default_reserved, values))
    item_reserved = item.get("reserved")
    if item_reserved not in (None, "") and not isinstance(item_reserved, dict):
        raise ValueError("TOOL_INVALID_ARGUMENTS: request_ranges reserved must be an object")
    if isinstance(item_reserved, dict):
        reserved.update(_render_reserved_templates(item_reserved, values))
    return reserved


def _render_reserved_templates(value: object, values: dict[str, object]) -> object:
    if isinstance(value, str):
        return _render_range_template(value, "", values)
    if isinstance(value, list):
        return [_render_reserved_templates(item, values) for item in value]
    if isinstance(value, dict):
        return {
            str(key): _render_reserved_templates(item, values)
            for key, item in value.items()
            if str(key).strip()
        }
    return value


def _range_step_days(value: object) -> int:
    try:
        parsed = int(value) if value not in (None, "") else 7
    except (TypeError, ValueError):
        parsed = 7
    return max(1, min(366, parsed))


def _parse_date(value: object, name: str) -> date:
    text = _text_param(value, name=name, max_chars=32, strip=True)
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"TOOL_INVALID_ARGUMENTS: {name} must be YYYY-MM-DD") from exc


def _item_date_bounds(value: object) -> dict[str, str]:
    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        raise ValueError("TOOL_INVALID_ARGUMENTS: item_date_bounds must be an object")
    field = _text_param(value.get("field") or "date", name="item_date_bounds.field", max_chars=80, strip=True)
    bounds: dict[str, str] = {"field": field}
    if value.get("min") not in (None, ""):
        bounds["min"] = _parse_date(value.get("min"), "item_date_bounds.min").isoformat()
    if value.get("max") not in (None, ""):
        bounds["max"] = _parse_date(value.get("max"), "item_date_bounds.max").isoformat()
    if "min" not in bounds and "max" not in bounds:
        raise ValueError("TOOL_INVALID_ARGUMENTS: item_date_bounds requires min or max")
    return bounds


def _render_range_template(value: object, default: str, values: dict[str, object]) -> str:
    template = _normalize_template_placeholders(str(value or default))
    try:
        return template.format_map(values)
    except KeyError as exc:
        raise ValueError(f"TOOL_INVALID_ARGUMENTS: unknown range template placeholder {exc.args[0]}") from exc


def _normalize_template_placeholders(template: str) -> str:
    return _SHELL_PLACEHOLDER_RE.sub(r"{\1\2}", template)


# LLM: _request_spec validates one HTTP group request.
# 函数用途: 将 name/source_id/url/item_path/limit 归一成稳定结构。
def _request_spec(item: object, index: int) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise ValueError("TOOL_INVALID_ARGUMENTS: each request must be an object")
    artifact_ref = str(item.get("artifact_ref") or item.get("artifact_path") or "").strip()
    if artifact_ref:
        return {
            "artifact_ref": _text_param(artifact_ref, name="artifact_ref", max_chars=4096, strip=True),
            "item_path": str(item.get("item_path") or "").strip(),
            "limit": _bounded_limit(item.get("limit")),
            "name": _text_param(item.get("name", f"Group{index}"), name="name", max_chars=80, strip=True),
            "reserved": _request_reserved(item.get("reserved")),
            "source_id": _text_param(item.get("source_id", f"src-{index:03d}"), name="source_id", max_chars=120, strip=True),
        }
    return {
        "item_path": str(item.get("item_path") or "").strip(),
        "limit": _bounded_limit(item.get("limit")),
        "name": _text_param(item.get("name", f"Group{index}"), name="name", max_chars=80, strip=True),
        "reserved": _request_reserved(item.get("reserved")),
        "source_id": _text_param(item.get("source_id", f"src-{index:03d}"), name="source_id", max_chars=120, strip=True),
        "url": _normalize_url(item.get("url")),
    }


def _request_reserved(value: object) -> dict[str, object]:
    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        raise ValueError("TOOL_INVALID_ARGUMENTS: requests reserved must be an object")
    return {str(key): item for key, item in value.items() if str(key).strip()}


def _columns(value: object, fields: dict[str, object], llm_generated_fields: list[str]) -> list[str]:
    if isinstance(value, list):
        columns = [str(item).strip() for item in value if str(item).strip()]
        if columns:
            return columns
    return [str(key) for key in fields] + [field for field in llm_generated_fields if field not in fields]


def _evidence_fields(value: object, fields: dict[str, object], llm_generated_fields: list[str]) -> list[str]:
    llm_fields = set(llm_generated_fields)
    if isinstance(value, list):
        parsed = [str(item).strip() for item in value if str(item).strip() and str(item).strip() not in llm_fields]
        if parsed:
            return parsed
    return [str(key) for key in fields if str(key) not in llm_fields]


def _validate_column_mappings(columns: list[str], fields: dict[str, object], llm_generated_fields: list[str]) -> None:
    llm_fields = set(llm_generated_fields)
    missing = [column for column in columns if column not in fields and column not in llm_fields]
    if missing:
        joined = ", ".join(missing[:5])
        raise ValueError(f"TOOL_INVALID_ARGUMENTS: fields missing mappings for columns: {joined}")


def _fields_without_llm_generated(fields: dict[str, object], llm_generated_fields: list[str]) -> dict[str, object]:
    llm_fields = set(llm_generated_fields)
    return {str(key): value for key, value in fields.items() if str(key).strip() and str(key) not in llm_fields}


def _field_names(value: object) -> list[str]:
    return [str(item).strip() for item in value if str(item).strip()] if isinstance(value, list) else []


def _completion_evidence(value: object) -> dict[str, object]:
    result = dict(value) if isinstance(value, dict) and value else {"method": "api_json_collection"}
    result.setdefault("retrieved_at", _now())
    return result


def _bounded_limit(value: object) -> int:
    try:
        limit = int(value) if value not in (None, "") else 10
    except (TypeError, ValueError):
        limit = 10
    return max(1, min(_MAX_ITEMS_PER_REQUEST, limit))


def _bool_param(value: object) -> bool:
    return bool(value) if isinstance(value, bool) else str(value).strip().lower() in {"1", "true", "yes", "on"}


def _request_delay_seconds(value: object, request_count: int) -> float:
    floor = _request_delay_floor(request_count)
    if value in (None, ""):
        return floor
    try:
        return max(floor, min(60.0, float(value)))
    except (TypeError, ValueError):
        return floor


def _request_delay_floor(request_count: int) -> float:
    return _BATCH_REQUEST_DELAY_FLOOR_SECONDS if request_count > _BATCH_REQUEST_DELAY_FLOOR_COUNT else 0.0


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


__all__ = ["collection_request"]
