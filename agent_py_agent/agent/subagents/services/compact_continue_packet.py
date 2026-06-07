
from __future__ import annotations

"""Task-local subagent continue packet writer.

Continuation readiness uses current TaskStatus/VerificationStatus helpers;
unknown raw status text and free-form summaries are audit data only.
"""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...common.json_io import read_json_object_report
from ..models import SUBAGENT_HANDLED_TERMINAL_STATUSES, SubAgentTask, task_needs_continuation

_SCHEMA_VERSION = "subagent_continue_packet.v1"
_CLOSED_STATUSES = SUBAGENT_HANDLED_TERMINAL_STATUSES


@dataclass(frozen=True)
class SubagentContinuePacketRequest:
    task: SubAgentTask
    output_payload: dict[str, object]
    load_errors: tuple[dict[str, object], ...] = ()


def write_subagent_continue_packet(request: SubagentContinuePacketRequest) -> str:
    compactions = _compactions_dir(request.task)
    if not compactions:
        return ""
    session_dir = compactions / "session"
    session_dir.mkdir(parents=True, exist_ok=True)
    packet_ref = session_dir / "latest_continue_packet.json"
    packet = build_subagent_continue_packet(request, packet_ref)
    packet_ref.write_text(json.dumps(packet, ensure_ascii=False, indent=2), encoding="utf-8")
    request.task.agent_run_latest_session_continue_packet_json = str(packet_ref)
    ledger_ref = session_dir / "session_compact_ledger.jsonl"
    request.task.agent_run_session_compaction_ledger_jsonl = str(ledger_ref)
    _append_packet_ledger(ledger_ref, packet, packet_ref)
    return str(packet_ref)


def build_subagent_continue_packet(request: SubagentContinuePacketRequest, packet_ref: Path) -> dict[str, Any]:
    task = request.task
    restore_refs = _restore_refs(task)
    session_compact, session_load_error = _session_compact_refs(task)
    work_progress, progress_load_error = _work_progress_payload(task, request.output_payload)
    load_errors = _load_errors(
        [
            *request.load_errors,
            session_load_error,
            progress_load_error,
        ]
    )
    return {
        "schema_version": _SCHEMA_VERSION,
        "kind": "subagent_task_local_continue_packet",
        "owner": {"owner_type": "subagent_run", "owner_id": task.id},
        "memory_scope": "task_local",
        "writes_main_memory": False,
        "automatic_tool_execution": "none",
        "ready_to_continue": _ready_to_continue(task),
        "continue_mode": "subagent_task_local",
        "run_id": task.id,
        "root_id": task.root_id or task.id,
        "parent_id": task.parent_id,
        "role": task.role,
        "agent_name": task.agent_name,
        "status": task.status,
        "verification_status": task.verification_status,
        "current_step": task.current_step or task.status,
        "latest_summary": _latest_summary(task, request.output_payload, work_progress),
        "next_action": _next_action(task, request.output_payload),
        "blockers": _unique_strings([*task.blockers, *_strings(request.output_payload.get("blockers"))]),
        "restore_refs": restore_refs,
        "session_compact": session_compact,
        "work_progress": work_progress,
        "recommended_read_paths": _recommended_read_paths(task, packet_ref, restore_refs),
        "guard": _guard_payload(task),
        "load_errors": load_errors,
    }


def _load_errors(values: list[object]) -> list[dict[str, object]]:
    return [value for value in values if isinstance(value, dict)]


def _compactions_dir(task: SubAgentTask) -> Path | None:
    value = str(getattr(task, "agent_run_compactions_dir", "") or "").strip()
    return Path(value) if value else None


def _restore_refs(task: SubAgentTask) -> dict[str, str]:
    pairs = {
        "agent_work_dir": task.agent_run_workspace_dir,
        "agent_run_task": task.agent_run_task_md,
        "agent_run_checkpoint": task.agent_run_checkpoint_json,
        "agent_run_summary": task.agent_run_summary_md,
        "agent_run_final_report": task.agent_run_final_report_md,
        "agent_run_findings": task.agent_run_findings_jsonl,
        "agent_run_timeline": task.agent_run_timeline_jsonl,
        "agent_run_compactions": task.agent_run_compactions_dir,
        "agent_run_latest_compaction_summary": task.agent_run_latest_compaction_summary_md,
        "agent_run_latest_compaction_metadata": task.agent_run_latest_compaction_metadata_json,
        "agent_run_session_compaction_ledger": task.agent_run_session_compaction_ledger_jsonl,
        "agent_run_latest_session_compaction_summary": task.agent_run_latest_session_compaction_summary_md,
        "agent_run_latest_session_compaction_metadata": task.agent_run_latest_session_compaction_metadata_json,
        "agent_run_latest_session_continue_packet": task.agent_run_latest_session_continue_packet_json,
        "agent_run_tool_progress": _tool_progress_ref(task),
        "agent_run_latest_tool_progress": _latest_tool_progress_ref(task),
        "runner_result": task.runner_result_json,
        "output_json": task.output_json,
        "takeover_readiness": task.takeover_readiness_json,
        "shared_blackboard": task.task_workspace_shared_blackboard,
        "shared_messages": task.task_workspace_shared_messages_jsonl,
        "shared_findings": task.task_workspace_shared_findings_jsonl,
    }
    return {key: str(value) for key, value in pairs.items() if str(value or "").strip()}


def subagent_restore_refs(task: SubAgentTask) -> dict[str, str]:
    return _restore_refs(task)


def _session_compact_refs(task: SubAgentTask) -> tuple[dict[str, str], dict[str, object] | None]:
    metadata_ref = str(getattr(task, "agent_run_latest_session_compaction_metadata_json", "") or "").strip()
    summary_ref = str(getattr(task, "agent_run_latest_session_compaction_summary_md", "") or "").strip()
    if not metadata_ref or not Path(metadata_ref).exists():
        return {}, None
    report = read_json_object_report(
        Path(metadata_ref),
        parse_nested_string=True,
        context="subagent.continue_packet.session_compact",
    )
    if report.load_error:
        return {}, report.load_error
    payload = report.payload
    return {
        "schema_version": str(payload.get("schema_version") or "subagent_session_compact.v1"),
        "package_id": str(payload.get("package_id") or ""),
        "metadata_ref": metadata_ref,
        "summary_ref": summary_ref if summary_ref and Path(summary_ref).exists() else "",
        "next_action": str(payload.get("next_action") or ""),
    }, None


def _recommended_read_paths(task: SubAgentTask, packet_ref: Path, restore_refs: dict[str, str]) -> list[str]:
    values = [
        str(packet_ref),
        restore_refs.get("agent_run_latest_tool_progress", ""),
        restore_refs.get("agent_run_checkpoint", ""),
        restore_refs.get("agent_run_summary", ""),
        str(getattr(task, "agent_run_latest_session_compaction_metadata_json", "") or ""),
        str(getattr(task, "agent_run_latest_session_compaction_summary_md", "") or ""),
        restore_refs.get("agent_run_task", ""),
        restore_refs.get("output_json", ""),
        restore_refs.get("runner_result", ""),
        restore_refs.get("takeover_readiness", ""),
        restore_refs.get("shared_messages", ""),
    ]
    return _unique_strings([item for item in values if _path_exists_or_is_future_ref(item, task)])


def _path_exists_or_is_future_ref(path_text: str, task: SubAgentTask) -> bool:
    if not path_text:
        return False
    if path_text.endswith("latest_continue_packet.json"):
        return True
    if path_text in {task.output_json, task.runner_result_json}:
        return True
    return Path(path_text).exists()


def _ready_to_continue(task: SubAgentTask) -> bool:
    return task_needs_continuation(task, closed_statuses=_CLOSED_STATUSES)


def _next_action(task: SubAgentTask, output_payload: dict[str, object]) -> str:
    candidates = [
        task.current_step,
        *_strings(output_payload.get("next_actions")),
        task.latest_status_report.next_recommended_action if task.latest_status_report else "",
        f"resolve blocker: {task.blockers[0]}" if task.blockers else "",
        "resume runner from task-local checkpoint",
    ]
    return next((str(item).strip() for item in candidates if str(item or "").strip()), "")


def _latest_summary(
    task: SubAgentTask,
    output_payload: dict[str, object],
    work_progress: dict[str, Any],
) -> str:
    candidates = [
        work_progress.get("summary"),
        output_payload.get("summary"),
        task.latest_summary,
    ]
    return next((str(item).strip() for item in candidates if str(item or "").strip()), "")


def _work_progress_payload(task: SubAgentTask, output_payload: dict[str, object]) -> tuple[dict[str, Any], dict[str, object] | None]:
    explicit = output_payload.get("work_progress")
    if isinstance(explicit, dict):
        return explicit, None
    ref = _latest_tool_progress_ref(task)
    if not ref or not Path(ref).exists():
        return {}, None
    report = read_json_object_report(
        Path(ref),
        parse_nested_string=True,
        context="subagent.continue_packet.work_progress",
    )
    return report.payload, report.load_error


def _tool_progress_ref(task: SubAgentTask) -> str:
    workspace = str(getattr(task, "agent_run_workspace_dir", "") or "").strip()
    return str(Path(workspace) / "progress" / "tool_progress.jsonl") if workspace else ""


def _latest_tool_progress_ref(task: SubAgentTask) -> str:
    workspace = str(getattr(task, "agent_run_workspace_dir", "") or "").strip()
    return str(Path(workspace) / "progress" / "latest_tool_progress.json") if workspace else ""


def _guard_payload(task: SubAgentTask) -> dict[str, object]:
    return {
        "allowed_to_continue": _ready_to_continue(task),
        "requires_parent_dispatch": True,
        "automatic_tool_execution": "none",
        "writes_main_memory": False,
    }


def _append_packet_ledger(path: Path, packet: dict[str, Any], packet_ref: Path) -> None:
    row = {
        "schema_version": "subagent_session_compact_ledger.v1",
        "run_id": packet.get("run_id", ""),
        "root_id": packet.get("root_id", ""),
        "status": packet.get("status", ""),
        "ready_to_continue": packet.get("ready_to_continue", False),
        "packet_ref": str(packet_ref),
        "memory_scope": "task_local",
        "writes_main_memory": False,
        "state_fingerprint": _packet_state_fingerprint(packet),
    }
    if _last_packet_ledger_fingerprint(path) == row["state_fingerprint"]:
        return
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def _packet_state_fingerprint(packet: dict[str, Any]) -> str:
    progress = packet.get("work_progress") if isinstance(packet.get("work_progress"), dict) else {}
    session = packet.get("session_compact") if isinstance(packet.get("session_compact"), dict) else {}
    payload = {
        "run_id": packet.get("run_id", ""),
        "status": packet.get("status", ""),
        "verification_status": packet.get("verification_status", ""),
        "ready_to_continue": packet.get("ready_to_continue", False),
        "latest_summary": packet.get("latest_summary", ""),
        "next_action": packet.get("next_action", ""),
        "progress_summary": progress.get("summary", "") if isinstance(progress, dict) else "",
        "progress_path": progress.get("latest_written_path", "") if isinstance(progress, dict) else "",
        "session_package_id": session.get("package_id", "") if isinstance(session, dict) else "",
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _last_packet_ledger_fingerprint(path: Path) -> str:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            return str(row.get("state_fingerprint") or "")
    return ""


def _strings(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if str(item or "").strip()]
    text = str(value or "").strip()
    return [text] if text else []


def _unique_strings(values: list[str]) -> list[str]:
    return list(dict.fromkeys(item for item in values if str(item or "").strip()))


__all__ = [
    "SubagentContinuePacketRequest",
    "build_subagent_continue_packet",
    "subagent_restore_refs",
    "write_subagent_continue_packet",
]
