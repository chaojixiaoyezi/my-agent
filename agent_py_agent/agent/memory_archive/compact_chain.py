
from __future__ import annotations

"""checkpoint-first compact chain records for agent run workspaces.

Human version:
This module writes a conservative compact chain for a subagent run. It records
checkpoint snapshots and compact metadata without deleting timelines, artifacts,
or legacy work-order files.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..common.json_io import read_json_object, write_json_object
from ..common.path_segments import safe_path_segment


@dataclass(frozen=True)
class CompactChainResult:
    """Paths for the latest run-local compact checkpoint chain."""

    ledger_jsonl: Path
    latest_summary_md: Path
    latest_metadata_json: Path


@dataclass(frozen=True)
class SyncAgentRunCompactChainRequest:
    """Bundle inputs for appending one agent-run compact checkpoint."""

    task: Any
    agent_run_workspace_root: Path
    artifact_manifest_jsonl: Path
    now: float


@dataclass(frozen=True)
class _CompactSnapshotContext:
    event_id: str
    sequence: int
    previous_event_id: str
    agent_run_workspace_root: Path
    artifact_manifest_jsonl: Path
    summary_md: Path
    metadata_json: Path
    now: float
    state_fingerprint: str


def sync_agent_run_compact_chain(
    request: SyncAgentRunCompactChainRequest | Any = None,
    *,
    task: Any | None = None,
    agent_run_workspace_root: Path | None = None,
    artifact_manifest_jsonl: Path | None = None,
    now: float | None = None,
) -> CompactChainResult:
    """Append a checkpoint snapshot event and update latest compact refs."""

    inputs = _coerce_sync_request(
        request,
        task=task,
        agent_run_workspace_root=agent_run_workspace_root,
        artifact_manifest_jsonl=artifact_manifest_jsonl,
        now=now,
    )
    paths = _compact_paths(inputs.agent_run_workspace_root)
    paths.ledger_jsonl.parent.mkdir(parents=True, exist_ok=True)
    state_fingerprint = _state_fingerprint(inputs.task, inputs.artifact_manifest_jsonl)
    if _latest_state_fingerprint(paths.latest_metadata_json) == state_fingerprint:
        return paths
    sequence = _next_sequence(paths.ledger_jsonl)
    previous_event_id = _last_event_id(paths.ledger_jsonl)
    event_id = _event_id(str(getattr(inputs.task, "id", "")), sequence)
    event_summary = paths.ledger_jsonl.parent / f"{event_id}.md"
    event_metadata = paths.ledger_jsonl.parent / f"{event_id}.json"
    context = _CompactSnapshotContext(
        event_id=event_id,
        sequence=sequence,
        previous_event_id=previous_event_id,
        agent_run_workspace_root=inputs.agent_run_workspace_root,
        artifact_manifest_jsonl=inputs.artifact_manifest_jsonl,
        summary_md=event_summary,
        metadata_json=event_metadata,
        now=inputs.now,
        state_fingerprint=state_fingerprint,
    )
    metadata = _metadata_payload(inputs.task, context)
    summary = _summary_markdown(inputs.task, metadata)
    _write_markdown(event_summary, summary)
    write_json_object(event_metadata, metadata, sort_keys=False)
    _write_markdown(paths.latest_summary_md, summary)
    write_json_object(paths.latest_metadata_json, metadata, sort_keys=False)
    _append_ledger(paths.ledger_jsonl, metadata)
    _merge_checkpoint(inputs.agent_run_workspace_root / "checkpoint.json", metadata)
    return paths


def _coerce_sync_request(
    request: SyncAgentRunCompactChainRequest | Any,
    *,
    task: Any | None,
    agent_run_workspace_root: Path | None,
    artifact_manifest_jsonl: Path | None,
    now: float | None,
) -> SyncAgentRunCompactChainRequest:
    if isinstance(request, SyncAgentRunCompactChainRequest):
        return request
    resolved_task = request if request is not None else task
    if (
        resolved_task is None
        or agent_run_workspace_root is None
        or artifact_manifest_jsonl is None
        or now is None
    ):
        raise TypeError(
            "sync_agent_run_compact_chain requires task, agent_run_workspace_root, "
            "artifact_manifest_jsonl, and now"
        )
    return SyncAgentRunCompactChainRequest(
        task=resolved_task,
        agent_run_workspace_root=Path(agent_run_workspace_root),
        artifact_manifest_jsonl=Path(artifact_manifest_jsonl),
        now=now,
    )


def default_compact_chain_result(agent_run_workspace_root: Path) -> CompactChainResult:
    """Return compact-chain paths without writing them."""

    return _compact_paths(agent_run_workspace_root)


def _compact_paths(agent_run_workspace_root: Path) -> CompactChainResult:
    compactions_dir = agent_run_workspace_root / "compactions"
    return CompactChainResult(
        ledger_jsonl=compactions_dir / "compaction_ledger.jsonl",
        latest_summary_md=compactions_dir / "latest_summary.md",
        latest_metadata_json=compactions_dir / "latest_metadata.json",
    )


def _metadata_payload(
    task: Any,
    context: _CompactSnapshotContext,
) -> dict[str, object]:
    task_id = str(getattr(task, "root_id", "") or getattr(task, "id", "task"))
    run_id = str(getattr(task, "id", "") or task_id)
    return {
        "version": 1,
        "event_id": context.event_id,
        "event_type": "checkpoint_snapshot",
        "compact_status": "checkpoint_only",
        "sequence": context.sequence,
        "previous_event_id": context.previous_event_id,
        "task_id": task_id,
        "run_id": run_id,
        "status": str(getattr(task, "status", "")),
        "progress": float(getattr(task, "progress", 0.0) or 0.0),
        "current_step": str(getattr(task, "current_step", "")),
        "summary": str(getattr(task, "latest_summary", "")),
        "refs": _refs(task, context),
        "created_at": _utc_iso(context.now),
        "state_fingerprint": context.state_fingerprint,
    }


def _refs(
    task: Any,
    context: _CompactSnapshotContext,
) -> dict[str, str]:
    root = context.agent_run_workspace_root
    return {
        "agent_run_workspace": str(root),
        "checkpoint": str(root / "checkpoint.json"),
        "summary": str(context.summary_md),
        "metadata": str(context.metadata_json),
        "compaction_ledger": str(root / "compactions" / "compaction_ledger.jsonl"),
        "artifact_manifest": str(context.artifact_manifest_jsonl),
        "timeline": str(root / "timeline.jsonl"),
        "legacy_checkpoint": str(getattr(task, "checkpoint_json", "")),
        "legacy_task_dir": str(getattr(task, "task_dir", "")),
    }


def _summary_markdown(task: Any, metadata: dict[str, object]) -> str:
    refs = metadata.get("refs") if isinstance(metadata.get("refs"), dict) else {}
    blockers = "\n".join(f"- {item}" for item in list(getattr(task, "blockers", []) or [])) or "- 暂无"
    artifact_refs = "\n".join(f"- {item}" for item in list(getattr(task, "artifact_refs", []) or [])) or "- 暂无"
    return (
        "# Compact Checkpoint Summary\n\n"
        f"- event_id: {metadata['event_id']}\n"
        f"- compact_status: {metadata['compact_status']}\n"
        f"- task_id: {metadata['task_id']}\n"
        f"- run_id: {metadata['run_id']}\n"
        f"- status: {metadata['status']}\n"
        f"- progress: {metadata['progress']}\n"
        f"- checkpoint: {refs.get('checkpoint', '')}\n"
        f"- compaction_ledger: {refs.get('compaction_ledger', '')}\n\n"
        "## Latest Summary\n\n"
        f"{metadata.get('summary') or '暂无'}\n\n"
        "## Blockers\n\n"
        f"{blockers}\n\n"
        "## Artifact Refs\n\n"
        f"{artifact_refs}\n\n"
        "## Next Recovery Step\n\n"
        "Read checkpoint.json first, then verify against timeline.jsonl, findings.jsonl, and artifact manifests.\n"
    )


def _merge_checkpoint(path: Path, metadata: dict[str, object]) -> None:
    checkpoint = read_json_object(path)
    refs = metadata.get("refs") if isinstance(metadata.get("refs"), dict) else {}
    checkpoint["compact_chain"] = {
        "status": metadata.get("compact_status", ""),
        "last_event_id": metadata.get("event_id", ""),
        "ledger_ref": refs.get("compaction_ledger", ""),
        "summary_ref": refs.get("summary", ""),
        "metadata_ref": refs.get("metadata", ""),
        "artifact_manifest_ref": refs.get("artifact_manifest", ""),
        "timeline_ref": refs.get("timeline", ""),
        "content_preserved": True,
    }
    write_json_object(path, checkpoint, sort_keys=False)


def _next_sequence(path: Path) -> int:
    return len(_ledger_lines(path)) + 1


def _last_event_id(path: Path) -> str:
    for line in reversed(_ledger_lines(path)):
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        event_id = str(payload.get("event_id") or "")
        if event_id:
            return event_id
    return ""


def _latest_state_fingerprint(path: Path) -> str:
    return str(read_json_object(path).get("state_fingerprint") or "")


def _state_fingerprint(task: Any, artifact_manifest_jsonl: Path) -> str:
    payload = {
        "status": str(getattr(task, "status", "")),
        "verification_status": str(getattr(task, "verification_status", "")),
        "progress": float(getattr(task, "progress", 0.0) or 0.0),
        "current_step": str(getattr(task, "current_step", "")),
        "latest_summary": str(getattr(task, "latest_summary", "")),
        "blockers": [str(item) for item in list(getattr(task, "blockers", []) or [])],
        "artifact_refs": [str(item) for item in list(getattr(task, "artifact_refs", []) or [])],
        "manifest_sha256": _file_sha256(artifact_manifest_jsonl),
    }
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def _ledger_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _append_ledger(path: Path, payload: dict[str, object]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def _write_markdown(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _event_id(run_id: str, sequence: int) -> str:
    return f"compact-{safe_path_segment(run_id, default='run', replacement='_')}-{sequence:04d}"


def _utc_iso(value: float) -> str:
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()


__all__ = [
    "CompactChainResult",
    "SyncAgentRunCompactChainRequest",
    "default_compact_chain_result",
    "sync_agent_run_compact_chain",
]
