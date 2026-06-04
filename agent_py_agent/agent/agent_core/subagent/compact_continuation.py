from __future__ import annotations

"""Task-local compact continuation prompt section for subagent runners."""

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...subagents import SubAgentExecutionContext
from . import compact_continuation_io as compact_io

_DEFAULT_SNIPPET_CHARS = 1200
_STALE_PACKET_SECONDS = 7 * 24 * 60 * 60
_REF_KEYS = (
    "agent_run_task",
    "agent_run_checkpoint",
    "agent_run_summary",
    "agent_run_latest_session_compaction_metadata",
    "agent_run_latest_session_compaction_summary",
    "agent_run_final_report",
    "agent_run_findings",
    "agent_run_timeline",
)


@dataclass(frozen=True)
class SubagentCompactContinuationRequest:
    context: SubAgentExecutionContext
    max_chars: int = _DEFAULT_SNIPPET_CHARS


def build_subagent_compact_continuation_section(request: SubagentCompactContinuationRequest) -> str:
    refs = _workspace_refs(request.context)
    packet = _latest_continue_packet_path(refs)
    existing_refs = _existing_refs(refs)
    if not existing_refs and not packet:
        return ""
    lines = [
        "## Task-Local Compact Continuation",
        "",
        "- memory_scope: task_local",
        "- writes_main_memory: false",
        "- automatic_tool_execution: none",
        "- 只从下面的子代理任务目录接续；不要读取或写入主代理长期 memory。",
        "",
    ]
    lines.extend(_preflight_lines(request.context.context_bundle, request.max_chars))
    lines.extend(_packet_lines(packet, request.max_chars))
    lines.extend(_session_compact_lines(existing_refs, request.max_chars))
    lines.extend(_ref_lines(existing_refs))
    lines.extend(_snippet_lines(existing_refs, request.max_chars))
    return "\n".join(lines).rstrip()


def _workspace_refs(context: SubAgentExecutionContext) -> dict[str, str]:
    bundle = context.context_bundle if isinstance(context.context_bundle, dict) else {}
    refs = bundle.get("workspace_refs") if isinstance(bundle.get("workspace_refs"), dict) else {}
    values = {str(key): str(value) for key, value in refs.items() if str(value or "").strip()}
    run_workspace = values.get("agent_work_dir") or values.get("agent_run_workspace", "")
    if run_workspace:
        base = Path(run_workspace)
        values.setdefault("agent_run_task", str(base / "task.md"))
        values.setdefault("agent_run_checkpoint", str(base / "checkpoint.json"))
        values.setdefault("agent_run_summary", str(base / "summary.md"))
        values.setdefault("agent_run_final_report", str(base / "final_report.md"))
        values.setdefault("agent_run_findings", str(base / "findings.jsonl"))
        values.setdefault("agent_run_timeline", str(base / "timeline.jsonl"))
        values.setdefault("agent_run_compactions", str(base / "compactions"))
    compactions = values.get("agent_run_compactions", "")
    if compactions:
        session = Path(compactions) / "session"
        values.setdefault("agent_run_latest_continue_packet", str(session / "latest_continue_packet.json"))
        values.setdefault("agent_run_latest_session_compaction_metadata", str(session / "latest_metadata.json"))
        values.setdefault("agent_run_latest_session_compaction_summary", str(session / "latest_summary.md"))
        values.setdefault("agent_run_latest_compaction_metadata", str(Path(compactions) / "latest_metadata.json"))
        values.setdefault("agent_run_latest_compaction_summary", str(Path(compactions) / "latest_summary.md"))
    return values


def _latest_continue_packet_path(refs: dict[str, str]) -> Path | None:
    value = refs.get("agent_run_latest_continue_packet", "")
    if not value and refs.get("agent_run_compactions"):
        value = str(Path(refs["agent_run_compactions"]) / "session" / "latest_continue_packet.json")
    path = Path(value) if value else None
    return path if path and path.exists() else None


def _existing_refs(refs: dict[str, str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for key in _REF_KEYS:
        value = refs.get(key, "")
        if value and Path(value).exists():
            result[key] = value
    return result


def _packet_lines(path: Path | None, max_chars: int) -> list[str]:
    if not path:
        return []
    payload, status, load_error = compact_io.read_json_with_status(
        path,
        context="subagent_compact_continuation.continue_packet",
    )
    lines = ["### Continue Packet", "", f"- latest_continue_packet: {path}"]
    if status != "ok":
        lines.extend([
            f"- packet_status: {status}",
            "- recovery_refs: checkpoint/summary/task-local refs",
        ])
        lines.extend(compact_io.load_error_lines("packet_load_error", load_error, max_chars))
        return lines + [""]
    if _is_stale_packet(path, payload):
        lines.extend([
            "- packet_status: stale",
            "- recovery_refs: checkpoint/summary/task-local refs",
        ])
        return lines + [""]
    for key in ("ready_to_continue", "continue_mode", "next_action"):
        if key in payload:
            lines.append(f"- {key}: {compact_io.short_value(payload[key], max_chars)}")
    lines.extend(_packet_progress_lines(payload, max_chars))
    paths = payload.get("recommended_read_paths")
    if isinstance(paths, list) and paths:
        lines.append("- recommended_read_paths:")
        lines.extend(f"  - {compact_io.short_value(item, max_chars)}" for item in paths[:5])
    lines.extend(_packet_read_policy_lines(payload))
    return lines + [""]


def _packet_progress_lines(payload: dict[str, Any], max_chars: int) -> list[str]:
    progress = payload.get("work_progress")
    if not isinstance(progress, dict) or not progress:
        return ["- work_progress: none"]
    lines = ["- work_progress:"]
    for key in ("summary", "latest_written_path", "next_action", "latest_tool_progress_ref"):
        value = progress.get(key)
        if str(value or "").strip():
            lines.append(f"  - {key}: {compact_io.short_value(value, max_chars)}")
    headings = progress.get("headings")
    if isinstance(headings, list) and headings:
        lines.append("  - headings:")
        lines.extend(f"    - {compact_io.short_value(item, max_chars)}" for item in headings[:8])
    return lines


def _packet_read_policy_lines(payload: dict[str, Any]) -> list[str]:
    progress = payload.get("work_progress")
    session = payload.get("session_compact")
    has_progress = isinstance(progress, dict) and bool(progress)
    has_session = isinstance(session, dict) and bool(session)
    if has_progress or has_session:
        return ["- packet_read_policy: read specific refs only when the summary is insufficient."]
    return [
        "- packet_read_policy: packet is already summarized here; "
        "because work_progress/session_compact are empty, start from the task goal instead of reading the packet body.",
    ]


def _session_compact_lines(refs: dict[str, str], max_chars: int) -> list[str]:
    metadata_ref = refs.get("agent_run_latest_session_compaction_metadata", "")
    summary_ref = refs.get("agent_run_latest_session_compaction_summary", "")
    if not metadata_ref and not summary_ref:
        metadata_ref = _session_metadata_ref(refs)
        summary_ref = refs.get("agent_run_latest_compaction_summary", "") if metadata_ref else ""
    if not metadata_ref and not summary_ref:
        return []
    payload, status, load_error = (
        compact_io.read_json_with_status(
            Path(metadata_ref),
            context="subagent_compact_continuation.session_compact_metadata",
        )
        if metadata_ref
        else ({}, "missing", None)
    )
    lines = ["### Session Compact Package", ""]
    lines.extend(_compact_ref_lines(metadata_ref, summary_ref))
    lines.extend(_compact_metadata_bullets(payload, status, load_error, max_chars))
    summary = compact_io.read_text(Path(summary_ref), max_chars) if summary_ref else ""
    if summary:
        lines.extend(["", summary])
    return lines + [""]


def _session_metadata_ref(refs: dict[str, str]) -> str:
    metadata_ref = refs.get("agent_run_latest_compaction_metadata", "")
    if not metadata_ref:
        return ""
    payload, status, _load_error = compact_io.read_json_with_status(
        Path(metadata_ref),
        context="subagent_compact_continuation.session_compact_metadata",
    )
    if status == "ok" and payload.get("schema_version") == "subagent_session_compact.v1":
        return metadata_ref
    return ""

def _compact_ref_lines(metadata_ref: str, summary_ref: str) -> list[str]:
    lines: list[str] = []
    if metadata_ref:
        lines.append(f"- latest_metadata: {metadata_ref}")
    if summary_ref:
        lines.append(f"- latest_summary: {summary_ref}")
    return lines


def _compact_metadata_bullets(
    payload: dict[str, Any],
    status: str,
    load_error: dict[str, object] | None,
    max_chars: int,
) -> list[str]:
    if status != "ok":
        lines = [f"- metadata_status: {status}"]
        lines.extend(compact_io.load_error_lines("metadata_load_error", load_error, max_chars))
        return lines
    keys = ("schema_version", "memory_scope", "writes_main_memory", "current_step", "next_action")
    return [f"- {key}: {compact_io.short_value(payload[key], max_chars)}" for key in keys if key in payload]


def _preflight_lines(context_bundle: dict[str, object], max_chars: int) -> list[str]:
    if not isinstance(context_bundle, dict):
        return []
    preflight = context_bundle.get("runner_recovery_preflight")
    if not isinstance(preflight, dict):
        return []
    status = str(preflight.get("packet_status") or "unknown")
    lines = [
        "### Recovery Preflight",
        "",
        f"- packet_status_before_prepare: {compact_io.short_value(status, max_chars)}",
        f"- packet_ref_before_prepare: {compact_io.short_value(preflight.get('packet_ref', ''), max_chars)}",
        "- recovery_refs: checkpoint/summary/task-local refs",
    ]
    instruction = str(preflight.get("runner_instruction") or "").strip()
    if instruction:
        lines.append(f"- runner_instruction: {compact_io.short_value(instruction, max_chars)}")
    refs = preflight.get("recovery_refs")
    if isinstance(refs, list) and refs:
        lines.append("- recovery_refs:")
        lines.extend(f"  - {compact_io.short_value(item, max_chars)}" for item in refs[:5])
    if preflight.get("save_may_regenerate_continue_packet") is True:
        lines.append("- prepare_note: runner prepare may regenerate latest_continue_packet after this preflight.")
    return lines + [""]


def _ref_lines(refs: dict[str, str]) -> list[str]:
    if not refs:
        return []
    lines = ["### Recovery Refs", ""]
    lines.extend(f"- {key}: {value}" for key, value in refs.items())
    return lines + [""]


def _snippet_lines(refs: dict[str, str], max_chars: int) -> list[str]:
    lines: list[str] = []
    for key in ("agent_run_checkpoint", "agent_run_summary", "agent_run_task", "agent_run_findings"):
        text = compact_io.read_text(Path(refs[key]), max_chars) if key in refs else ""
        if text:
            lines.extend([f"### {key}", "", text, ""])
    return lines


def _is_stale_packet(path: Path, payload: dict[str, Any]) -> bool:
    timestamp = _packet_timestamp(path, payload)
    return timestamp > 0 and time.time() - timestamp > _STALE_PACKET_SECONDS


def _packet_timestamp(path: Path, payload: dict[str, Any]) -> float:
    raw = payload.get("created_at")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        try:
            value = path.stat().st_mtime
        except OSError:
            return 0.0
    return max(0.0, value)


__all__ = ["SubagentCompactContinuationRequest", "build_subagent_compact_continuation_section"]
