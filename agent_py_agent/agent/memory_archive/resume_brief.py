
from __future__ import annotations

"""builds concise human/LLM-readable recovery briefs for memory-resume.

新手说明:
这个文件只负责把归档线索、LocalStore 线索和任务事实源压成一份'恢复简报'。
它不读取文件、不修改状态，只帮人和后续自动化快速知道下一步该看哪里。
"""

import json
from dataclasses import dataclass
from typing import Any

from ..common.value_parsing import dedupe_strings, sequence_strings


@dataclass
class RecoveryBriefContext:
    """Bundle for _context_block keyword parameters."""

    latest_user_intents: list[str]
    latest_assistant_actions: list[str]
    related_ids: dict[str, list[str]]
    likely_task_statuses: list[dict[str, str]]
    recommended_read_paths: list[str]
    next_actions: list[str]
    authority_note: str


@dataclass(frozen=True)
class ResumeBriefParams:
    recommended_read_paths: list[str] | None = None
    next_actions: list[str] | None = None


def build_resume_brief(
    archive_matches: list[dict[str, Any]],
    local_hits: list[dict[str, Any]],
    task_payloads: list[dict[str, Any]],
    *,
    params: ResumeBriefParams | None = None,
    recommended_read_paths: list[str] | None = None,
    next_actions: list[str] | None = None,
) -> dict[str, Any]:
    """Synthesize a compact recovery brief from existing resume evidence."""

    values = params or ResumeBriefParams(recommended_read_paths, next_actions)
    recommended_read_paths = list(values.recommended_read_paths or [])
    next_actions = list(values.next_actions or [])
    latest_user_intents = _latest_user_intents(archive_matches)
    latest_assistant_actions = _latest_assistant_actions(archive_matches)
    related_ids = _related_ids(archive_matches, local_hits, task_payloads)
    likely_task_statuses = _likely_task_statuses(task_payloads)
    authority_note = "archive/local matches are recovery clues; task files are the authority for current state."
    ctx = RecoveryBriefContext(
        latest_user_intents=latest_user_intents,
        latest_assistant_actions=latest_assistant_actions,
        related_ids=related_ids,
        likely_task_statuses=likely_task_statuses,
        recommended_read_paths=recommended_read_paths,
        next_actions=next_actions,
        authority_note=authority_note,
    )
    return {
        "latest_user_intent": latest_user_intents[0] if latest_user_intents else "",
        "latest_assistant_action": latest_assistant_actions[0] if latest_assistant_actions else "",
        "latest_user_intents": latest_user_intents[:5],
        "latest_assistant_actions": latest_assistant_actions[:5],
        "related_ids": related_ids,
        "likely_task_statuses": likely_task_statuses,
        "recommended_read_paths": recommended_read_paths[:20],
        "next_actions": next_actions,
        "authority_note": authority_note,
        "summary": _summary(ctx),
        "context_block": _context_block(ctx),
    }


def _summary(ctx: RecoveryBriefContext) -> str:
    summary_lines = [
        _summary_line("latest_user_intent", ctx.latest_user_intents[:1]),
        _summary_line("latest_assistant_action", ctx.latest_assistant_actions[:1]),
        _summary_line("related_run_ids", ctx.related_ids["run_ids"][:3]),
        _summary_line("likely_task_status", _task_status_summaries(ctx.likely_task_statuses[:3])),
    ]
    return "\n".join(line for line in summary_lines if line)


def _task_status_summaries(statuses: list[dict[str, str]]) -> list[str]:
    return [f"{item['run_id']} {item['status']}/{item['verification_status']}" for item in statuses]


def _latest_user_intents(archive_matches: list[dict[str, Any]]) -> list[str]:

    values: list[str] = []
    for record in archive_matches:
        payload = record.get("payload", {}) if isinstance(record.get("payload"), dict) else {}
        values.extend(sequence_strings(payload.get("user_intents"), allow_scalar=True))
        if record.get("speaker") == "user":
            values.append(str(record.get("content_preview", "") or ""))
    return dedupe_strings(values)


def _latest_assistant_actions(archive_matches: list[dict[str, Any]]) -> list[str]:

    values: list[str] = []
    for record in archive_matches:
        payload = record.get("payload", {}) if isinstance(record.get("payload"), dict) else {}
        values.extend(sequence_strings(payload.get("assistant_actions"), allow_scalar=True))
        if record.get("speaker") == "assistant":
            values.append(str(record.get("content_preview", "") or ""))
    return dedupe_strings(values)


def _related_ids(
    archive_matches: list[dict[str, Any]],
    local_hits: list[dict[str, Any]],
    task_payloads: list[dict[str, Any]],
) -> dict[str, list[str]]:

    ids = {
        "session_ids": [],
        "request_ids": [],
        "run_ids": [],
        "task_ids": [],
    }
    for record in archive_matches:
        _append(ids["session_ids"], record.get("session_id"))
        _append(ids["request_ids"], record.get("request_id"))
        _append(ids["run_ids"], record.get("run_id"))
        _append(ids["task_ids"], record.get("task_id"))
    for hit in local_hits:
        source_id = str(hit.get("source_id", "") or "")
        if source_id.startswith("gwreq-"):
            _append(ids["request_ids"], source_id)
        if source_id.startswith("subagent-"):
            _append(ids["run_ids"], source_id)
            _append(ids["task_ids"], source_id)
        metadata = hit.get("metadata", {}) if isinstance(hit.get("metadata"), dict) else {}
        _append(ids["request_ids"], metadata.get("request_id"))
        _append(ids["run_ids"], metadata.get("run_id"))
        _append(ids["task_ids"], metadata.get("task_id"))
    for task in task_payloads:
        _append(ids["run_ids"], task.get("run_id"))
        _append(ids["task_ids"], task.get("run_id"))
    return ids


def _likely_task_statuses(task_payloads: list[dict[str, Any]]) -> list[dict[str, str]]:

    statuses: list[dict[str, str]] = []
    for task in task_payloads:
        statuses.append(
            {
                "run_id": str(task.get("run_id", "") or ""),
                "exists": str(bool(task.get("exists", False))).lower(),
                "status": str(task.get("status", "missing") or "missing"),
                "verification_status": str(task.get("verification_status", "unknown") or "unknown"),
                "goal": str(task.get("goal", "") or ""),
                "task_dir": str(task.get("task_dir", "") or ""),
            }
        )
    return statuses


def _context_block(ctx: RecoveryBriefContext) -> str:

    lines = ["# Recovery Brief", "", f"- authority: {ctx.authority_note}"]
    lines.append(f"- latest_user_intent: {ctx.latest_user_intents[0] if ctx.latest_user_intents else 'unknown'}")
    lines.append(
        f"- latest_assistant_action: {ctx.latest_assistant_actions[0] if ctx.latest_assistant_actions else 'unknown'}"
    )
    lines.append("- related_ids: " + json.dumps(ctx.related_ids, ensure_ascii=False, sort_keys=True))
    if ctx.likely_task_statuses:
        lines.append("- likely_task_statuses:")
        for item in ctx.likely_task_statuses[:5]:
            lines.append(
                f"  - {item['run_id']} {item['status']}/{item['verification_status']} :: {item['goal']}"
            )
    else:
        lines.append("- likely_task_statuses: none")
    lines.append("- must_read:")
    for path in ctx.recommended_read_paths[:10] or ["none"]:
        lines.append(f"  - {path}")
    lines.append("- next_actions:")
    for action in ctx.next_actions[:5]:
        lines.append(f"  - {action}")
    return "\n".join(lines)


def _summary_line(label: str, values: list[str]) -> str:

    return f"{label}: {values[0]}" if values else ""



def _append(items: list[str], value: object) -> None:

    text = str(value or "").strip()
    if text and text not in items:
        items.append(text)
