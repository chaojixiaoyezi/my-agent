from __future__ import annotations

"""LLM: normalized artifact manifests for runtime memory.

Human version:
Artifact manifests turn arbitrary `artifact_refs` into small records with
summary, hash, path, and existence metadata. The manifest stores references and
checksums only; large output bodies stay in artifact files.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ArtifactManifestResult:
    """Task/run manifest paths produced while syncing artifacts."""

    task_manifest_jsonl: Path
    agent_manifest_jsonl: Path


@dataclass(frozen=True)
class SyncArtifactManifestsRequest:
    """Bundle inputs for syncing task/run artifact manifests."""

    # LLM: artifact sync stays reference-only, so future fields belong on this explicit request.
    task: Any
    task_workspace_root: Path
    agent_run_workspace_root: Path
    now: float


@dataclass(frozen=True)
class _ArtifactRecordContext:
    task_id: str
    run_id: str
    now: float


def sync_artifact_manifests(
    request: SyncArtifactManifestsRequest | Any = None,
    *,
    task: Any | None = None,
    task_workspace_root: Path | None = None,
    agent_run_workspace_root: Path | None = None,
    now: float | None = None,
) -> ArtifactManifestResult:
    """Write task-level and run-level artifact manifests from task artifact refs."""

    inputs = _coerce_sync_request(
        request,
        task=task,
        task_workspace_root=task_workspace_root,
        agent_run_workspace_root=agent_run_workspace_root,
        now=now,
    )
    records = _artifact_records(inputs.task, inputs.now)
    task_manifest = inputs.task_workspace_root / "artifacts" / "manifest.jsonl"
    agent_manifest = inputs.agent_run_workspace_root / "artifacts" / "manifest.jsonl"
    _write_manifest(task_manifest, records)
    _write_manifest(agent_manifest, records)
    return ArtifactManifestResult(task_manifest_jsonl=task_manifest, agent_manifest_jsonl=agent_manifest)


def _coerce_sync_request(
    request: SyncArtifactManifestsRequest | Any,
    *,
    task: Any | None,
    task_workspace_root: Path | None,
    agent_run_workspace_root: Path | None,
    now: float | None,
) -> SyncArtifactManifestsRequest:
    if isinstance(request, SyncArtifactManifestsRequest):
        return request
    resolved_task = request if request is not None else task
    if (
        resolved_task is None
        or task_workspace_root is None
        or agent_run_workspace_root is None
        or now is None
    ):
        raise TypeError(
            "sync_artifact_manifests requires task, task_workspace_root, "
            "agent_run_workspace_root, and now"
        )
    return SyncArtifactManifestsRequest(
        task=resolved_task,
        task_workspace_root=Path(task_workspace_root),
        agent_run_workspace_root=Path(agent_run_workspace_root),
        now=now,
    )


def _artifact_records(task: Any, now: float) -> list[dict[str, object]]:
    task_id = str(getattr(task, "root_id", "") or getattr(task, "id", "task"))
    run_id = str(getattr(task, "id", "") or task_id)
    task_dir = Path(str(getattr(task, "task_dir", ""))) if getattr(task, "task_dir", "") else None
    context = _ArtifactRecordContext(task_id=task_id, run_id=run_id, now=now)
    records: list[dict[str, object]] = []
    for index, ref in enumerate(_artifact_refs(task), start=1):
        resolved = _resolve_ref(ref, task_dir)
        records.append(_artifact_record(context, index, ref, resolved))
    return records


def _artifact_record(
    context: _ArtifactRecordContext,
    index: int,
    ref: str,
    resolved: Path | None,
) -> dict[str, object]:
    exists = bool(resolved and resolved.is_file())
    size_bytes = resolved.stat().st_size if exists and resolved else 0
    digest = _sha256_file(resolved) if exists and resolved else ""
    return {
        "version": 1,
        "artifact_id": _artifact_id(context.run_id, index, ref),
        "task_id": context.task_id,
        "run_id": context.run_id,
        "ref": ref,
        "path": str(resolved) if resolved else ref,
        "kind": _artifact_kind(ref),
        "exists": exists,
        "size_bytes": size_bytes,
        "sha256": digest,
        "summary": _summary(ref, exists, size_bytes),
        "content_externalized": True,
        "source": "subagent_artifact_refs",
        "created_at": _utc_iso(context.now),
    }


def _artifact_refs(task: Any) -> list[str]:
    refs: list[str] = []
    for item in list(getattr(task, "artifact_refs", []) or []):
        text = str(item).strip()
        if text and text not in refs:
            refs.append(text)
    return refs


def _resolve_ref(ref: str, task_dir: Path | None) -> Path | None:
    path = Path(ref).expanduser()
    if path.is_absolute():
        return path
    return (task_dir / path) if task_dir else path


def _write_manifest(path: Path, records: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = "\n".join(json.dumps(record, ensure_ascii=False, sort_keys=True) for record in records)
    path.write_text((content + "\n") if content else "", encoding="utf-8")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _artifact_id(run_id: str, index: int, ref: str) -> str:
    digest = hashlib.sha1(ref.encode("utf-8")).hexdigest()[:12]
    return f"artifact-{_safe_segment(run_id)}-{index}-{digest}"


def _artifact_kind(ref: str) -> str:
    suffix = Path(ref).suffix.lower()
    if suffix in {".json", ".md", ".txt", ".log"}:
        return "report"
    if suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
        return "image"
    return "artifact"


def _summary(ref: str, exists: bool, size_bytes: int) -> str:
    if exists:
        return f"{Path(ref).name or ref} ({size_bytes} bytes)"
    return f"unresolved artifact ref: {ref}"


def _utc_iso(value: float) -> str:
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()


def _safe_segment(value: str) -> str:
    return str(value or "item").replace("/", "_").replace("\\", "_").strip() or "item"


__all__ = [
    "ArtifactManifestResult",
    "SyncArtifactManifestsRequest",
    "sync_artifact_manifests",
]
