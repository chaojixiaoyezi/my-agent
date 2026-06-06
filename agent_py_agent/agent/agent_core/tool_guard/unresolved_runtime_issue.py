
from __future__ import annotations

import json
from typing import Any


def unresolved_runtime_issues(params: object) -> list[dict[str, object]]:
    active: dict[str, dict[str, object]] = {}
    for record, fact in _issue_events(params):
        _apply_issue_event(active, record, fact)
    return list(active.values())


def unresolved_runtime_issue_context(params: object, redirects: int) -> str:
    issues = unresolved_runtime_issues(params)
    if not issues:
        return ""
    envelope = {
        "unresolved_runtime_issues": issues[:8],
        "repair_attempt": redirects + 1,
        "suggested_next_action": "repair_revalidate_or_report_real_blocker",
        "final_response_allowed": False,
        "suggested_rework": [
            "先定位失败工具、失败 target 和 blocker_codes。",
            "优先换工具、换路径、重写产物或重新复验；不要只重复同一个失败动作。",
            "如果确实无法修复，请明确说明真实阻塞原因、已经尝试过的动作和需要用户补充什么。",
        ],
    }
    return "\n".join(
        [
            "[tool-system unresolved-runtime-issues]",
            json.dumps(envelope, ensure_ascii=False, sort_keys=True),
            "已有结构化工具失败尚未被后续成功记录清除；下一轮请修复、复验或换策略，无法推进时说明真实阻塞原因。",
        ]
    )


def has_unresolved_runtime_issues(params: object) -> bool:
    return bool(unresolved_runtime_issues(params))


def _archive_records(params: object) -> list[dict[str, object]]:
    records = getattr(params, "archive_tool_calls", [])
    return [item for item in records if isinstance(item, dict)]


def _issue_events(params: object):
    for record in _archive_records(params):
        for fact in _record_issue_facts(record):
            yield record, fact


def _apply_issue_event(
    active: dict[str, dict[str, object]],
    record: dict[str, object],
    fact: dict[str, object],
) -> None:
    key = str(fact.get("issue_key") or "")
    if not key:
        return
    if fact.get("clears_issue"):
        active.pop(key, None)
        return
    if fact.get("blocks_final"):
        active[key] = _issue_payload(fact, record)


def _record_issue_facts(record: dict[str, object]) -> list[dict[str, object]]:
    facts: list[dict[str, object]] = []
    facts.extend(_artifact_integrity_facts(record))
    facts.extend(_successful_artifact_write_facts(record))
    return facts


def _artifact_integrity_facts(record: dict[str, object]) -> list[dict[str, object]]:
    integrity = _artifact_integrity(record)
    if not integrity:
        return []
    key = _artifact_issue_key(integrity)
    if bool(integrity.get("ok")):
        return [{"issue_key": key, "clears_issue": True}]
    codes = _blocker_code_list(integrity.get("blocker_codes")) or _issue_codes(
        integrity.get("issues"),
        severity="blocker",
    )
    if not codes:
        return []
    return [
        {
            "issue_key": key,
            "blocks_final": True,
            "code": "ARTIFACT_INTEGRITY_UNRESOLVED",
            "error_code": str(record.get("error_code") or ""),
            "kind": str(integrity.get("kind") or "artifact"),
            "target": str(integrity.get("path") or ""),
            "blocker_codes": codes[:12],
        }
    ]


def _artifact_integrity(record: dict[str, object]) -> dict[str, object]:
    envelope = record.get("tool_result_envelope")
    if isinstance(envelope, dict) and isinstance(envelope.get("artifact_integrity"), dict):
        return _artifact_integrity_with_path(envelope, record)
    value = record.get("artifact_integrity")
    return dict(value) if isinstance(value, dict) else {}


def _artifact_integrity_with_path(
    envelope: dict[str, object],
    record: dict[str, object],
) -> dict[str, object]:
    integrity = dict(envelope["artifact_integrity"])
    if str(integrity.get("path") or "").strip():
        return integrity
    refs = _target_refs(envelope) or _target_refs(record)
    if refs:
        integrity["path"] = refs[0]
    return integrity


def _artifact_issue_key(integrity: dict[str, object]) -> str:
    kind = str(integrity.get("kind") or "artifact")
    target = str(integrity.get("path") or integrity.get("target_path") or "")
    return f"artifact_integrity:{kind}:{target}"


def _successful_artifact_write_facts(record: dict[str, object]) -> list[dict[str, object]]:
    if not bool(record.get("ok")):
        return []
    envelope = record.get("tool_result_envelope")
    if not isinstance(envelope, dict):
        return []
    if not _is_successful_artifact_write(record, envelope):
        return []
    facts: list[dict[str, object]] = []
    for target in _target_refs(envelope):
        for kind in _artifact_kinds_for_path(target):
            facts.append({
                "issue_key": f"artifact_integrity:{kind}:{target}",
                "clears_issue": True,
            })
    return facts


def _is_successful_artifact_write(record: dict[str, object], envelope: dict[str, object]) -> bool:
    tool = str(record.get("tool") or "")
    status = str(envelope.get("status") or "")
    action = str(envelope.get("action") or "")
    return bool(_target_refs(envelope))


def _target_refs(payload: dict[str, object]) -> list[str]:
    refs: list[str] = []
    for key in ("path", "target_path", "output_path"):
        refs.extend(_path_values(payload.get(key)))
    return _dedupe_refs(refs)


def _path_values(value: object) -> list[str]:
    if isinstance(value, dict):
        ordered = [
            value.get("resolved"),
            value.get("path"),
            value.get("raw"),
            value.get("display"),
            value.get("artifact_ref"),
        ]
        return [str(item) for item in ordered if str(item or "").strip()]
    text = str(value or "").strip()
    return [text] if text else []


def _artifact_kinds_for_path(path: str) -> list[str]:
    lowered = path.lower()
    if lowered.endswith((".html", ".htm")):
        return ["html", "artifact"]
    if lowered.endswith(".json"):
        return ["json", "artifact"]
    return ["artifact"]


def _dedupe_refs(values: list[str]) -> list[str]:
    seen: set[str] = set()
    refs: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            refs.append(text)
    return refs


def _issue_payload(fact: dict[str, object], record: dict[str, object]) -> dict[str, object]:
    payload = {
        "code": fact.get("code", ""),
        "error_code": fact.get("error_code", ""),
        "kind": fact.get("kind", ""),
        "target": fact.get("target", ""),
        "blocker_codes": fact.get("blocker_codes", []),
        "tool": record.get("tool", ""),
        "call_id": record.get("call_id") or record.get("id") or "",
        "operation_id": record.get("operation_id", ""),
    }
    return {key: value for key, value in payload.items() if value not in ("", [], {}, None)}


def _issue_codes(value: object, *, severity: str = "") -> list[str]:
    if not isinstance(value, list):
        return []
    return [
        str(item.get("code"))
        for item in value
        if (
            isinstance(item, dict)
            and str(item.get("code") or "").strip()
            and (not severity or str(item.get("severity") or "") == severity)
        )
    ]


def _blocker_code_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item or "").strip()]


__all__ = [
    "has_unresolved_runtime_issues",
    "unresolved_runtime_issue_context",
    "unresolved_runtime_issues",
]
