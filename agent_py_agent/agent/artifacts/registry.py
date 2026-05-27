"""Run-local registry for user-visible artifacts.

Human version:
This is the single source of truth for deliverable files inside one run
workspace. Legacy payloads may still mention paths, but closeout, boards and
parents should first resolve those paths through this registry.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "artifact_registry.v1"
REGISTRY_RELATIVE_PATH = Path("data") / "artifacts" / "registry.jsonl"


@dataclass(frozen=True)
class ArtifactRegistration:
    """Inputs for registering one concrete artifact file."""

    workspace_root: Path
    path: str | Path
    artifact_id: str = ""
    run_id: str = ""
    task_id: str = ""
    agent_id: str = ""
    kind: str = ""
    mime_type: str = ""
    source: str = ""
    created_by_tool: str = ""
    status: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ArtifactRegistryRecord:
    """One latest-or-historical artifact registry entry."""

    schema_version: str
    artifact_id: str
    run_id: str
    task_id: str
    agent_id: str
    kind: str
    mime_type: str
    path: str
    sha256: str
    size_bytes: int
    status: str
    source: str
    created_by_tool: str
    updated_at: float
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def registry_path(workspace_root: str | Path) -> Path:
    """Return the canonical registry path for a run workspace."""

    return Path(workspace_root).expanduser().resolve(strict=False) / REGISTRY_RELATIVE_PATH


def register_artifact(request: ArtifactRegistration) -> ArtifactRegistryRecord:
    """Append a registry entry and return the current record."""

    root = Path(request.workspace_root).expanduser().resolve(strict=False)
    path = Path(request.path).expanduser()
    resolved = path.resolve(strict=False) if path.is_absolute() else (root / path).resolve(strict=False)
    record = ArtifactRegistryRecord(
        schema_version=SCHEMA_VERSION,
        artifact_id=_artifact_id(request, resolved),
        run_id=str(request.run_id or ""),
        task_id=str(request.task_id or ""),
        agent_id=str(request.agent_id or ""),
        kind=str(request.kind or _kind_from_path(resolved)),
        mime_type=str(request.mime_type or ""),
        path=str(resolved),
        sha256=_sha256_file(resolved),
        size_bytes=_size_bytes(resolved),
        status=_status_from_request(request, resolved),
        source=str(request.source or ""),
        created_by_tool=str(request.created_by_tool or ""),
        updated_at=time.time(),
        metadata=dict(request.metadata or {}),
    )
    _append_record(registry_path(root), record)
    return record


def latest_artifact_records(workspace_root: str | Path) -> dict[str, ArtifactRegistryRecord]:
    """Read the registry and return latest record by artifact_id."""

    latest: dict[str, ArtifactRegistryRecord] = {}
    path = registry_path(workspace_root)
    if not path.exists():
        return latest
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return latest
    for line in lines:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        record = _record_from_payload(payload)
        if record is not None and record.artifact_id:
            latest[record.artifact_id] = record
    return latest


def resolve_artifact_record(
    workspace_root: str | Path,
    artifact_id: str = "",
    *,
    path: str | Path = "",
) -> ArtifactRegistryRecord | None:
    """Resolve by id first, then by exact current path."""

    records = latest_artifact_records(workspace_root)
    key = str(artifact_id or "").strip()
    if key and key in records:
        return records[key]
    path_text = str(path or "").strip()
    if not path_text:
        return None
    try:
        resolved = Path(path_text).expanduser().resolve(strict=False)
    except OSError:
        return None
    for record in records.values():
        if Path(record.path).expanduser().resolve(strict=False) == resolved:
            return record
    return None


def _append_record(path: Path, record: ArtifactRegistryRecord) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record.to_dict(), ensure_ascii=False, sort_keys=True) + "\n"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())


def _record_from_payload(payload: object) -> ArtifactRegistryRecord | None:
    if not isinstance(payload, dict):
        return None
    try:
        return ArtifactRegistryRecord(
            schema_version=str(payload.get("schema_version") or ""),
            artifact_id=str(payload.get("artifact_id") or ""),
            run_id=str(payload.get("run_id") or ""),
            task_id=str(payload.get("task_id") or ""),
            agent_id=str(payload.get("agent_id") or ""),
            kind=str(payload.get("kind") or ""),
            mime_type=str(payload.get("mime_type") or ""),
            path=str(payload.get("path") or ""),
            sha256=str(payload.get("sha256") or ""),
            size_bytes=int(payload.get("size_bytes") or 0),
            status=str(payload.get("status") or ""),
            source=str(payload.get("source") or ""),
            created_by_tool=str(payload.get("created_by_tool") or ""),
            updated_at=float(payload.get("updated_at") or 0.0),
            metadata=dict(payload.get("metadata") or {}),
        )
    except (TypeError, ValueError):
        return None


def _artifact_id(request: ArtifactRegistration, path: Path) -> str:
    explicit = str(request.artifact_id or "").strip()
    if explicit:
        return explicit
    seed = "|".join(
        [
            str(request.run_id or ""),
            str(request.task_id or ""),
            str(request.agent_id or ""),
            str(path),
        ]
    )
    return f"art_{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:20]}"


def _kind_from_path(path: Path) -> str:
    suffix = path.suffix.lower().lstrip(".")
    return suffix or "artifact"


def _status_from_request(request: ArtifactRegistration, path: Path) -> str:
    explicit = str(request.status or "").strip()
    if explicit:
        return explicit
    return "ready" if path.is_file() else "missing"


def _sha256_file(path: Path) -> str:
    if not path.is_file():
        return ""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _size_bytes(path: Path) -> int:
    try:
        return path.stat().st_size if path.is_file() else 0
    except OSError:
        return 0
