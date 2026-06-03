
from __future__ import annotations

import json
from typing import Any

_MAX_INLINE_JSON = 1600
_TOP_LEVEL_KEYS = (
    "action",
    "status",
    "session_id",
    "target_path",
    "manifest_path",
    "code",
    "message",
    "error_code",
    "recommended_action",
    "retryable",
)
_TOOL_CALL_SUFFIX = "_tool_call"


def actionable_tool_result_summary(result: object, archive_record: dict[str, object]) -> str:
    payload = _json_object(str(getattr(result, "output", "") or ""))
    if not payload:
        return ""
    action_fields = _action_fields(payload)
    if not action_fields and not _has_error_fact(result, payload):
        return ""
    lines = [
        f"[tool={getattr(result, 'tool', '')}; status={'ok' if getattr(result, 'ok', False) else 'error'}]",
        "actionable_tool_result:",
        "- policy: full tool output is archived; live prompt keeps structured recovery facts.",
    ]
    lines.extend(_top_level_lines(payload, result))
    lines.extend(_artifact_integrity_lines(payload.get("artifact_integrity")))
    lines.extend(_tool_call_lines(action_fields))
    lines.extend(_archive_lines(archive_record))
    return "\n".join(lines)


def _action_fields(payload: dict[str, Any]) -> dict[str, object]:
    return {
        key: value
        for key, value in payload.items()
        if key.endswith(_TOOL_CALL_SUFFIX) and isinstance(value, dict)
    }


def _has_error_fact(result: object, payload: dict[str, Any]) -> bool:
    return bool(
        not getattr(result, "ok", True)
        or payload.get("error_code")
        or payload.get("recommended_action")
        or payload.get("artifact_integrity")
    )


def _top_level_lines(payload: dict[str, Any], result: object) -> list[str]:
    lines: list[str] = []
    for key in _TOP_LEVEL_KEYS:
        value = payload.get(key)
        if value not in (None, "", [], {}):
            lines.append(f"- {key}: {_json_inline(value)}")
    error_code = str(getattr(result, "error_code", "") or "")
    if error_code and not payload.get("error_code"):
        lines.append(f"- error_code: {error_code}")
    recommended = str(getattr(result, "recommended_action", "") or "")
    if recommended and not payload.get("recommended_action"):
        lines.append(f"- recommended_action: {recommended}")
    return lines


def _artifact_integrity_lines(value: object) -> list[str]:
    if not isinstance(value, dict):
        return []
    lines = ["- artifact_integrity: available"]
    for key in ("blocker_codes", "warning_codes", "ok"):
        if key in value:
            lines.append(f"- artifact_integrity_{key}: {_json_inline(value.get(key))}")
    issue_codes = _issue_codes(value.get("issues"))
    if issue_codes:
        lines.append(f"- artifact_integrity_issue_codes: {_json_inline(issue_codes)}")
    return lines


def _issue_codes(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    codes: list[str] = []
    for item in value[:8]:
        if isinstance(item, dict) and item.get("code"):
            codes.append(str(item.get("code")))
    return codes


def _tool_call_lines(fields: dict[str, object]) -> list[str]:
    lines: list[str] = []
    for key in sorted(fields):
        lines.append(f"- {key}: {_json_inline(fields[key])}")
    return lines


def _archive_lines(record: dict[str, object]) -> list[str]:
    return [
        f"- output_path: {record.get('output_path', '')}",
        f"- output_artifact_ref: {record.get('artifact_ref') or record.get('output_path') or ''}",
        f"- output_call_id: {record.get('id', '')}",
        f"- output_scoped_call_id: {record.get('scoped_call_id', '')}",
        f"- output_hash: {record.get('output_hash', '')}",
    ]


def _json_object(text: str) -> dict[str, Any] | None:
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _json_inline(value: object) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    except TypeError:
        text = repr(value)
    return text if len(text) <= _MAX_INLINE_JSON else text[:_MAX_INLINE_JSON].rstrip() + "...[truncated]"
