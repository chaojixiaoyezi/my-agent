
"""Resolve subagent compact/resume refs without writing parent memory.

Subagent resume needs to find the task-local run workspace and current compact
packet. This resolver stays bounded to the known workspace roots and returns refs only; it never creates files or promotes
subagent state into the main agent's long-term memory.
"""

from __future__ import annotations

"""read-only subagent owner reference resolver for compact resume."""

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .compact_resume.io import read_json_object
from .schema import (
    RuntimeMemorySchemaOptions,
    runtime_memory_schema_payload,
)

COMPACT_SUBAGENT_OWNER_SCHEMA = RuntimeMemorySchemaOptions("compact_subagent_owner_refs")
SUPPORTED_SUBAGENT_OWNER_TYPES = ("subagent_run", "subagent_session")


@dataclass(frozen=True)
class CompactSubagentOwnerRequest:
    workspace: Path
    owner_type: str
    owner_id: str
    resume_mode: str
    subagent_workspace: Path | None = None


def resolve_compact_subagent_owner(request: CompactSubagentOwnerRequest) -> dict[str, Any]:
    base = _base_payload(request)
    if request.owner_type not in SUPPORTED_SUBAGENT_OWNER_TYPES:
        return base | {"status": "not_subagent_owner", "refs": {}, "workspace_refs": []}
    if not request.owner_id:
        return base | {"status": "missing_owner_id", "refs": {}, "workspace_refs": []}
    refs = _owner_refs(request)
    status = _owner_status(refs)
    return base | {
        "status": status,
        "refs": refs,
        "workspace_refs": refs.get("agent_run_workspaces", []),
        "recommended_read_paths": _recommended_subagent_read_paths(refs),
        "continuation_hooks": _continuation_hooks(request, refs),
    }


def _base_payload(request: CompactSubagentOwnerRequest) -> dict[str, Any]:
    return {
        "version": COMPACT_SUBAGENT_OWNER_SCHEMA.version,
        "schema": runtime_memory_schema_payload(COMPACT_SUBAGENT_OWNER_SCHEMA),
        "supported_owner_types": list(SUPPORTED_SUBAGENT_OWNER_TYPES),
        "requested_owner_type": request.owner_type,
        "requested_owner_id": request.owner_id,
        "requested_resume_mode": request.resume_mode,
        "owner": {"owner_type": request.owner_type, "owner_id": request.owner_id},
        "memory_scope": "task_local",
        "writes_main_memory": False,
        "automatic_tool_execution": "none",
        "continuation_hooks": {},
    }


def _owner_refs(request: CompactSubagentOwnerRequest) -> dict[str, Any]:
    owner_id = _owner_path_segment(request.owner_id)
    if not owner_id:
        return {}
    run_workspaces = _run_workspaces_from_search_roots(request, owner_id)
    primary = Path(run_workspaces[0]) if run_workspaces else None
    refs: dict[str, Any] = {
        "agent_run_workspace": str(primary) if primary else "",
        "agent_run_workspaces": run_workspaces,
    }
    if primary:
        refs.update(_primary_run_refs(primary))
    return {key: value for key, value in refs.items() if value}


def _run_workspaces_from_search_roots(request: CompactSubagentOwnerRequest, owner_id: str) -> list[str]:
    return _unique_existing_dirs(
        path
        for root in _run_workspace_search_roots(request)
        for path in _agent_run_workspaces(root, owner_id)
    )


def _run_workspace_search_roots(request: CompactSubagentOwnerRequest) -> list[Path]:
    return _unique_paths([request.workspace, *_configured_subagent_workspace_roots(request)])


def _configured_subagent_workspace_roots(request: CompactSubagentOwnerRequest) -> list[Path]:
    return _unique_paths([request.subagent_workspace] if request.subagent_workspace else [])


def _agent_run_workspaces(workspace: Path, owner_id: str) -> list[str]:
    tasks_root = workspace / "tasks"
    if not tasks_root.exists():
        return []
    matches: list[str] = []
    for task_dir in sorted(path for path in tasks_root.iterdir() if path.is_dir()):
        matches.extend(str(path) for path in _agent_run_workspace_candidates(task_dir, owner_id) if path.is_dir())
    return matches


def _agent_run_workspace_candidates(task_dir: Path, owner_id: str) -> tuple[Path, Path]:
    return (task_dir / "work" / "agents" / owner_id,)


def _primary_run_refs(run_workspace: Path) -> dict[str, str]:
    candidates = {
        "agent_state": run_workspace / "state.json",
        "agent_checkpoint": run_workspace / "checkpoint.json",
        "agent_summary": run_workspace / "summary.md",
        "agent_task": run_workspace / "task.md",
        "agent_timeline": run_workspace / "timeline.jsonl",
        "agent_findings": run_workspace / "findings.jsonl",
        "agent_compactions": run_workspace / "compactions",
        "latest_continue_packet": run_workspace / "compactions" / "session" / "latest_continue_packet.json",
        "session_compact_ledger": run_workspace / "compactions" / "session" / "session_compact_ledger.jsonl",
        "agent_artifacts": run_workspace / "artifacts",
    }
    return {key: str(path) for key, path in candidates.items() if path.exists()}


def _recommended_subagent_read_paths(refs: dict[str, Any]) -> list[str]:
    ordered_keys = [
        "latest_continue_packet",
        "agent_checkpoint",
        "agent_summary",
        "agent_task",
        "agent_timeline",
        "agent_findings",
    ]
    return [str(refs[key]) for key in ordered_keys if refs.get(key)]


def _owner_path_segment(owner_id: str) -> str:
    if not owner_id or owner_id in {".", ".."}:
        return ""
    path = Path(owner_id)
    return owner_id if path.name == owner_id and len(path.parts) == 1 else ""


def _unique_paths(paths: Iterable[Path | None]) -> list[Path]:
    result: list[Path] = []
    seen: set[str] = set()
    for raw in paths:
        if raw is None:
            continue
        path = raw.expanduser()
        key = str(path.resolve()) if path.exists() else str(path)
        if key not in seen:
            seen.add(key)
            result.append(path)
    return result


def _unique_existing_dirs(paths: Iterable[str | Path]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in paths:
        path = Path(raw)
        if not path.is_dir():
            continue
        key = str(path.resolve())
        if key not in seen:
            seen.add(key)
            result.append(str(path))
    return result


def _path_is_under_any(path: Path, roots: Iterable[Path]) -> bool:
    resolved = path.resolve()
    for root in roots:
        try:
            resolved.relative_to(root.expanduser().resolve())
        except ValueError:
            continue
        return True
    return False


def _continuation_hooks(request: CompactSubagentOwnerRequest, refs: dict[str, Any]) -> dict[str, Any]:
    compactions = str(refs.get("agent_compactions", "") or "")
    continue_packet = str(refs.get("latest_continue_packet", "") or "")
    return {
        "enabled": bool(continue_packet),
        "owner_type": request.owner_type,
        "owner_id": request.owner_id,
        "run_compactions_dir": compactions,
        "session_compact_ledger": f"{compactions}/session/session_compact_ledger.jsonl" if compactions else "",
        "continue_packet_ref": continue_packet or (f"{compactions}/session/latest_continue_packet.json" if compactions else ""),
        "continue_packet_ready": bool(continue_packet),
        "writes_main_memory": False,
        "automatic_tool_execution": "none",
        "notes": [
            "task-local subagent session compact hook",
            "must not write main-agent long-term memory",
        ],
    }


def _owner_status(refs: dict[str, Any]) -> str:
    if refs.get("agent_run_workspace"):
        return "linked_run_workspace"
    return "owner_refs_not_found"


__all__ = [
    "CompactSubagentOwnerRequest",
    "resolve_compact_subagent_owner",
]
