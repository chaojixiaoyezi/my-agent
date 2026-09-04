"""LLM: Protect registered artifacts around foreground shell execution.

模块用途: shell 无法复用 write_file 的逐文件写前校验，因此命令开始前把 ready 产物原子备份到
owner 私有数据区，结束后复核并只保留真正发生变化所需的恢复副本。
"""

from __future__ import annotations

import errno
import hashlib
import os
import secrets
import stat
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from agent_py_agent.agent.contracts.artifact_acceptance import (
    ArtifactAcceptanceRequest,
    validate_artifact,
)

from .registry import (
    ARTIFACT_ROLE_METADATA_KEY,
    ARTIFACT_ROLE_TOOL_OUTPUT_ARCHIVE,
    SHELL_PREIMAGE_POLICY_EXCLUDE,
    SHELL_PREIMAGE_POLICY_INCLUDE,
    SHELL_PREIMAGE_POLICY_METADATA_KEY,
    ArtifactRegistration,
    artifact_metadata_record_exists,
    artifact_operation_record_exists,
    latest_artifact_records_report,
    register_observed_artifact,
)
from .shell_operation_journal import (
    operation_manifest_ref,
    read_operation_manifest,
    remove_operation_manifest,
    write_operation_manifest,
)

_BACKUP_REF_PREFIX = "owner-artifact-backup:"
_BACKUP_SCHEMA = "v1"
_ACTIVE_OPERATION_KEYS: set[str] = set()
_ACTIVE_OPERATION_LOCK = threading.RLock()


# LLM: Snapshot exposes only an owner-local opaque backup ref; never add a host absolute backup path.
# 类用途: 保存 shell 启动前一份 ready 产物的内容身份和恢复引用，供退出后精确复核。
@dataclass(frozen=True)
class ShellArtifactSnapshot:
    """One protected artifact before run_command starts."""

    artifact_id: str
    run_id: str
    task_id: str
    agent_id: str
    kind: str
    mime_type: str
    path: str
    sha256: str
    size_bytes: int
    backup_ref: str
    operation_key: str

    # LLM: Public serialization may enter tool envelopes, so backup_ref must remain opaque and owner-local.
    # 函数用途: 把快照转成不泄露 owner 内部物理目录的结构化字典。
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# LLM: Manifest construction receives one immutable request so state transitions cannot accidentally
# mix identities or roots across positional parameters.
# 类用途: 汇总一次 shell 产物恢复清单需要持久化的宿主结构化字段。
@dataclass(frozen=True)
class _ShellOperationStateRequest:
    workspace_root: Path
    run_scope: object
    operation_id: str
    tool_call_id: str
    state: str
    snapshots: list[ShellArtifactSnapshot]
    source_roots: tuple[Path, ...] | None


# LLM: Post-shell observations carry only bytes read through the no-follow path seam. Unsafe
# paths use empty digest/size and an explicit finding; callers must not reopen them while recording.
# 类用途: 保存命令退出后安全观测到的变更状态，避免登记时再次读取可能已变化的路径。
@dataclass(frozen=True)
class ShellArtifactObservation:
    change_status: str
    findings: list[dict[str, Any]]
    sha256: str = ""
    size_bytes: int = 0


# LLM: This exception represents a structural source-boundary violation, never a task-quality
# judgment. Messages intentionally omit the hostile absolute path from model-visible output.
# 类用途: 标识产物路径越出受信根、变成符号链接或不再是普通文件。
class ArtifactSourceBoundaryError(OSError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


# LLM: This object owns one no-follow descriptor anchored under host-trusted roots. Consumers
# must close it exactly once and use the descriptor, not reopen `path`, for content reads.
# 类用途: 保存安全打开的产物文件描述符及稳定身份，供备份、哈希和复核共享。
@dataclass
class OpenedArtifactSource:
    path: Path
    descriptor: int
    size_bytes: int

    # LLM: Descriptor lifetime is explicit because retries and partial backup failures must not leak fds.
    # 函数用途: 关闭这次安全读取占用的文件描述符。
    def close(self) -> None:
        if self.descriptor >= 0:
            os.close(self.descriptor)
            self.descriptor = -1


# LLM: Backup storage is injected from HomePaths and must never be derived from the mutable task cwd.
# 函数用途: 把当前 ready 产物复制到 owner 私有数据区，为即将执行的前台 shell 留恢复引用。
def snapshot_ready_artifacts(
    workspace_root: str | Path,
    backup_store_root: str | Path | None,
    *,
    source_roots: tuple[str | Path, ...] | None = (),
    run_scope: object = None,
    tool_call_id: str = "",
    operation_id: str = "",
) -> list[ShellArtifactSnapshot]:
    """Copy current ready artifacts so shell damage has a restore reference."""

    root = _lexical_absolute_path(workspace_root)
    legacy_root = root / "data" / "artifacts" / "shell_backups"
    if legacy_root.exists() or legacy_root.is_symlink():
        if backup_store_root is None:
            raise OSError("canonical owner artifact backup root is unavailable")
        _migrate_legacy_shell_backups(root, backup_store_root)
    registry_report = latest_artifact_records_report(root)
    if registry_report.errors:
        raise OSError("artifact registry is unreadable or malformed")
    records = registry_report.records
    ready = [
        record
        for record in records.values()
        if str(record.status or "") == "ready"
        and not _is_artifact_group(record)
        and _requires_shell_preimage(record)
    ]
    if not ready:
        return []
    if backup_store_root is None:
        raise OSError("canonical owner artifact backup root is unavailable")
    store_root = _lexical_absolute_path(backup_store_root)
    trusted_roots = (
        (root,)
        if source_roots == ()
        else _normalized_source_roots(source_roots)
    )
    operation_key = _operation_key(
        run_scope,
        operation_id=operation_id,
        tool_call_id=tool_call_id,
    )
    operation_dir = store_root / _BACKUP_SCHEMA / operation_key
    snapshots: list[ShellArtifactSnapshot] = []
    try:
        for record in ready:
            try:
                opened = _open_regular_source(record.path, trusted_roots)
            except FileNotFoundError as exc:
                raise OSError(
                    "registered ready artifact disappeared before shell backup"
                ) from exc
            try:
                backup, digest, size_bytes = _write_atomic_backup(
                    opened,
                    operation_dir,
                    artifact_id=record.artifact_id,
                )
            finally:
                opened.close()
            snapshots.append(
                ShellArtifactSnapshot(
                    artifact_id=record.artifact_id,
                    run_id=record.run_id,
                    task_id=record.task_id,
                    agent_id=record.agent_id,
                    kind=record.kind,
                    mime_type=record.mime_type,
                    path=str(opened.path),
                    sha256=digest,
                    size_bytes=size_bytes,
                    backup_ref=_backup_ref(store_root, backup),
                    operation_key=operation_key,
                )
            )
        _write_shell_operation_state(
            store_root,
            operation_key,
            _ShellOperationStateRequest(
                workspace_root=root,
                run_scope=run_scope,
                operation_id=operation_id,
                tool_call_id=tool_call_id,
                state="prepared",
                snapshots=snapshots,
                source_roots=trusted_roots,
            ),
        )
    except BaseException:
        for snapshot in snapshots:
            try:
                _discard_backup(store_root, snapshot.backup_ref)
            except OSError:
                pass
        _prune_empty_backup_dirs(operation_dir, store_root / _BACKUP_SCHEMA)
        raise
    return snapshots


# LLM: Explicit host metadata is authoritative. The reserved tool_output role/kind is a migration
# default for old rows; every unknown/new artifact kind remains protected unless explicitly excluded.
# 函数用途: 判断 ready 记录是否属于用户交付物，需要在前台 shell 启动前保留恢复前像。
def _requires_shell_preimage(record: object) -> bool:
    metadata = getattr(record, "metadata", None)
    facts = metadata if isinstance(metadata, dict) else {}
    policy = str(facts.get(SHELL_PREIMAGE_POLICY_METADATA_KEY) or "").strip().lower()
    if policy == SHELL_PREIMAGE_POLICY_INCLUDE:
        return True
    if policy == SHELL_PREIMAGE_POLICY_EXCLUDE:
        return False
    role = str(facts.get(ARTIFACT_ROLE_METADATA_KEY) or "").strip().lower()
    if role == ARTIFACT_ROLE_TOOL_OUTPUT_ARCHIVE:
        return False
    return str(getattr(record, "kind", "") or "").strip().lower() != "tool_output"


# LLM: File groups use a directory projection plus typed member rows and require a separate
# multi-file reconciliation contract. Do not misread that projection as a forged single file.
# 函数用途: 识别结构化文件组，当前单文件 shell 保护链先保持旧版跳过语义而不阻断所有命令。
def _is_artifact_group(record: object) -> bool:
    metadata = getattr(record, "metadata", None)
    return isinstance(metadata, dict) and metadata.get("artifact_type") == "file_group"


# LLM: Legacy project-local backups are migrated before deletion. Only latest authoritative
# shell recovery refs are retained; unreferenced no-op copies are garbage. Every legacy ref must
# resolve beneath the exact task-local legacy root, and partial append failures keep old data.
# 函数用途: 升级时把仍被账本引用的旧 shell 恢复副本迁到 owner 私有区，再清掉项目里的历史备份树。
def _migrate_legacy_shell_backups(
    workspace_root: Path,
    backup_store_root: str | Path,
) -> dict[str, int]:
    legacy_root = workspace_root / "data" / "artifacts" / "shell_backups"
    legacy_identity = _verify_legacy_backup_tree(workspace_root)
    if legacy_identity is None:
        return {"migrated": 0, "removed_legacy_root": 0}
    canonical_legacy_root = legacy_root
    store_root = _lexical_absolute_path(backup_store_root)
    if store_root.is_relative_to(canonical_legacy_root) or canonical_legacy_root.is_relative_to(
        store_root
    ):
        raise OSError("owner backup store overlaps legacy task backup root")
    migration_key = _operation_key(
        {"task_id": hashlib.sha256(str(workspace_root).encode()).hexdigest()},
        operation_id="legacy-shell-backup-migration",
        tool_call_id="",
    )
    operation_dir = store_root / _BACKUP_SCHEMA / migration_key
    pending: list[tuple[object, str]] = []
    appended = 0
    try:
        registry_report = latest_artifact_records_report(workspace_root)
        if registry_report.errors:
            raise OSError("artifact registry is unreadable or malformed")
        for record in registry_report.records.values():
            legacy_ref = _legacy_backup_ref(record)
            if not legacy_ref:
                continue
            source = _legacy_backup_source(workspace_root, canonical_legacy_root, legacy_ref)
            try:
                target, digest, size_bytes = _write_atomic_backup(
                    source,
                    operation_dir,
                    artifact_id=record.artifact_id,
                )
            finally:
                source.close()
            new_ref = _backup_ref(store_root, target)
            pending.append((record, new_ref))
            _verify_legacy_preimage_metadata(record, digest, size_bytes)
        for record, new_ref in pending:
            metadata = dict(record.metadata or {})
            metadata.update(
                {
                    "backup_ref": new_ref,
                    "legacy_backup_ref_migrated": True,
                    "legacy_backup_source": str(record.source or ""),
                    "preimage_verification": "verified",
                    "migration_operation_id": migration_key,
                }
            )
            register_observed_artifact(
                ArtifactRegistration(
                    workspace_root=workspace_root,
                    path=record.path,
                    artifact_id=record.artifact_id,
                    run_id=record.run_id,
                    task_id=record.task_id,
                    agent_id=record.agent_id,
                    kind=record.kind,
                    mime_type=record.mime_type,
                    source="shell_backup_migration",
                    created_by_tool=record.created_by_tool,
                    status=record.status,
                    metadata=metadata,
                ),
                observed_sha256=record.sha256,
                observed_size_bytes=record.size_bytes,
            )
            appended += 1
        _remove_legacy_backup_tree(workspace_root, legacy_identity)
    except BaseException:
        for _record, new_ref in pending[appended:]:
            if not _migration_ref_may_be_durable(
                workspace_root,
                _record,
                new_ref,
                migration_key,
            ):
                try:
                    _discard_backup(store_root, new_ref)
                except (OSError, ValueError):
                    pass
        raise
    _prune_empty_backup_dirs(operation_dir, store_root / _BACKUP_SCHEMA)
    return {"migrated": appended, "removed_legacy_root": 1}


# LLM: Only old shell-postcheck rows may carry an absolute legacy recovery ref. Other metadata
# fields are unrelated and must not be interpreted as filesystem authority.
# 函数用途: 从旧版 shell 产物记录中取出需要迁移的绝对恢复路径。
def _legacy_backup_ref(record: object) -> str:
    metadata = getattr(record, "metadata", None)
    values = metadata if isinstance(metadata, dict) else {}
    ref = str(values.get("backup_ref") or "").strip()
    if not ref or ref.startswith(_BACKUP_REF_PREFIX):
        return ""
    change_status = str(values.get("change_status") or "").strip()
    source = str(getattr(record, "source", "") or "").strip()
    if source not in {"shell_postcheck", "shell_backup_migration"} and not change_status:
        return ""
    return ref


# LLM: Legacy refs are untrusted registry strings. Check lexical and resolved containment plus
# regular-file identity before opening; never accept a sibling path that merely shares a prefix.
# 函数用途: 把旧绝对引用解析成 exact legacy root 下的普通备份文件。
def _legacy_backup_source(
    workspace_root: Path,
    legacy_root: Path,
    legacy_ref: str,
) -> OpenedArtifactSource:
    raw_candidate = Path(legacy_ref).expanduser()
    if not raw_candidate.is_absolute() or ".." in raw_candidate.parts:
        raise OSError("legacy shell backup ref is not absolute")
    candidate = Path(os.path.normpath(str(raw_candidate)))
    canonical_legacy_root = Path(os.path.normpath(str(legacy_root)))
    try:
        candidate.relative_to(canonical_legacy_root)
    except ValueError as exc:
        raise OSError("legacy shell backup ref escapes task backup root") from exc
    try:
        return _open_regular_source(candidate, (canonical_legacy_root,))
    except (ArtifactSourceBoundaryError, FileNotFoundError) as exc:
        raise OSError("legacy shell backup ref is unavailable") from exc


# LLM: Old postcheck rows already carry the expected preimage hash and size. Migration may publish
# a replacement opaque ref only when the copied bytes match those facts exactly.
# 函数用途: 校验旧恢复副本确实是账本声称的命令前内容，缺字段或不一致都保留旧树并停止迁移。
def _verify_legacy_preimage_metadata(record: object, digest: str, size_bytes: int) -> None:
    metadata = getattr(record, "metadata", None)
    values = metadata if isinstance(metadata, dict) else {}
    if "previous_sha256" not in values or "previous_size_bytes" not in values:
        raise OSError("legacy shell backup lacks preimage identity")
    try:
        expected_size = int(values.get("previous_size_bytes"))
    except (TypeError, ValueError) as exc:
        raise OSError("legacy shell backup has invalid preimage identity") from exc
    expected_digest = str(values.get("previous_sha256") or "")
    if not expected_digest or expected_digest != digest or expected_size != size_bytes:
        raise OSError("legacy shell backup does not match preimage identity")


# LLM: An append may have reached disk even when the caller observed an exception. Re-read the
# canonical ledger; uncertainty keeps the blob so no durable opaque ref can dangle.
# 函数用途: 判断迁移引用是否可能已经落账，只有确定没落账时才允许清理对应新 blob。
def _migration_ref_may_be_durable(
    workspace_root: Path,
    record: object,
    new_ref: str,
    migration_key: str,
) -> bool:
    artifact_id = str(getattr(record, "artifact_id", "") or "")
    try:
        return artifact_metadata_record_exists(
            workspace_root,
            artifact_id=artifact_id,
            required_metadata={
                "backup_ref": new_ref,
                "migration_operation_id": migration_key,
                "preimage_verification": "verified",
            },
        )
    except OSError:
        return True


# LLM: Legacy cleanup is destructive, so every fixed path component must be opened from the
# resolved workspace fd with O_NOFOLLOW. A symlink at data/artifacts/shell_backups aborts migration.
# 函数用途: 确认旧备份树确实是当前任务目录里的框架目录，而不是指向外部的链接。
def _verify_legacy_backup_tree(workspace_root: Path) -> tuple[int, int] | None:
    try:
        descriptor = _open_directory_beneath(
            workspace_root,
            ("data", "artifacts", "shell_backups"),
        )
    except FileNotFoundError:
        return None
    except ArtifactSourceBoundaryError as exc:
        raise OSError("legacy shell backup tree is unsafe") from exc
    try:
        metadata = os.fstat(descriptor)
        return (int(metadata.st_dev), int(metadata.st_ino))
    finally:
        os.close(descriptor)


# LLM: Removal first atomically renames the exact verified child inside its already-open parent,
# then recursively unlinks via dir_fd. No absolute resolved target reaches a recursive delete API.
# 函数用途: 把已迁移的旧备份树在原父目录内隔离后安全删除。
def _remove_legacy_backup_tree(
    workspace_root: Path,
    expected_identity: tuple[int, int],
) -> None:
    try:
        parent_fd = _open_directory_beneath(workspace_root, ("data", "artifacts"))
    except (FileNotFoundError, ArtifactSourceBoundaryError) as exc:
        raise OSError("legacy shell backup parent is unavailable") from exc
    quarantine = f".shell_backups-migrated-{secrets.token_hex(16)}"
    try:
        source = os.stat("shell_backups", dir_fd=parent_fd, follow_symlinks=False)
        if stat.S_ISLNK(source.st_mode) or not stat.S_ISDIR(source.st_mode):
            raise OSError("legacy shell backup root changed before cleanup")
        if (int(source.st_dev), int(source.st_ino)) != expected_identity:
            raise OSError("legacy shell backup root identity changed before cleanup")
        os.rename(
            "shell_backups",
            quarantine,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        _rmtree_at(parent_fd, quarantine, expected_identity=expected_identity)
    finally:
        os.close(parent_fd)


# LLM: This helper is the directory analogue of artifact source traversal. Each component is
# opened relative to the previous verified descriptor and no path string can escape upward.
# 函数用途: 从指定根目录逐层安全打开一个内部子目录。
def _open_directory_beneath(root: Path, parts: tuple[str, ...]) -> int:
    descriptor = _open_verified_directory(root)
    try:
        for part in parts:
            child = _openat_verified(descriptor, part, directory=True)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


# LLM: Recursive deletion receives an open parent descriptor and treats every symlink as a leaf.
# It never follows directory links and removes only the quarantined exact name.
# 函数用途: 通过 dir_fd 递归删除隔离后的框架旧目录，避免符号链接删除逃逸。
def _rmtree_at(
    parent_fd: int,
    name: str,
    *,
    expected_identity: tuple[int, int] | None = None,
) -> None:
    directory_fd = _openat_verified(parent_fd, name, directory=True)
    try:
        if expected_identity is not None:
            metadata = os.fstat(directory_fd)
            if (int(metadata.st_dev), int(metadata.st_ino)) != expected_identity:
                raise OSError("quarantined legacy backup identity changed")
        with os.scandir(directory_fd) as entries:
            names = [entry.name for entry in entries]
        for child_name in names:
            metadata = os.stat(child_name, dir_fd=directory_fd, follow_symlinks=False)
            if stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode):
                _rmtree_at(directory_fd, child_name)
            else:
                os.unlink(child_name, dir_fd=directory_fd)
    finally:
        os.close(directory_fd)
    os.rmdir(name, dir_fd=parent_fd)


# LLM: `executing` is written durably before spawn and also tracked in-process. Reconciliation in
# the same live Gateway must not inspect a command that is still mutating its artifacts.
# 函数用途: 把已完成预备份的 operation 原子推进到“命令可能已经执行”，并登记本进程活跃身份。
def mark_shell_artifact_operation_executing(
    backup_store_root: str | Path,
    snapshots: list[ShellArtifactSnapshot],
) -> str:
    operation_key, manifest = _manifest_for_snapshots(backup_store_root, snapshots)
    if str(manifest.get("state") or "") != "prepared":
        raise OSError("shell artifact operation is not prepared")
    with _ACTIVE_OPERATION_LOCK:
        if operation_key in _ACTIVE_OPERATION_KEYS:
            raise OSError("shell artifact operation is already active")
        _ACTIVE_OPERATION_KEYS.add(operation_key)
    try:
        manifest["state"] = "executing"
        manifest["updated_at"] = time.time()
        write_operation_manifest(backup_store_root, operation_key, manifest)
    except BaseException:
        release_shell_artifact_operation(operation_key)
        raise
    return operation_key


# LLM: The completed handler result closes the crash window between ShellTool return and the
# generic ToolOperation store commit. It remains owner-private and is replayed only for the exact id.
# 函数用途: 在工具返回前持久化 shell 的确定结果，供 operation store 在断电后精确核对。
def complete_shell_artifact_operation(
    backup_store_root: str | Path,
    snapshots: list[ShellArtifactSnapshot],
    result: dict[str, Any],
) -> str:
    operation_key, manifest = _manifest_for_snapshots(backup_store_root, snapshots)
    if str(manifest.get("state") or "") != "executing":
        raise OSError("shell artifact operation is not executing")
    items = manifest.get("items")
    if not isinstance(items, list) or any(
        not isinstance(item, dict)
        or str(item.get("state") or "")
        not in {"registered", "unchanged", "cleanup_pending"}
        for item in items
    ):
        raise OSError("shell artifact operation reconciliation is incomplete")
    manifest["state"] = "completed"
    manifest["result"] = dict(result)
    manifest["updated_at"] = time.time()
    write_operation_manifest(backup_store_root, operation_key, manifest)
    release_shell_artifact_operation(operation_key)
    return operation_manifest_ref(operation_key)


# LLM: Managed callers invoke this only after the matching ToolOperation terminal fact persists;
# direct/read-only calls without an operation store invoke it before returning. It drops the
# crash-window manifest while preserving changed/invalid blobs still referenced by the registry.
# 函数用途: 在权威账本结算后或无账本调用返回前清理 shell 临时清单；有效旧版本继续保留。
def settle_shell_artifact_operation(
    backup_store_root: str | Path,
    *,
    run_scope: dict[str, object],
    operation_id: str,
    operation_key: str = "",
) -> bool:
    scope = run_scope if isinstance(run_scope, dict) else {}
    operation_key = str(operation_key or "").strip() or _operation_key(
        scope, operation_id=operation_id, tool_call_id=""
    )
    manifest = read_operation_manifest(backup_store_root, operation_key)
    if manifest is None:
        return False
    root = _lexical_absolute_path(str(manifest.get("workspace_root") or ""))
    _validate_operation_manifest_context(manifest, root, scope, operation_id)
    with _ACTIVE_OPERATION_LOCK:
        if operation_key in _ACTIVE_OPERATION_KEYS:
            raise OSError("active shell artifact operation cannot be settled")
    if str(manifest.get("state") or "") != "completed":
        raise OSError("shell artifact operation is not completed")
    snapshots = _snapshots_from_manifest(manifest, operation_key)
    _retry_manifest_cleanup(backup_store_root, operation_key, manifest, snapshots)
    items = manifest.get("items")
    if not isinstance(items, list):
        raise OSError("shell artifact operation items are invalid")
    if any(
        isinstance(item, dict)
        and str(item.get("state") or "") in {"cleanup_pending", "unchanged_observed"}
        for item in items
    ):
        return False
    remove_operation_manifest(backup_store_root, operation_key)
    store_root = _lexical_absolute_path(backup_store_root)
    _prune_empty_backup_dirs(
        store_root / _BACKUP_SCHEMA / operation_key,
        store_root / _BACKUP_SCHEMA,
    )
    return True


# LLM: An uncertain handler path deliberately leaves its manifest at `executing`; only the
# in-memory activity marker is released so the generic operation reconciler can inspect it later.
# 函数用途: 命令结果未知或复核失败时释放本进程占用，但保留耐久恢复清单。
def release_shell_artifact_operation(operation_key: str) -> None:
    key = str(operation_key or "").strip()
    if not key:
        return
    with _ACTIVE_OPERATION_LOCK:
        _ACTIVE_OPERATION_KEYS.discard(key)


# LLM: Generic ToolOperation reconciliation uses the exact stable operation id. Prepared means
# no spawn was attempted; executing means side effects may exist and is never automatically replayed.
# 函数用途: Gateway 重启或 operation 变成 unknown 后，按私有清单核对产物并返回结构化终态事实。
def reconcile_shell_artifact_operation(
    backup_store_root: str | Path,
    *,
    run_scope: dict[str, object],
    operation_id: str,
) -> dict[str, Any]:
    operation_key = _operation_key(
        run_scope,
        operation_id=operation_id,
        tool_call_id="",
    )
    source_ref = operation_manifest_ref(operation_key)
    manifest = read_operation_manifest(backup_store_root, operation_key)
    if manifest is None:
        return {"outcome": "unknown", "source_ref": source_ref, "reason": "manifest_missing"}
    root = _lexical_absolute_path(str(manifest.get("workspace_root") or ""))
    _validate_operation_manifest_context(manifest, root, run_scope, operation_id)
    with _ACTIVE_OPERATION_LOCK:
        if operation_key in _ACTIVE_OPERATION_KEYS:
            return {
                "outcome": "unknown",
                "source_ref": source_ref,
                "reason": "operation_still_active",
            }
    snapshots = _snapshots_from_manifest(manifest, operation_key)
    state = str(manifest.get("state") or "")
    if state == "prepared":
        manifest["state"] = "not_started"
        manifest["updated_at"] = time.time()
        write_operation_manifest(backup_store_root, operation_key, manifest)
        cleanup_deferred: list[str] = []
        for snapshot in snapshots:
            try:
                _discard_backup(backup_store_root, snapshot.backup_ref)
            except (OSError, ValueError):
                cleanup_deferred.append(snapshot.backup_ref)
        manifest["cleanup_deferred"] = cleanup_deferred
        manifest["updated_at"] = time.time()
        write_operation_manifest(backup_store_root, operation_key, manifest)
        return {"outcome": "not_started", "source_ref": source_ref}
    if state == "not_started":
        return {"outcome": "not_started", "source_ref": source_ref}
    if state == "completed":
        _retry_manifest_cleanup(backup_store_root, operation_key, manifest, snapshots)
        result = manifest.get("result")
        if not isinstance(result, dict):
            raise OSError("completed shell artifact operation lacks result")
        if result.get("ok") is True:
            outcome = "succeeded"
        elif str(result.get("effect_outcome") or "") in {"not_started", "failed"}:
            outcome = "failed"
        else:
            outcome = "unknown"
        return {
            "outcome": outcome,
            "source_ref": source_ref,
            "result": dict(result) if outcome != "unknown" else None,
            "reason": (
                "" if outcome != "unknown" else "completed_shell_effect_remains_unknown"
            ),
        }
    if state == "terminal_unknown":
        _retry_manifest_cleanup(backup_store_root, operation_key, manifest, snapshots)
        return {
            "outcome": "unknown",
            "source_ref": source_ref,
            "reason": str(manifest.get("reason") or "command_effect_unknown"),
        }
    if state != "executing":
        raise OSError("shell artifact operation has invalid state")
    summary = reconcile_shell_artifact_operation_items(
        root,
        snapshots,
        backup_store_root=backup_store_root,
        source_roots=_manifest_source_roots(manifest),
    )
    manifest["state"] = "terminal_unknown"
    manifest["reconciliation"] = summary
    manifest["reason"] = "command_effect_unknown_artifacts_reconciled"
    manifest["updated_at"] = time.time()
    write_operation_manifest(backup_store_root, operation_key, manifest)
    return {
        "outcome": "unknown",
        "source_ref": source_ref,
        "reason": "command_effect_unknown_artifacts_reconciled",
    }


# LLM: Every snapshot in one batch must point to the same exact operation directory. This prevents
# a forged in-memory item from deleting or updating another operation's recovery data.
# 函数用途: 读取一批快照对应的清单并验证 operation key 和 opaque backup ref 都一致。
def _manifest_for_snapshots(
    backup_store_root: str | Path,
    snapshots: list[ShellArtifactSnapshot],
) -> tuple[str, dict[str, Any]]:
    if not snapshots:
        raise ValueError("shell artifact snapshots are empty")
    operation_key = snapshots[0].operation_key
    if any(
        snapshot.operation_key != operation_key
        or not _backup_ref_belongs_to_operation(snapshot.backup_ref, operation_key)
        for snapshot in snapshots
    ):
        raise OSError("shell artifact snapshots cross operation boundary")
    manifest = read_operation_manifest(backup_store_root, operation_key)
    if manifest is None:
        raise OSError("shell artifact operation manifest is missing")
    expected = [snapshot.to_dict() for snapshot in snapshots]
    if manifest.get("snapshots") != expected:
        raise OSError("shell artifact snapshots do not match durable manifest")
    return operation_key, manifest


# LLM: Manifest creation captures only host-owned identity and the already-authorized source roots;
# no command text or model prose participates in recovery decisions.
# 函数用途: 持久化 shell 产物保护阶段、快照、工作区身份和结构化运行身份。
def _write_shell_operation_state(
    backup_store_root: Path,
    operation_key: str,
    request: _ShellOperationStateRequest,
) -> None:
    identity = _path_identity(request.workspace_root)
    scope = request.run_scope if isinstance(request.run_scope, dict) else {}
    payload = {
        "state": request.state,
        "workspace_root": str(request.workspace_root),
        "workspace_identity": {"device": identity[0], "inode": identity[1]},
        "owner_id": str(scope.get("owner_id") or ""),
        "run_id": str(scope.get("run_id") or ""),
        "task_id": str(scope.get("task_id") or ""),
        "operation_id": str(request.operation_id or ""),
        "tool_call_id": str(request.tool_call_id or ""),
        "source_roots": (
            None
            if request.source_roots is None
            else [str(root) for root in request.source_roots]
        ),
        "snapshots": [snapshot.to_dict() for snapshot in request.snapshots],
        "items": [
            {"state": "prepared", "snapshot": snapshot.to_dict()}
            for snapshot in request.snapshots
        ],
        "updated_at": time.time(),
    }
    write_operation_manifest(backup_store_root, operation_key, payload)


# LLM: Reconciliation must bind the private manifest back to the current canonical operation and
# the unchanged workspace inode before any registered path or backup ref is consumed.
# 函数用途: 核对清单属于当前 owner/run/task/operation 和同一个工作区目录。
def _validate_operation_manifest_context(
    manifest: dict[str, Any],
    workspace_root: Path,
    run_scope: dict[str, object],
    operation_id: str,
) -> None:
    expected = {
        "owner_id": str(run_scope.get("owner_id") or ""),
        "run_id": str(run_scope.get("run_id") or ""),
        "task_id": str(run_scope.get("task_id") or ""),
        "operation_id": str(operation_id or ""),
    }
    if str(manifest.get("workspace_root") or "") != str(workspace_root) or any(
        str(manifest.get(key) or "") != value for key, value in expected.items()
    ):
        raise OSError("shell artifact operation context does not match")
    identity = manifest.get("workspace_identity")
    current = _path_identity(workspace_root)
    if not isinstance(identity, dict) or (
        int(identity.get("device") or -1),
        int(identity.get("inode") or -1),
    ) != current:
        raise OSError("shell artifact workspace identity changed")


# LLM: Snapshot reconstruction accepts only the exact schema fields and verifies every recovery
# ref remains inside this operation. Extra manifest text never becomes constructor authority.
# 函数用途: 从私有清单恢复强类型 shell 产物快照。
def _snapshots_from_manifest(
    manifest: dict[str, Any],
    operation_key: str,
) -> list[ShellArtifactSnapshot]:
    rows = manifest.get("snapshots")
    if not isinstance(rows, list) or not rows:
        raise OSError("shell artifact operation has no snapshots")
    snapshots: list[ShellArtifactSnapshot] = []
    fields = tuple(ShellArtifactSnapshot.__dataclass_fields__)
    for row in rows:
        if not isinstance(row, dict):
            raise OSError("shell artifact operation snapshot is invalid")
        try:
            snapshot = ShellArtifactSnapshot(**{field: row[field] for field in fields})
        except (KeyError, TypeError, ValueError) as exc:
            raise OSError("shell artifact operation snapshot is invalid") from exc
        if snapshot.operation_key != operation_key or not _backup_ref_belongs_to_operation(
            snapshot.backup_ref,
            operation_key,
        ):
            raise OSError("shell artifact operation snapshot crosses boundary")
        snapshots.append(snapshot)
    return snapshots


# LLM: Stored source roots came from the original authorized invocation and are used only to
# narrow host reads during reconciliation; malformed values fail closed.
# 函数用途: 从清单恢复命令执行时的受信读取根。
def _manifest_source_roots(manifest: dict[str, Any]) -> tuple[Path, ...] | None:
    values = manifest.get("source_roots")
    if values is None:
        return None
    if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
        raise OSError("shell artifact operation source roots are invalid")
    return tuple(_lexical_absolute_path(value) for value in values)


# LLM: Backup refs encode v1/<operation_key>/<blob>; exact component comparison prevents one
# manifest from gaining cleanup authority over a sibling operation.
# 函数用途: 判断 opaque backup ref 是否属于指定 shell operation。
def _backup_ref_belongs_to_operation(backup_ref: str, operation_key: str) -> bool:
    prefix = f"{_BACKUP_REF_PREFIX}{_BACKUP_SCHEMA}/{operation_key}/"
    return str(backup_ref or "").startswith(prefix)


# LLM: Workspace identity is checked without following a replacement symlink.
# 函数用途: 返回普通工作区目录的设备号和 inode，链接或非目录直接拒绝。
def _path_identity(path: Path) -> tuple[int, int]:
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise OSError("artifact workspace root is not a regular directory")
    return (int(metadata.st_dev), int(metadata.st_ino))


# LLM: Reconcile one snapshot at a time and commit the per-item phase after each append/cleanup.
# If append acknowledgment is lost, scan the exact operation/ref row before touching live bytes.
# 函数用途: 幂等复核一次 shell operation 的所有产物，并在每项完成后立即刷入私有清单。
def reconcile_shell_artifact_operation_items(
    workspace_root: str | Path,
    snapshots: list[ShellArtifactSnapshot],
    *,
    backup_store_root: str | Path,
    source_roots: tuple[str | Path, ...] | None = (),
) -> dict[str, Any]:
    operation_key, manifest = _manifest_for_snapshots(backup_store_root, snapshots)
    if str(manifest.get("state") or "") != "executing":
        raise OSError("shell artifact operation is not executing")
    items = manifest.get("items")
    if not isinstance(items, list) or len(items) != len(snapshots):
        raise OSError("shell artifact operation items are invalid")
    aggregate = _empty_reconciliation_summary(len(snapshots))
    for index, snapshot in enumerate(snapshots):
        item = items[index]
        if not isinstance(item, dict) or item.get("snapshot") != snapshot.to_dict():
            raise OSError("shell artifact operation item does not match snapshot")
        state = str(item.get("state") or "")
        stored_summary = item.get("summary")
        if state in {"registered", "unchanged"}:
            _merge_reconciliation_summary(aggregate, stored_summary)
            continue
        if state in {"unchanged_observed", "cleanup_pending"}:
            cleanup_deferred = _finish_unchanged_item_cleanup(
                backup_store_root,
                snapshot,
            )
            item["state"] = "cleanup_pending" if cleanup_deferred else "unchanged"
            summary = (
                dict(stored_summary)
                if isinstance(stored_summary, dict)
                else _unchanged_item_summary(snapshot)
            )
            summary["cleanup_deferred"] = cleanup_deferred
            item["summary"] = summary
            item["updated_at"] = time.time()
            manifest["items"] = items
            manifest["updated_at"] = time.time()
            write_operation_manifest(backup_store_root, operation_key, manifest)
            _merge_reconciliation_summary(aggregate, summary)
            continue
        if state != "prepared":
            raise OSError("shell artifact operation item has invalid state")
        if artifact_operation_record_exists(
            workspace_root,
            artifact_id=snapshot.artifact_id,
            operation_key=operation_key,
            backup_ref=snapshot.backup_ref,
        ):
            summary = {
                "snapshots": 1,
                "changed": [
                    {
                        "artifact_id": snapshot.artifact_id,
                        "path": snapshot.path,
                        "status": "recorded",
                        "change_status": "recovered_committed_record",
                        "backup_ref": snapshot.backup_ref,
                        "finding_codes": [],
                    }
                ],
                "invalid": [],
                "cleanup_deferred": [],
            }
            item["state"] = "registered"
        else:
            summary = reconcile_shell_artifacts(
                workspace_root,
                [snapshot],
                backup_store_root=backup_store_root,
                source_roots=source_roots,
                defer_unchanged_cleanup=True,
            )
            if summary.get("changed"):
                item["state"] = "registered"
            else:
                item["state"] = "unchanged_observed"
                item["summary"] = summary
                item["updated_at"] = time.time()
                manifest["items"] = items
                manifest["updated_at"] = time.time()
                write_operation_manifest(backup_store_root, operation_key, manifest)
                cleanup_deferred = _finish_unchanged_item_cleanup(
                    backup_store_root,
                    snapshot,
                )
                summary["cleanup_deferred"] = cleanup_deferred
                item["state"] = (
                    "cleanup_pending" if cleanup_deferred else "unchanged"
                )
        item["summary"] = summary
        item["updated_at"] = time.time()
        manifest["items"] = items
        manifest["updated_at"] = time.time()
        write_operation_manifest(backup_store_root, operation_key, manifest)
        _merge_reconciliation_summary(aggregate, summary)
    return aggregate


# LLM: Summary shape is shared with the existing shell postcheck projection. Counts are recomputed
# from durable per-item rows instead of trusting model-visible text.
# 函数用途: 创建一次 operation 级产物复核的空聚合结果。
def _empty_reconciliation_summary(snapshot_count: int) -> dict[str, Any]:
    return {
        "snapshots": snapshot_count,
        "changed": [],
        "invalid": [],
        "unchanged": [],
        "cleanup_deferred": [],
    }


# LLM: Per-item summaries are host-owned dictionaries produced by reconcile_shell_artifacts.
# Merge only known list fields so unexpected payload keys cannot gain state authority.
# 函数用途: 把单个产物复核结果合并到 operation 汇总。
def _merge_reconciliation_summary(
    aggregate: dict[str, Any],
    summary: object,
) -> None:
    if not isinstance(summary, dict):
        return
    for key in ("changed", "invalid", "unchanged", "cleanup_deferred"):
        values = summary.get(key)
        if isinstance(values, list):
            aggregate[key].extend(values)


# LLM: The durable `unchanged_observed` phase always precedes blob deletion. This helper can be
# replayed after a crash without reopening or reclassifying the live artifact.
# 函数用途: 清理一份已确认无变化的前像，失败时返回需要后续重试的 opaque ref。
def _finish_unchanged_item_cleanup(
    backup_store_root: str | Path,
    snapshot: ShellArtifactSnapshot,
) -> list[str]:
    try:
        _discard_backup(backup_store_root, snapshot.backup_ref)
    except (OSError, ValueError):
        return [snapshot.backup_ref]
    return []


# LLM: A minimal unchanged summary records the exact item/ref that was observed before cleanup;
# no current filesystem state is consulted while replaying cleanup debt.
# 函数用途: 构造单个未变化产物的结构化复核结果。
def _unchanged_item_summary(snapshot: ShellArtifactSnapshot) -> dict[str, Any]:
    return {
        "snapshots": 1,
        "changed": [],
        "invalid": [],
        "unchanged": [
            {
                "artifact_id": snapshot.artifact_id,
                "path": snapshot.path,
                "backup_ref": snapshot.backup_ref,
            }
        ],
        "cleanup_deferred": [],
    }


# LLM: Terminal manifests may retain cleanup debt without changing the command outcome. Retry only
# those recorded refs and persist each transition; never rerun validation or shell execution.
# 函数用途: 核对终态清单时重试无变化前像的清理债务。
def _retry_manifest_cleanup(
    backup_store_root: str | Path,
    operation_key: str,
    manifest: dict[str, Any],
    snapshots: list[ShellArtifactSnapshot],
) -> None:
    items = manifest.get("items")
    if not isinstance(items, list) or len(items) != len(snapshots):
        raise OSError("shell artifact operation items are invalid")
    changed = False
    for index, snapshot in enumerate(snapshots):
        item = items[index]
        if not isinstance(item, dict) or str(item.get("state") or "") not in {
            "cleanup_pending",
            "unchanged_observed",
        }:
            continue
        deferred = _finish_unchanged_item_cleanup(backup_store_root, snapshot)
        item["state"] = "cleanup_pending" if deferred else "unchanged"
        summary = item.get("summary")
        if isinstance(summary, dict):
            summary["cleanup_deferred"] = deferred
        item["updated_at"] = time.time()
        changed = True
    if changed:
        manifest["items"] = items
        manifest["updated_at"] = time.time()
        write_operation_manifest(backup_store_root, operation_key, manifest)


# LLM: Reconciliation owns backup retention: unchanged preimages are deleted, changed/unknown preimages remain durable.
# 函数用途: 复核 shell 前后产物，更新登记状态并清理没有发生变化的临时恢复副本。
def reconcile_shell_artifacts(
    workspace_root: str | Path,
    snapshots: list[ShellArtifactSnapshot],
    *,
    backup_store_root: str | Path,
    source_roots: tuple[str | Path, ...] | None = (),
    defer_unchanged_cleanup: bool = False,
) -> dict[str, Any]:
    """Update registry entries for shell-changed artifacts and return a prompt-safe summary."""

    root = _lexical_absolute_path(workspace_root)
    trusted_roots = (
        (root,)
        if source_roots == ()
        else _normalized_source_roots(source_roots)
    )
    changed: list[dict[str, Any]] = []
    invalid: list[dict[str, Any]] = []
    unchanged: list[dict[str, Any]] = []
    cleanup_deferred: list[str] = []
    for snapshot in snapshots:
        path = Path(snapshot.path).expanduser()
        observation = _post_shell_status(
            path,
            root,
            snapshot,
            trusted_roots,
            _lexical_absolute_path(backup_store_root),
        )
        if observation.change_status == "unchanged":
            unchanged.append(
                {
                    "artifact_id": snapshot.artifact_id,
                    "path": str(path),
                    "backup_ref": snapshot.backup_ref,
                }
            )
            if not defer_unchanged_cleanup:
                try:
                    _discard_backup(backup_store_root, snapshot.backup_ref)
                except (OSError, ValueError):
                    cleanup_deferred.append(snapshot.backup_ref)
            continue
        _verify_backup_preimage(backup_store_root, snapshot)
        registered = register_observed_artifact(
            ArtifactRegistration(
                workspace_root=root,
                path=path,
                artifact_id=snapshot.artifact_id,
                run_id=snapshot.run_id,
                task_id=snapshot.task_id,
                agent_id=snapshot.agent_id,
                kind=snapshot.kind,
                mime_type=snapshot.mime_type,
                source="shell_postcheck",
                created_by_tool="run_command",
                status=(
                    "ready"
                    if observation.change_status == "changed_ready"
                    else "invalid"
                ),
                metadata={
                    "change_status": observation.change_status,
                    "previous_sha256": snapshot.sha256,
                    "previous_size_bytes": snapshot.size_bytes,
                    "backup_ref": snapshot.backup_ref,
                    "protection_operation_id": snapshot.operation_key,
                    "findings": observation.findings,
                },
            ),
            observed_sha256=observation.sha256,
            observed_size_bytes=observation.size_bytes,
        )
        item = {
            "artifact_id": snapshot.artifact_id,
            "path": str(path),
            "status": registered.status,
            "change_status": observation.change_status,
            "backup_ref": snapshot.backup_ref,
            "finding_codes": [
                str(finding.get("code") or "")
                for finding in observation.findings
                if finding.get("code")
            ],
        }
        changed.append(item)
        if registered.status == "invalid":
            invalid.append(item)
    return {
        "snapshots": len(snapshots),
        "changed": changed,
        "invalid": invalid,
        "unchanged": unchanged,
        "cleanup_deferred": cleanup_deferred,
    }


# LLM: Only changed/invalid facts belong in the model prompt; never reveal the owner store physical path.
# 函数用途: 把 shell 产物复核结果整理成简短、可追踪的模型提示。
def shell_artifact_protection_note(summary: dict[str, Any]) -> str:
    """Render concise machine-readable facts for the model after shell exits."""

    changed = list(summary.get("changed") or [])
    invalid = list(summary.get("invalid") or [])
    if not changed and not invalid:
        return ""
    lines = [
        "artifact_protection:",
        f"artifact_protection_changed={len(changed)}",
        f"artifact_protection_invalid={len(invalid)}",
    ]
    for item in invalid[:5]:
        codes = ",".join(str(code) for code in item.get("finding_codes") or [])
        lines.append(
            "artifact_invalid_after_shell="
            f"{item.get('artifact_id')} path={item.get('path')} "
            f"backup_ref={item.get('backup_ref')} codes={codes}"
        )
    return "\n".join(lines)


# LLM: Post-check compares against the exact copied preimage, not a possibly stale registry digest.
# 函数用途: 判断 shell 是否删除、破坏、修改或保持了某份已登记产物。
def _post_shell_status(
    path: Path,
    workspace_root: Path,
    snapshot: ShellArtifactSnapshot,
    source_roots: tuple[Path, ...] | None = (),
    backup_store_root: Path | None = None,
) -> ShellArtifactObservation:
    try:
        opened = _open_regular_source(path, source_roots)
    except FileNotFoundError:
        return ShellArtifactObservation(
            "missing_after_shell",
            [{"code": "ARTIFACT_MISSING_AFTER_SHELL", "message": "artifact path is missing"}],
        )
    except ArtifactSourceBoundaryError as exc:
        return ShellArtifactObservation(
            "invalid_after_shell",
            [{"code": exc.code, "message": str(exc)}],
        )
    try:
        if backup_store_root is None:
            raise OSError("canonical owner artifact backup root is unavailable")
        validation_copy, current_sha, current_size = _write_validation_copy(
            opened,
            backup_store_root,
        )
        try:
            if current_sha == snapshot.sha256:
                return ShellArtifactObservation("unchanged", [], current_sha, current_size)
            if current_size == 0:
                return ShellArtifactObservation(
                    "invalid_after_shell",
                    [{"code": "ARTIFACT_EMPTY_AFTER_SHELL", "message": "artifact is empty"}],
                    current_sha,
                    current_size,
                )
            report = validate_artifact(
                ArtifactAcceptanceRequest(
                    path=validation_copy,
                    workspace_root=validation_copy.parent,
                    validation_contract={"artifact_kind": snapshot.kind},
                    reference_roots=(path.parent, workspace_root),
                )
            )
        finally:
            _discard_validation_copy(validation_copy, backup_store_root)
        findings = [
            _logical_finding(finding, validation_copy, path)
            for finding in report.findings
        ]
        return ShellArtifactObservation(
            "changed_ready" if report.ok else "invalid_after_shell",
            findings,
            current_sha,
            current_size,
        )
    finally:
        opened.close()


# LLM: None is the explicit local-admin Full Access sentinel. Every tuple value is normalized
# once and must come from host runtime state, never from registry rows or command text.
# 函数用途: 规范化当前 shell 可安全读取并保护的产物根目录。
def _normalized_source_roots(
    source_roots: tuple[str | Path, ...] | None,
) -> tuple[Path, ...] | None:
    if source_roots is None:
        return None
    roots: list[Path] = []
    for value in source_roots:
        root = _lexical_absolute_path(value)
        if root not in roots:
            roots.append(root)
    return tuple(roots)


# LLM: Registry paths are untrusted durable data. POSIX walks every component from one trusted
# root with dir_fd + O_NOFOLLOW; the portable fallback compares lstat/fstat identity.
# 函数用途: 从受信根逐段打开一份普通文件，防止父目录或文件符号链接把宿主读取带出权限边界。
def _open_regular_source(
    raw_path: str | Path,
    source_roots: tuple[Path, ...] | None,
) -> OpenedArtifactSource:
    raw_candidate = Path(raw_path).expanduser()
    if not raw_candidate.is_absolute():
        raise ArtifactSourceBoundaryError(
            "ARTIFACT_SOURCE_PATH_INVALID",
            "registered artifact path is not absolute",
        )
    candidate = Path(os.path.normpath(str(raw_candidate)))
    anchor, relative = _source_anchor(candidate, source_roots)
    if os.open in os.supports_dir_fd:
        return _open_regular_beneath(anchor, relative, candidate)
    return _open_regular_portable(candidate, source_roots)


# LLM: Select the most specific lexical trusted root before any filesystem resolution. `None`
# means explicit Full Access and anchors traversal at the filesystem root.
# 函数用途: 为目标路径选择最窄的受信起点和逐段相对路径。
def _source_anchor(
    candidate: Path,
    source_roots: tuple[Path, ...] | None,
) -> tuple[Path, Path]:
    if source_roots is None:
        anchor = Path(candidate.anchor)
        return anchor, candidate.relative_to(anchor)
    matches: list[tuple[Path, Path]] = []
    for root in source_roots:
        try:
            relative = candidate.relative_to(root)
        except ValueError:
            continue
        if ".." not in relative.parts:
            matches.append((root, relative))
    if not matches:
        raise ArtifactSourceBoundaryError(
            "ARTIFACT_SOURCE_SCOPE_BLOCKED",
            "registered artifact path is outside trusted source roots",
        )
    return max(matches, key=lambda item: len(item[0].parts))


# LLM: POSIX traversal keeps every parent directory descriptor open until its child has been
# opened with O_NOFOLLOW and inode identity checked, closing parent-symlink TOCTOU windows.
# 函数用途: 从受信目录逐段无跟随地打开目标普通文件。
def _open_regular_beneath(
    anchor: Path,
    relative: Path,
    candidate: Path,
) -> OpenedArtifactSource:
    if not relative.parts:
        return _open_regular_portable(candidate, (anchor,))
    directory_fd = _open_verified_directory(anchor)
    try:
        for part in relative.parts[:-1]:
            child_fd = _openat_verified(directory_fd, part, directory=True)
            os.close(directory_fd)
            directory_fd = child_fd
        file_fd = _openat_verified(directory_fd, relative.parts[-1], directory=False)
        metadata = os.fstat(file_fd)
        return OpenedArtifactSource(candidate, file_fd, int(metadata.st_size))
    finally:
        os.close(directory_fd)


# LLM: Trusted anchors themselves are opened no-follow and compared with their lstat identity;
# an anchor replacement between inspection and open is rejected.
# 函数用途: 安全打开逐段遍历的起始目录。
def _open_verified_directory(path: Path) -> int:
    try:
        before = path.lstat()
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
            raise ArtifactSourceBoundaryError(
                "ARTIFACT_SOURCE_SYMLINK_BLOCKED",
                "trusted source root is not a regular directory",
            )
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        if not _same_file_identity(before, os.fstat(descriptor)):
            os.close(descriptor)
            raise ArtifactSourceBoundaryError(
                "ARTIFACT_SOURCE_CHANGED_DURING_OPEN",
                "trusted source root changed during open",
            )
        return descriptor
    except FileNotFoundError:
        raise
    except ArtifactSourceBoundaryError:
        raise
    except OSError as exc:
        raise ArtifactSourceBoundaryError(
            "ARTIFACT_SOURCE_PATH_UNREADABLE",
            "trusted source root cannot be opened",
        ) from exc


# LLM: Component names come only from pathlib parts and are opened relative to an already
# verified parent fd. Symlinks and inode swaps fail before a descriptor reaches callers.
# 函数用途: 在安全父目录下打开一个普通子目录或最终普通文件。
def _openat_verified(parent_fd: int, name: str, *, directory: bool) -> int:
    try:
        before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        expected = stat.S_ISDIR if directory else stat.S_ISREG
        if stat.S_ISLNK(before.st_mode) or not expected(before.st_mode):
            raise ArtifactSourceBoundaryError(
                (
                    "ARTIFACT_SOURCE_SYMLINK_BLOCKED"
                    if stat.S_ISLNK(before.st_mode)
                    else "ARTIFACT_SOURCE_NOT_REGULAR"
                ),
                "registered artifact path contains a symlink or non-regular component",
            )
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        if directory:
            flags |= getattr(os, "O_DIRECTORY", 0)
        descriptor = os.open(name, flags, dir_fd=parent_fd)
        if not _same_file_identity(before, os.fstat(descriptor)):
            os.close(descriptor)
            raise ArtifactSourceBoundaryError(
                "ARTIFACT_SOURCE_CHANGED_DURING_OPEN",
                "registered artifact path changed during open",
            )
        return descriptor
    except FileNotFoundError:
        raise
    except ArtifactSourceBoundaryError:
        raise
    except OSError as exc:
        raise ArtifactSourceBoundaryError(
            "ARTIFACT_SOURCE_PATH_UNREADABLE",
            "registered artifact path cannot be opened",
        ) from exc


# LLM: Platforms without dir_fd support keep a conservative fallback: resolved containment,
# no-follow leaf open, and lstat/fstat inode equality. Owner-scoped process execution already
# fails closed on platforms lacking the stronger sandbox.
# 函数用途: 在缺少逐段 dir_fd 能力的平台上保守打开普通文件。
def _open_regular_portable(
    candidate: Path,
    source_roots: tuple[Path, ...] | None,
) -> OpenedArtifactSource:
    try:
        before = candidate.lstat()
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            raise ArtifactSourceBoundaryError(
                "ARTIFACT_SOURCE_SYMLINK_BLOCKED",
                "registered artifact path is not a regular file",
            )
        resolved = candidate.resolve(strict=True)
        if source_roots is not None and not any(
            resolved == root or resolved.is_relative_to(root) for root in source_roots
        ):
            raise ArtifactSourceBoundaryError(
                "ARTIFACT_SOURCE_SCOPE_BLOCKED",
                "registered artifact path is outside trusted source roots",
            )
        descriptor = os.open(candidate, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        after = os.fstat(descriptor)
        if not _same_file_identity(before, after) or not stat.S_ISREG(after.st_mode):
            os.close(descriptor)
            raise ArtifactSourceBoundaryError(
                "ARTIFACT_SOURCE_CHANGED_DURING_OPEN",
                "registered artifact path changed during open",
            )
        return OpenedArtifactSource(resolved, descriptor, int(after.st_size))
    except FileNotFoundError:
        raise
    except ArtifactSourceBoundaryError:
        raise
    except OSError as exc:
        raise ArtifactSourceBoundaryError(
            "ARTIFACT_SOURCE_PATH_UNREADABLE",
            "registered artifact path cannot be opened",
        ) from exc


# LLM: Device+inode is the stable identity shared by lstat and fstat on supported platforms.
# 函数用途: 判断路径检查与文件描述符是否仍指向同一个文件系统对象。
def _same_file_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


# LLM: Validation findings may come from dataclass-like or protocol objects; keep conversion bounded to data only.
# 函数用途: 把不同校验器返回的 finding 统一转换为字典。
def _finding_to_dict(value: object) -> dict[str, Any]:
    if hasattr(value, "to_dict") and callable(value.to_dict):  # type: ignore[attr-defined]
        return dict(value.to_dict())
    if hasattr(value, "__dict__"):
        return dict(vars(value))
    return {"message": str(value)}


# LLM: Hash only a duplicate of the already-authorized descriptor; path names are never reopened.
# 函数用途: 从安全文件描述符流式计算 SHA-256 和大小。
def _sha256_opened_source(source: OpenedArtifactSource) -> tuple[str, int]:
    before = os.fstat(source.descriptor)
    digest = hashlib.sha256()
    size_bytes = 0
    duplicate = os.dup(source.descriptor)
    os.lseek(duplicate, 0, os.SEEK_SET)
    with os.fdopen(duplicate, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            size_bytes += len(chunk)
    _assert_source_copy_stable(before, os.fstat(source.descriptor), size_bytes)
    return digest.hexdigest(), size_bytes


# LLM: Deep validators receive an owner-private immutable copy made from the already-authorized
# descriptor. The `.blob` suffix prevents test discovery; declared artifact_kind preserves the
# original format selection without reopening the live path.
# 函数用途: 为命令后深度校验创建一份短生命周期、安全且不会被 pytest 收集的内容副本。
def _write_validation_copy(
    source: OpenedArtifactSource,
    backup_store_root: Path,
) -> tuple[Path, str, int]:
    directory = backup_store_root / _BACKUP_SCHEMA / ".postcheck"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    _chmod_private(directory, directory=True)
    target = directory / f".postcheck-{secrets.token_hex(16)}.blob"
    writer_fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    reader_fd = -1
    before = os.fstat(source.descriptor)
    digest = hashlib.sha256()
    size_bytes = 0
    try:
        reader_fd = os.dup(source.descriptor)
        os.lseek(reader_fd, 0, os.SEEK_SET)
        with os.fdopen(reader_fd, "rb") as reader, os.fdopen(writer_fd, "wb") as writer:
            reader_fd = -1
            writer_fd = -1
            while chunk := reader.read(1024 * 1024):
                writer.write(chunk)
                digest.update(chunk)
                size_bytes += len(chunk)
            writer.flush()
            os.fsync(writer.fileno())
        _assert_source_copy_stable(before, os.fstat(source.descriptor), size_bytes)
        _chmod_private(target, directory=False)
        return target, digest.hexdigest(), size_bytes
    except BaseException:
        if reader_fd >= 0:
            os.close(reader_fd)
        if writer_fd >= 0:
            os.close(writer_fd)
        target.unlink(missing_ok=True)
        _prune_empty_backup_dirs(directory, backup_store_root / _BACKUP_SCHEMA)
        raise


# LLM: Validation copies never carry recovery authority. Cleanup failure is bounded private debt
# and must not overwrite an otherwise authoritative postcheck result.
# 函数用途: 尽力清掉一次校验副本及空目录，失败时保留私有残片而不泄露路径或改变命令结果。
def _discard_validation_copy(path: Path, backup_store_root: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        return
    _prune_empty_backup_dirs(path.parent, backup_store_root / _BACKUP_SCHEMA)


# LLM: An in-place writer can mutate one inode while it is copied. Device/inode, size, mtime and
# ctime must stay stable and the observed byte count must equal the final stat before bytes gain
# recovery or validation authority.
# 函数用途: 确认产物在整次读取期间没有并发变化，避免哈希、大小和校验副本来自不同版本。
def _assert_source_copy_stable(
    before: os.stat_result,
    after: os.stat_result,
    copied_size: int,
) -> None:
    if (
        not _same_file_identity(before, after)
        or int(before.st_size) != int(after.st_size)
        or int(before.st_mtime_ns) != int(after.st_mtime_ns)
        or int(before.st_ctime_ns) != int(after.st_ctime_ns)
        or copied_size != int(after.st_size)
    ):
        raise OSError("artifact source changed while being copied")


# LLM: Validation copies are private implementation details. Rewrite every string field back to
# the logical artifact path before persisting findings or exposing them to the model.
# 函数用途: 把校验结果里的临时副本路径替换为用户真实产物路径。
def _logical_finding(
    finding: object,
    validation_copy: Path,
    logical_path: Path,
) -> dict[str, Any]:
    payload = _finding_to_dict(finding)
    private = str(validation_copy)
    public = str(logical_path)
    return {
        key: (value.replace(private, public) if isinstance(value, str) else value)
        for key, value in payload.items()
    }


# LLM: A changed/invalid registry row may claim recoverability only after its opaque preimage
# resolves to a regular owner-store blob whose digest exactly matches the recorded snapshot.
# 函数用途: 登记产物变更前确认恢复副本仍存在且没有被替换或篡改。
def _verify_backup_preimage(
    backup_store_root: str | Path,
    snapshot: ShellArtifactSnapshot,
) -> None:
    try:
        backup = resolve_shell_artifact_backup(backup_store_root, snapshot.backup_ref)
        opened = _open_regular_source(
            backup,
            (_lexical_absolute_path(backup_store_root),),
        )
        try:
            digest, size_bytes = _sha256_opened_source(opened)
        finally:
            opened.close()
    except (OSError, ValueError) as exc:
        raise OSError("artifact recovery preimage is unavailable") from exc
    if digest != snapshot.sha256 or size_bytes != snapshot.size_bytes:
        raise OSError("artifact recovery preimage does not match snapshot")


# LLM: Managed execution derives one stable key from the canonical ToolCall.operation_id plus
# owner/run/task/workspace facts. Unmanaged direct calls receive a nonce and cannot be reconciled.
# 函数用途: 为本次 shell 产物保护生成稳定且不可穿越目录的 operation key。
def _operation_key(
    run_scope: object,
    *,
    operation_id: str,
    tool_call_id: str,
) -> str:
    scope = run_scope if isinstance(run_scope, dict) else {}
    canonical_operation = str(operation_id or "").strip()
    if not canonical_operation:
        canonical_operation = (
            f"unmanaged:{tool_call_id}:{time.time_ns()}:{secrets.token_hex(16)}"
        )
    facts = [
        *(str(scope.get(key) or "").strip() for key in ("owner_id", "task_id", "run_id")),
        canonical_operation,
    ]
    return hashlib.sha256("\x00".join(facts).encode()).hexdigest()


# LLM: Host roots are normalized lexically, never resolved after a model-controlled process had a
# chance to replace them with symlinks. Later lstat/openat checks retain the original path identity.
# 函数用途: 把路径转成不跟随符号链接的绝对规范形式。
def _lexical_absolute_path(value: str | Path) -> Path:
    expanded = Path(value).expanduser()
    return Path(os.path.abspath(os.path.normpath(str(expanded))))


# LLM: The write is temp-file + fsync + atomic replace; a partial copy must never become a durable backup ref.
# 函数用途: 原子复制一份产物并返回最终 blob 路径、内容哈希和字节数。
def _write_atomic_backup(
    source: OpenedArtifactSource,
    operation_dir: Path,
    *,
    artifact_id: str,
) -> tuple[Path, str, int]:
    for directory in (
        operation_dir.parent.parent,
        operation_dir.parent,
        operation_dir,
    ):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        _chmod_private(directory, directory=True)
    temp_path = operation_dir / f".tmp-{secrets.token_hex(16)}"
    digest = hashlib.sha256()
    size_bytes = 0
    descriptor = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    published: Path | None = None
    before = os.fstat(source.descriptor)
    try:
        read_descriptor = os.dup(source.descriptor)
        os.lseek(read_descriptor, 0, os.SEEK_SET)
        with os.fdopen(read_descriptor, "rb") as reader, os.fdopen(descriptor, "wb") as writer:
            descriptor = -1
            while chunk := reader.read(1024 * 1024):
                writer.write(chunk)
                digest.update(chunk)
                size_bytes += len(chunk)
            writer.flush()
            os.fsync(writer.fileno())
        _assert_source_copy_stable(before, os.fstat(source.descriptor), size_bytes)
        artifact_key = hashlib.sha256(str(artifact_id).encode()).hexdigest()[:16]
        target = operation_dir / f"{artifact_key}-{digest.hexdigest()}.blob"
        os.replace(temp_path, target)
        published = target
        _chmod_private(target, directory=False)
        _fsync_directory(operation_dir)
        return target, digest.hexdigest(), size_bytes
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        temp_path.unlink(missing_ok=True)
        if published is not None:
            published.unlink(missing_ok=True)
        _prune_empty_backup_dirs(operation_dir, operation_dir.parent)
        raise


# LLM: Refs are owner-local protocol identifiers; callers must resolve them against the exact injected owner store.
# 函数用途: 把物理 blob 路径转换为可持久化但不泄露宿主目录的逻辑引用。
def _backup_ref(store_root: Path, backup_path: Path) -> str:
    relative = backup_path.relative_to(store_root).as_posix()
    return f"{_BACKUP_REF_PREFIX}{relative}"


# LLM: This is the sole resolver for shell artifact backup refs and rejects legacy absolute/traversal strings.
# 函数用途: 在指定 owner 私有根内解析 opaque ref，供复核、未来恢复和测试共同使用。
def resolve_shell_artifact_backup(
    backup_store_root: str | Path,
    backup_ref: str,
) -> Path:
    text = str(backup_ref or "").strip()
    if not text.startswith(_BACKUP_REF_PREFIX):
        raise ValueError("invalid shell artifact backup ref")
    relative = PurePosixPath(text[len(_BACKUP_REF_PREFIX) :])
    if relative.is_absolute() or not relative.parts or relative.parts[0] != _BACKUP_SCHEMA:
        raise ValueError("invalid shell artifact backup ref")
    if any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError("invalid shell artifact backup ref")
    root = _lexical_absolute_path(backup_store_root)
    target = _lexical_absolute_path(root / Path(*relative.parts))
    if not target.is_relative_to(root):
        raise ValueError("shell artifact backup ref escapes owner store")
    current = root
    for part in relative.parts[:-1]:
        current = current / part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            break
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError("shell artifact backup ref escapes owner store")
        if not stat.S_ISDIR(metadata.st_mode):
            raise ValueError("invalid shell artifact backup ref")
    return target


# LLM: Only unchanged preimages are discarded; pruning stops at schema root and never touches another operation.
# 函数用途: 删除无变化命令留下的 blob，并清掉本次已经为空的操作目录。
def _discard_backup(backup_store_root: str | Path, backup_ref: str) -> None:
    root = _lexical_absolute_path(backup_store_root)
    backup = resolve_shell_artifact_backup(root, backup_ref)
    backup.unlink(missing_ok=True)
    stop = (root / _BACKUP_SCHEMA).resolve(strict=False)
    _prune_empty_backup_dirs(backup.parent, stop)


# LLM: Cleanup may remove only empty descendants of the exact schema root; populated sibling operations survive.
# 函数用途: 从本操作目录向上删除空目录，并在 schema 根前停止。
def _prune_empty_backup_dirs(start: Path, stop: Path) -> None:
    parent = start.resolve(strict=False)
    stop = stop.resolve(strict=False)
    while parent != stop and parent.is_relative_to(stop):
        try:
            parent.rmdir()
        except OSError:
            break
        parent = parent.parent


# LLM: POSIX private modes are defense in depth; non-POSIX filesystems keep their native ACL semantics.
# 函数用途: 在支持的系统上把内部目录设为 0700、blob 设为 0600。
def _chmod_private(path: Path, *, directory: bool) -> None:
    if os.name == "posix":
        path.chmod(0o700 if directory else 0o600)


# LLM: Directory fsync closes the rename durability window where supported; unsupported platforms degrade explicitly.
# 函数用途: 尽力把 blob 的目录项同步到磁盘，不因平台不支持而破坏主链。
def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        if exc.errno in {errno.EINVAL, errno.ENOTSUP, getattr(errno, "EOPNOTSUPP", -1)}:
            return
        raise
    try:
        os.fsync(descriptor)
    except OSError as exc:
        if exc.errno not in {
            errno.EINVAL,
            errno.ENOTSUP,
            getattr(errno, "EOPNOTSUPP", -1),
        }:
            raise
    finally:
        os.close(descriptor)


__all__ = [
    "ShellArtifactSnapshot",
    "complete_shell_artifact_operation",
    "mark_shell_artifact_operation_executing",
    "reconcile_shell_artifact_operation",
    "reconcile_shell_artifact_operation_items",
    "reconcile_shell_artifacts",
    "release_shell_artifact_operation",
    "resolve_shell_artifact_backup",
    "settle_shell_artifact_operation",
    "shell_artifact_protection_note",
    "snapshot_ready_artifacts",
]
