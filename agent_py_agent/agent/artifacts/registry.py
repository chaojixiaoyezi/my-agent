# LLM: Artifact registry records user-visible deliverables as machine facts.
# 模块用途: 统一登记任务产物的路径、hash、状态和来源，让父代理、tree 和 closeout 只认一套账本。

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


# LLM: ArtifactRegistration is the input contract for one artifact write.
# 类用途: 保存登记产物所需的 workspace、路径、身份、格式和来源字段。
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


# LLM: ArtifactRegistryRecord is the persisted artifact ledger entry.
# 类用途: 保存一次产物登记结果，包括 artifact_id、路径、hash、大小和状态。
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

    # LLM: to_dict serializes registry records for JSONL persistence.
    # 函数用途: 将 dataclass 记录转成普通 dict，供 registry.jsonl 写入。
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# LLM: registry_path centralizes the on-disk artifact ledger location.
# 函数用途: 返回当前 workspace 的统一产物登记文件路径。
def registry_path(workspace_root: str | Path) -> Path:
    """Return the canonical registry path for a run workspace."""

    return Path(workspace_root).expanduser().resolve(strict=False) / REGISTRY_RELATIVE_PATH


# LLM: register_artifact appends a new current artifact fact.
# 函数用途: 计算产物 hash/大小/状态并写入 registry.jsonl。
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


# LLM: latest_artifact_records reads current artifact facts by artifact_id.
# 函数用途: 扫描 registry.jsonl，返回每个 artifact_id 的最新记录。
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


# LLM: resolve_artifact_record resolves model-visible ids or paths against the registry.
# 函数用途: 优先按 artifact_id 查找，必要时用精确路径匹配登记记录。
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


# LLM: _append_record writes one registry record durably.
# 函数用途: 追加 JSONL 并 fsync，减少 shell/进程中断造成的登记丢失。
def _append_record(path: Path, record: ArtifactRegistryRecord) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record.to_dict(), ensure_ascii=False, sort_keys=True) + "\n"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())


# LLM: _record_from_payload tolerates old or corrupt registry lines.
# 函数用途: 将 JSON 对象还原成 ArtifactRegistryRecord，坏记录返回 None。
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


# LLM: _artifact_id creates a stable fallback id when the caller omits one.
# 函数用途: 用 run/task/agent/path 派生短 artifact_id，避免只靠自然语言路径。
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


# LLM: _kind_from_path keeps unknown formats open-world.
# 函数用途: 从文件后缀推导 kind；没有后缀时使用 artifact 兜底。
def _kind_from_path(path: Path) -> str:
    suffix = path.suffix.lower().lstrip(".")
    return suffix or "artifact"


# LLM: _status_from_request keeps explicit status authoritative.
# 函数用途: 优先使用调用方声明状态，否则按文件存在性生成 ready/missing。
def _status_from_request(request: ArtifactRegistration, path: Path) -> str:
    explicit = str(request.status or "").strip()
    if explicit:
        return explicit
    return "ready" if path.is_file() else "missing"


# LLM: _sha256_file makes artifact records content-addressable.
# 函数用途: 分块读取文件并计算 sha256，缺失文件返回空字符串。
def _sha256_file(path: Path) -> str:
    if not path.is_file():
        return ""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# LLM: _size_bytes records lightweight artifact size metadata.
# 函数用途: 返回文件大小，无法读取时安全返回 0。
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
