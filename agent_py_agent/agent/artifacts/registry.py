
"""Run-local registry for user-visible artifacts.

Human version:
This is the single source of truth for deliverable files inside one run
workspace. Closeout, boards, and parents resolve deliverables through this
registry.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .registry_reader import latest_records_with_errors, lookup_record_with_errors

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
class ArtifactGroupRegistration:
    """Inputs for registering one logical deliverable made of multiple files."""

    workspace_root: Path
    paths: list[str | Path]
    artifact_id: str
    run_id: str = ""
    task_id: str = ""
    agent_id: str = ""
    kind: str = "group"
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


@dataclass(frozen=True)
class ArtifactRegistryReadReport:
    """Latest registry records plus non-fatal read/parse diagnostics."""

    records: dict[str, ArtifactRegistryRecord] = field(default_factory=dict)
    errors: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class ArtifactRegistryLookupReport:
    """Registry lookup result for one artifact id or path."""

    record: ArtifactRegistryRecord | None = None
    errors: list[dict[str, Any]] = field(default_factory=list)


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


def register_artifact_group(request: ArtifactGroupRegistration) -> ArtifactRegistryRecord:
    """Append a registry entry for a logical artifact group."""

    root = Path(request.workspace_root).expanduser().resolve(strict=False)
    member_paths = [_resolve_member_path(root, path) for path in request.paths]
    common_path = _group_common_path(root, member_paths)
    member_rows = [_member_metadata(root, path) for path in member_paths]
    status = str(request.status or "").strip() or ("ready" if all(Path(row["path"]).is_file() for row in member_rows) else "missing")
    metadata = {
        **dict(request.metadata or {}),
        "artifact_type": "file_group",
        "members": member_rows,
    }
    record = ArtifactRegistryRecord(
        schema_version=SCHEMA_VERSION,
        artifact_id=str(request.artifact_id or "").strip() or _group_artifact_id(request, member_paths),
        run_id=str(request.run_id or ""),
        task_id=str(request.task_id or ""),
        agent_id=str(request.agent_id or ""),
        kind=str(request.kind or "group"),
        mime_type="",
        path=str(common_path),
        sha256=_sha256_group(member_rows),
        size_bytes=sum(int(row.get("size_bytes") or 0) for row in member_rows),
        status=status,
        source=str(request.source or ""),
        created_by_tool=str(request.created_by_tool or ""),
        updated_at=time.time(),
        metadata=metadata,
    )
    _append_record(registry_path(root), record)
    return record


def latest_artifact_records(workspace_root: str | Path) -> dict[str, ArtifactRegistryRecord]:
    """Read the registry and return latest record by artifact_id."""

    return latest_artifact_records_report(workspace_root).records


def latest_artifact_records_report(workspace_root: str | Path) -> ArtifactRegistryReadReport:
    """Read latest records and keep malformed rows as diagnostics."""

    latest, errors = latest_records_with_errors(registry_path(workspace_root), _record_from_payload)
    return ArtifactRegistryReadReport(records=latest, errors=errors)


def resolve_artifact_record(
    workspace_root: str | Path,
    artifact_id: str = "",
    *,
    path: str | Path = "",
) -> ArtifactRegistryRecord | None:
    """Resolve by id first, then by exact current path."""

    return resolve_artifact_record_report(workspace_root, artifact_id, path=path).record


def resolve_artifact_record_report(
    workspace_root: str | Path,
    artifact_id: str = "",
    *,
    path: str | Path = "",
) -> ArtifactRegistryLookupReport:
    """Resolve a registry record and preserve registry read diagnostics."""

    report = latest_artifact_records_report(workspace_root)
    record, errors = lookup_record_with_errors(report.records, report.errors, artifact_id, path=path)
    return ArtifactRegistryLookupReport(record=record, errors=errors)


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


def _group_artifact_id(request: ArtifactGroupRegistration, paths: list[Path]) -> str:
    seed = "|".join([str(request.run_id or ""), str(request.task_id or ""), *[str(path) for path in paths]])
    return f"artgrp_{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:20]}"


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


def _resolve_member_path(root: Path, path: str | Path) -> Path:
    candidate = Path(path).expanduser()
    return candidate.resolve(strict=False) if candidate.is_absolute() else (root / candidate).resolve(strict=False)


def _group_common_path(root: Path, paths: list[Path]) -> Path:
    if not paths:
        return root
    try:
        common = Path(os.path.commonpath([str(path) for path in paths]))
    except ValueError:
        return root
    return common if common.is_dir() else common.parent


def _member_metadata(root: Path, path: Path) -> dict[str, Any]:
    return {
        "path": str(path),
        "relative_path": _relative_path(root, path),
        "kind": _kind_from_path(path),
        "sha256": _sha256_file(path),
        "size_bytes": _size_bytes(path),
        "exists": path.is_file(),
    }


def _relative_path(root: Path, path: Path) -> str:
    try:
        return str(path.relative_to(root)).replace("\\", "/")
    except ValueError:
        return str(path)


def _sha256_group(member_rows: list[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    for row in sorted(member_rows, key=lambda item: str(item.get("relative_path") or item.get("path") or "")):
        digest.update(str(row.get("relative_path") or row.get("path") or "").encode("utf-8"))
        digest.update(str(row.get("sha256") or "").encode("utf-8"))
        digest.update(str(row.get("size_bytes") or 0).encode("utf-8"))
    return digest.hexdigest()
