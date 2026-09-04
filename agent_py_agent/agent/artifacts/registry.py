
"""LLM: Maintain the append-only artifact registry below one canonical task workspace.

模块用途: 保存用户可见交付物的唯一账本；收口、看板和父代理都从这里解析产物，宿主读写时
通过 no-follow 文件接口防止 shell 把内部目录替换成链接后越界。
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..common.nofollow_fs import NoFollowPathError, append_text_beneath, read_text_beneath
from ..runtime_errors import runtime_error_report
from .registry_reader import latest_records_from_lines, lookup_record_with_errors

SCHEMA_VERSION = "artifact_registry.v1"
REGISTRY_RELATIVE_PATH = Path("data") / "artifacts" / "registry.jsonl"
ARTIFACT_ROLE_METADATA_KEY = "artifact_role"
ARTIFACT_ROLE_TOOL_OUTPUT_ARCHIVE = "tool_output_archive"
SHELL_PREIMAGE_POLICY_METADATA_KEY = "shell_preimage_policy"
SHELL_PREIMAGE_POLICY_EXCLUDE = "exclude"
SHELL_PREIMAGE_POLICY_INCLUDE = "include"


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

    return _workspace_root_path(workspace_root) / REGISTRY_RELATIVE_PATH


def register_artifact(request: ArtifactRegistration) -> ArtifactRegistryRecord:
    """Append a registry entry and return the current record."""

    root = _workspace_root_path(request.workspace_root)
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
    _append_record(root, record)
    return record


# LLM: Shell post-check and migrations already hold observed file facts. This append-only seam
# must persist those facts without resolving or opening the path again, because the path may have
# become a symlink or disappeared after an external process ran.
# 函数用途: 把调用方已经安全观测到的产物状态追加到账本，避免复核阶段再次跟随路径读取文件。
def register_observed_artifact(
    request: ArtifactRegistration,
    *,
    observed_sha256: str,
    observed_size_bytes: int,
) -> ArtifactRegistryRecord:
    """Append trusted observations without touching the referenced filesystem path."""

    root = _workspace_root_path(request.workspace_root)
    path = Path(request.path).expanduser()
    if not path.is_absolute():
        raise ValueError("observed artifact path must be absolute")
    record = ArtifactRegistryRecord(
        schema_version=SCHEMA_VERSION,
        artifact_id=str(request.artifact_id or "").strip(),
        run_id=str(request.run_id or ""),
        task_id=str(request.task_id or ""),
        agent_id=str(request.agent_id or ""),
        kind=str(request.kind or _kind_from_path(path)),
        mime_type=str(request.mime_type or ""),
        path=str(path),
        sha256=str(observed_sha256 or ""),
        size_bytes=max(0, int(observed_size_bytes)),
        status=str(request.status or "").strip(),
        source=str(request.source or ""),
        created_by_tool=str(request.created_by_tool or ""),
        updated_at=time.time(),
        metadata=dict(request.metadata or {}),
    )
    if not record.artifact_id or not record.status:
        raise ValueError("observed artifact requires artifact_id and status")
    _append_record(root, record)
    return record


def register_artifact_group(request: ArtifactGroupRegistration) -> ArtifactRegistryRecord:
    """Append a registry entry for a logical artifact group."""

    root = _workspace_root_path(request.workspace_root)
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
    _append_record(root, record)
    return record


def latest_artifact_records(workspace_root: str | Path) -> dict[str, ArtifactRegistryRecord]:
    """Read the registry and return latest record by artifact_id."""

    return latest_artifact_records_report(workspace_root).records


def latest_artifact_records_report(workspace_root: str | Path) -> ArtifactRegistryReadReport:
    """Read latest records and keep malformed rows as diagnostics."""

    root = _workspace_root_path(workspace_root)
    path = registry_path(root)
    try:
        text = read_text_beneath(root, tuple(REGISTRY_RELATIVE_PATH.parts))
    except (NoFollowPathError, OSError, UnicodeError) as exc:
        error = runtime_error_report(exc, context="artifact_registry.read")
        error["path"] = str(path)
        return ArtifactRegistryReadReport(errors=[error])
    if text is None:
        return ArtifactRegistryReadReport()
    latest, errors = latest_records_from_lines(
        path,
        text.splitlines(),
        _record_from_payload,
    )
    return ArtifactRegistryReadReport(records=latest, errors=errors)


# LLM: Crash recovery must detect an append that reached disk before the caller observed success.
# Scan every durable row because a later record for the same artifact may already be the latest.
# 函数用途: 判断指定 shell protection operation 的产物变更行是否已经完整落账。
def artifact_operation_record_exists(
    workspace_root: str | Path,
    *,
    artifact_id: str,
    operation_key: str,
    backup_ref: str,
) -> bool:
    return artifact_metadata_record_exists(
        workspace_root,
        artifact_id=artifact_id,
        required_metadata={
            "protection_operation_id": operation_key,
            "backup_ref": backup_ref,
        },
    )


# LLM: Durable recovery checks may need to match a historical row after a later append superseded
# it. Read every row and compare only explicit metadata keys supplied by host code.
# 函数用途: 扫描完整产物账本，确认某个 artifact_id 是否存在匹配指定结构化元数据的历史记录。
def artifact_metadata_record_exists(
    workspace_root: str | Path,
    *,
    artifact_id: str,
    required_metadata: dict[str, object],
) -> bool:
    root = _workspace_root_path(workspace_root)
    try:
        text = read_text_beneath(root, tuple(REGISTRY_RELATIVE_PATH.parts))
    except (NoFollowPathError, OSError, UnicodeError) as exc:
        raise OSError("artifact registry cannot be scanned safely") from exc
    if text is None:
        return False
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise OSError("artifact registry contains malformed rows") from exc
        if not isinstance(payload, dict):
            raise OSError("artifact registry contains invalid rows")
        metadata = payload.get("metadata")
        values = metadata if isinstance(metadata, dict) else {}
        if (
            str(payload.get("artifact_id") or "") == artifact_id
            and all(values.get(key) == value for key, value in required_metadata.items())
        ):
            return True
    return False


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


# LLM: Registry writes are anchored at the trusted task root and reject symlinked framework
# directories. Keep fsync semantics because recovery decisions consume this ledger after crashes.
# 函数用途: 安全地向任务产物账本追加一行并刷盘，不跟随模型可替换的目录链接。
def _append_record(workspace_root: Path, record: ArtifactRegistryRecord) -> None:
    line = json.dumps(record.to_dict(), ensure_ascii=False, sort_keys=True) + "\n"
    append_text_beneath(
        workspace_root,
        tuple(REGISTRY_RELATIVE_PATH.parts),
        line,
    )


# LLM: The workspace path is host-owned but its leaf may be replaced by shell. Normalize only
# lexical components here; no registry entry point may resolve and follow that replacement link.
# 函数用途: 返回不跟随符号链接的绝对工作区路径。
def _workspace_root_path(value: str | Path) -> Path:
    return Path(os.path.abspath(os.path.normpath(str(Path(value).expanduser()))))


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
