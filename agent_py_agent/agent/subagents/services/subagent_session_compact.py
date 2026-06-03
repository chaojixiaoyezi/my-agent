
from __future__ import annotations

"""Task-local session compact package writer for subagent runners."""

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...common.value_parsing import sequence_strings
from ..models import SubAgentTask
from .compact_continue_packet import (
    SubagentContinuePacketRequest,
    subagent_restore_refs,
    write_subagent_continue_packet,
)

_SCHEMA_VERSION = "subagent_session_compact.v1"


@dataclass(frozen=True)
class SubagentSessionCompactRequest:
    task: SubAgentTask
    compact_payload: dict[str, object]
    output_payload: dict[str, object]


def write_subagent_session_compact(request: SubagentSessionCompactRequest) -> dict[str, str]:
    if not _should_write(request.compact_payload):
        return {}
    compactions = _compactions_dir(request.task)
    if not compactions:
        return {}
    compactions.mkdir(parents=True, exist_ok=True)
    session_dir = compactions / "session"
    session_dir.mkdir(parents=True, exist_ok=True)
    package_dir = session_dir / "packages" / _package_id(request.task)
    package_dir.mkdir(parents=True, exist_ok=True)
    refs = _package_refs(session_dir, package_dir)
    metadata = _metadata_payload(request, refs)
    refs["metadata"].write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    refs["restore_refs"].write_text(json.dumps(metadata["restore_refs"], ensure_ascii=False, indent=2), encoding="utf-8")
    refs["summary"].write_text(_summary_text(metadata), encoding="utf-8")
    _write_latest_refs(request.task, refs)
    _append_session_ledger(session_dir / "session_compact_ledger.jsonl", metadata, refs)
    write_subagent_continue_packet(SubagentContinuePacketRequest(request.task, request.output_payload))
    return {"metadata_ref": str(refs["latest_metadata"]), "summary_ref": str(refs["latest_summary"])}


def _should_write(payload: dict[str, object]) -> bool:
    if not isinstance(payload, dict) or not payload:
        return False
    if bool(payload.get("suggested")):
        return True
    status = str(payload.get("auto_status") or payload.get("status") or "").strip()
    return status not in {"", "ok", "skipped_below_threshold", "skipped_after_guarded_continuation"}


def _compactions_dir(task: SubAgentTask) -> Path | None:
    value = str(getattr(task, "agent_run_compactions_dir", "") or "").strip()
    return Path(value) if value else None


def _package_id(task: SubAgentTask) -> str:
    return f"session-compact-{time.time_ns()}-{_safe_id(task.id)}"


def _package_refs(session_dir: Path, package_dir: Path) -> dict[str, Path]:
    return {
        "metadata": package_dir / "metadata.json",
        "summary": package_dir / "summary.md",
        "restore_refs": package_dir / "restore_refs.json",
        "latest_metadata": session_dir / "latest_metadata.json",
        "latest_summary": session_dir / "latest_summary.md",
    }


def _metadata_payload(request: SubagentSessionCompactRequest, refs: dict[str, Path]) -> dict[str, Any]:
    task = request.task
    compact = request.compact_payload
    restore_refs = subagent_restore_refs(task)
    return {
        "schema_version": _SCHEMA_VERSION,
        "kind": "subagent_session_compact_package",
        "package_id": refs["metadata"].parent.name,
        "created_at": time.time(),
        "owner": {"owner_type": "subagent_run", "owner_id": task.id},
        "memory_scope": "task_local",
        "writes_main_memory": False,
        "automatic_tool_execution": "none",
        "status": task.status,
        "verification_status": task.verification_status,
        "current_step": task.current_step or task.status,
        "latest_summary": task.latest_summary,
        "next_action": _next_action(task, request.output_payload),
        "source": _source_payload(compact),
        "token_budget": _dict_payload(compact.get("token_budget")),
        "restore_refs": restore_refs,
        "package_refs": {key: str(value) for key, value in refs.items()},
        "reserved": {},
    }


def _source_payload(compact: dict[str, object]) -> dict[str, object]:
    return {
        "suggested": bool(compact.get("suggested")),
        "status": str(compact.get("status") or ""),
        "auto_status": str(compact.get("auto_status") or ""),
        "ratio": _float_value(compact.get("ratio")),
        "message": str(compact.get("message") or ""),
        "commands": sequence_strings(compact.get("commands"), allow_scalar=True),
    }


def _summary_text(metadata: dict[str, Any]) -> str:
    refs = metadata.get("restore_refs", {}) if isinstance(metadata.get("restore_refs"), dict) else {}
    lines = [
        "# Subagent Session Compact",
        "",
        f"- schema_version: {metadata.get('schema_version', '')}",
        f"- run_id: {metadata.get('owner', {}).get('owner_id', '')}",
        f"- memory_scope: {metadata.get('memory_scope', '')}",
        f"- writes_main_memory: {str(metadata.get('writes_main_memory', False)).lower()}",
        f"- status: {metadata.get('status', '')}",
        f"- current_step: {metadata.get('current_step', '')}",
        f"- next_action: {metadata.get('next_action', '')}",
        "",
        "## Restore Refs",
    ]
    lines.extend(f"- {key}: {value}" for key, value in refs.items())
    return "\n".join(lines).rstrip() + "\n"


def _write_latest_refs(task: SubAgentTask, refs: dict[str, Path]) -> None:
    refs["latest_metadata"].write_text(refs["metadata"].read_text(encoding="utf-8"), encoding="utf-8")
    refs["latest_summary"].write_text(refs["summary"].read_text(encoding="utf-8"), encoding="utf-8")
    session_dir = refs["latest_metadata"].parent
    task.agent_run_session_compaction_ledger_jsonl = str(session_dir / "session_compact_ledger.jsonl")
    task.agent_run_latest_session_compaction_metadata_json = str(refs["latest_metadata"])
    task.agent_run_latest_session_compaction_summary_md = str(refs["latest_summary"])


def _append_session_ledger(path: Path, metadata: dict[str, Any], refs: dict[str, Path]) -> None:
    row = {
        "schema_version": "subagent_session_compact_ledger.v1",
        "event_type": "subagent_session_compact",
        "run_id": metadata.get("owner", {}).get("owner_id", ""),
        "package_id": metadata.get("package_id", ""),
        "metadata_ref": str(refs["latest_metadata"]),
        "summary_ref": str(refs["latest_summary"]),
        "memory_scope": "task_local",
        "writes_main_memory": False,
        "created_at": metadata.get("created_at", 0.0),
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def _next_action(task: SubAgentTask, output_payload: dict[str, object]) -> str:
    for item in [*sequence_strings(output_payload.get("next_actions"), allow_scalar=True), output_payload.get("next_action"), task.current_step]:
        text = str(item or "").strip()
        if text:
            return text
    return "resume subagent runner from task-local compact package"


def _dict_payload(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _float_value(value: object) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _safe_id(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in str(value or "run"))
    return cleaned[:48] or "run"


__all__ = ["SubagentSessionCompactRequest", "write_subagent_session_compact"]
