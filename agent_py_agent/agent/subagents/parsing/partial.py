
from __future__ import annotations

import json

from ...common.value_parsing import sequence_strings
from ..models import SUBAGENT_FAILURE_STATUSES, TaskStatus, task_status_in


def extract_partial_subagent_result_text(text: str) -> str:
    """Build a minimal safe result from complete leading fields in an incomplete result block."""

    leading = _json_candidate_text(text)
    if not leading.startswith("{"):
        return ""
    status = _json_field_value(leading, "status")
    summary = _json_field_value(leading, "summary")
    evidence_packets = _dict_list(_json_field_value(leading, "evidence_packets"))
    if not evidence_packets:
        evidence_packets = _json_field_dict_items(leading, "evidence_packets")
    blocked_reason = _json_field_value(leading, "blocked_reason")
    failure_type = _json_field_value(leading, "failure_type")
    if not _partial_result_is_safe(status, evidence_packets, blocked_reason, failure_type):
        return ""
    payload = {
        "status": str(status or "DONE"),
        "summary": str(summary or "结构化结果尾部被截断，已从完整证据包恢复最小结果。"),
        "used_tools": sequence_strings(_json_field_value(leading, "used_tools")),
        "used_skills": sequence_strings(_json_field_value(leading, "used_skills")),
        "evidence": _dict_list(_json_field_value(leading, "evidence")),
        "evidence_packets": evidence_packets,
        "capability_requests": _dict_list(_json_field_value(leading, "capability_requests")),
        "artifacts": _dict_list(_json_field_value(leading, "artifacts")),
        "tests": _dict_list(_json_field_value(leading, "tests")),
        "patches": _dict_list(_json_field_value(leading, "patches")),
        "lessons": sequence_strings(_json_field_value(leading, "lessons")),
        "next_actions": sequence_strings(_json_field_value(leading, "next_actions")),
        "blocked_reason": str(blocked_reason or ""),
        "failure_type": str(failure_type or ""),
    }
    return json.dumps(payload, ensure_ascii=False)


def _json_candidate_text(text: str) -> str:
    leading = text.lstrip()
    lowered = leading.lower()
    if lowered.startswith("json"):
        return leading[4:].lstrip()
    if leading.startswith("```"):
        return _strip_json_fence(leading)
    return leading


def _json_field_value(text: str, key: str) -> object:
    marker = f'"{key}"'
    start = text.find(marker)
    if start < 0:
        return None
    colon = text.find(":", start + len(marker))
    if colon < 0:
        return None
    try:
        value, _ = json.JSONDecoder().raw_decode(text[colon + 1:].lstrip())
    except json.JSONDecodeError:
        return None
    return value


def _json_field_dict_items(text: str, key: str) -> list[dict[str, object]]:
    marker = f'"{key}"'
    start = text.find(marker)
    if start < 0:
        return []
    colon = text.find(":", start + len(marker))
    bracket = text.find("[", colon + 1)
    if colon < 0 or bracket < 0:
        return []
    return _decode_complete_dict_items(text, bracket + 1)


def _decode_complete_dict_items(text: str, index: int) -> list[dict[str, object]]:
    decoder = json.JSONDecoder()
    items: list[dict[str, object]] = []
    while index < len(text):
        index = _skip_array_separators(text, index)
        if index >= len(text) or text[index] != "{":
            break
        try:
            value, end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            break
        if isinstance(value, dict):
            items.append(value)
        index += end
    return items


def _skip_array_separators(text: str, index: int) -> int:
    while index < len(text) and text[index] in " \t\r\n,":
        index += 1
    return index


def _partial_result_is_safe(
    status: object,
    evidence_packets: list[dict[str, object]],
    blocked_reason: object,
    failure_type: object,
) -> bool:
    if task_status_in(status, SUBAGENT_FAILURE_STATUSES):
        return bool(str(blocked_reason or failure_type or "").strip())
    if not task_status_in(status, {TaskStatus.DONE.value}):
        return False
    return any(_packet_has_traceable_ref(item) for item in evidence_packets)


def _packet_has_traceable_ref(packet: dict[str, object]) -> bool:
    return bool(sequence_strings(packet.get("artifact_refs")) or sequence_strings(packet.get("evidence_refs")))


def _dict_list(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _strip_json_fence(raw: str) -> str:
    stripped = raw.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()
