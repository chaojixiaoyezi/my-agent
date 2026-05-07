from __future__ import annotations

"""LLM: checkpoint-first compact chain records for agent run workspaces.

Human version:
This module writes a conservative compact chain for a subagent run. It records
checkpoint snapshots and compact metadata without deleting timelines, artifacts,
or legacy work-order files.
"""

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class CompactChainResult:
    """Paths for the latest run-local compact checkpoint chain."""

    ledger_jsonl: Path
    latest_summary_md: Path
    latest_metadata_json: Path


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


def sync_agent_run_compact_chain(
    task: Any,
    *,
    agent_run_workspace_root: Path,
    artifact_manifest_jsonl: Path,
    now: float,
) -> CompactChainResult:
    """Append a checkpoint snapshot event and update latest compact refs."""

    paths = _compact_paths(agent_run_workspace_root)
    paths.ledger_jsonl.parent.mkdir(parents=True, exist_ok=True)
    sequence = _next_sequence(paths.ledger_jsonl)
    previous_event_id = _last_event_id(paths.ledger_jsonl)
    event_id = _event_id(str(getattr(task, "id", "")), sequence)
    event_summary = paths.ledger_jsonl.parent / f"{event_id}.md"
    event_metadata = paths.ledger_jsonl.parent / f"{event_id}.json"
    context = _CompactSnapshotContext(
        event_id=event_id,
        sequence=sequence,
        previous_event_id=previous_event_id,
        agent_run_workspace_root=agent_run_workspace_root,
        artifact_manifest_jsonl=artifact_manifest_jsonl,
        summary_md=event_summary,
        metadata_json=event_metadata,
        now=now,
    )
    metadata = _metadata_payload(task, context)
    summary = _summary_markdown(task, metadata)
    _write_markdown(event_summary, summary)
    _write_json(event_metadata, metadata)
    _write_markdown(paths.latest_summary_md, summary)
    _write_json(paths.latest_metadata_json, metadata)
    _append_ledger(paths.ledger_jsonl, metadata)
    _merge_checkpoint(agent_run_workspace_root / "checkpoint.json", metadata)
    return paths


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
    checkpoint = _read_json_object(path)
    refs = metadata.get("refs") if isinstance(metadata.get("refs"), dict) else {}
    # LLM: compact refs are additive recovery pointers; they never replace the original checkpoint facts.
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
    _write_json(path, checkpoint)


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


def _ledger_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _append_ledger(path: Path, payload: dict[str, object]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def _read_json_object(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_markdown(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _event_id(run_id: str, sequence: int) -> str:
    return f"compact-{_safe_segment(run_id)}-{sequence:04d}"


def _safe_segment(value: str) -> str:
    return str(value or "run").replace("/", "_").replace("\\", "_").strip() or "run"


def _utc_iso(value: float) -> str:
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()


__all__ = ["CompactChainResult", "default_compact_chain_result", "sync_agent_run_compact_chain"]
