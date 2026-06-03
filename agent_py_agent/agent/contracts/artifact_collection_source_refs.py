
from __future__ import annotations


def source_refs_by_id(value: object, lookup_path) -> dict[str, dict[str, object]]:
    refs = lookup_path(value, "source_refs")
    if not isinstance(refs, list):
        return {}
    result: dict[str, dict[str, object]] = {}
    for item in refs:
        if isinstance(item, dict):
            _add_source_ref(result, item)
    return result


def source_ref_is_audited(item: dict[str, object]) -> bool:
    if str(item.get("artifact_ref") or "").strip():
        return True
    reserved = item.get("reserved")
    if isinstance(reserved, dict) and _has_operation_binding(reserved):
        return True
    return _has_fetch_binding(item, reserved)


def _has_operation_binding(reserved: dict[str, object]) -> bool:
    return any(str(reserved.get(key) or "").strip() for key in ("tool_call_id", "operation_id", "tool_result_id"))


def _has_fetch_binding(item: dict[str, object], reserved: object) -> bool:
    if not isinstance(reserved, dict):
        return False
    source_type = str(item.get("source_type") or "").strip()
    content_hash = str(item.get("content_sha256") or "").strip()
    has_http_status = reserved.get("http_status") is not None
    return source_type in {"api_json", "http_json"} and bool(content_hash) and has_http_status


def _add_source_ref(result: dict[str, dict[str, object]], item: dict[str, object]) -> None:
    source_id = str(item.get("source_id") or "").strip()
    if source_id and (str(item.get("uri") or "").strip() or str(item.get("artifact_ref") or "").strip()):
        result[source_id] = item


__all__ = ["source_ref_is_audited", "source_refs_by_id"]
