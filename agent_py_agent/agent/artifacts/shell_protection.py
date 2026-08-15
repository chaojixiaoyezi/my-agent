
"""Snapshot registered artifacts around shell execution.

Human version:
Shell can run arbitrary scripts inside its allowed access mode, so it cannot use
the same pre-write validation path as write_file. This module protects existing
ready artifacts by copying them before shell starts and reconciling their status
after shell exits.
"""

from __future__ import annotations

import os
import shutil
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from agent_py_agent.agent.contracts.artifact_acceptance import (
    ArtifactAcceptanceRequest,
    validate_artifact,
)

from .registry import ArtifactRegistration, latest_artifact_records, register_artifact


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

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def snapshot_ready_artifacts(workspace_root: str | Path) -> list[ShellArtifactSnapshot]:
    """Copy current ready artifacts so shell damage has a restore reference."""

    root = Path(workspace_root).expanduser().resolve(strict=False)
    records = latest_artifact_records(root)
    backup_root = root / "data" / "artifacts" / "shell_backups" / str(time.time_ns())
    snapshots: list[ShellArtifactSnapshot] = []
    for record in records.values():
        if str(record.status or "") != "ready":
            continue
        path = Path(record.path).expanduser().resolve(strict=False)
        if not path.is_file():
            continue
        backup = backup_root / _safe_name(record.artifact_id) / path.name
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, backup)
        snapshots.append(
            ShellArtifactSnapshot(
                artifact_id=record.artifact_id,
                run_id=record.run_id,
                task_id=record.task_id,
                agent_id=record.agent_id,
                kind=record.kind,
                mime_type=record.mime_type,
                path=str(path),
                sha256=record.sha256,
                size_bytes=record.size_bytes,
                backup_ref=str(backup),
            )
        )
    return snapshots


def reconcile_shell_artifacts(
    workspace_root: str | Path,
    snapshots: list[ShellArtifactSnapshot],
) -> dict[str, Any]:
    """Update registry entries for shell-changed artifacts and return a prompt-safe summary."""

    root = Path(workspace_root).expanduser().resolve(strict=False)
    changed: list[dict[str, Any]] = []
    invalid: list[dict[str, Any]] = []
    for snapshot in snapshots:
        path = Path(snapshot.path).expanduser().resolve(strict=False)
        status, findings = _post_shell_status(path, root, snapshot)
        if status == "unchanged":
            continue
        registered = register_artifact(
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
                status="ready" if status == "changed_ready" else "invalid",
                metadata={
                    "change_status": status,
                    "previous_sha256": snapshot.sha256,
                    "previous_size_bytes": snapshot.size_bytes,
                    "backup_ref": snapshot.backup_ref,
                    "findings": findings,
                },
            )
        )
        item = {
            "artifact_id": snapshot.artifact_id,
            "path": str(path),
            "status": registered.status,
            "change_status": status,
            "backup_ref": snapshot.backup_ref,
            "finding_codes": [str(finding.get("code") or "") for finding in findings if finding.get("code")],
        }
        changed.append(item)
        if registered.status == "invalid":
            invalid.append(item)
    return {
        "snapshots": len(snapshots),
        "changed": changed,
        "invalid": invalid,
    }


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


def _post_shell_status(
    path: Path,
    workspace_root: Path,
    snapshot: ShellArtifactSnapshot,
) -> tuple[str, list[dict[str, Any]]]:
    if not path.exists():
        return "missing_after_shell", [{"code": "ARTIFACT_MISSING_AFTER_SHELL", "message": "artifact path is missing"}]
    if not path.is_file():
        return "invalid_after_shell", [{"code": "ARTIFACT_NOT_FILE_AFTER_SHELL", "message": "artifact path is not a file"}]
    current_sha = _sha256_file(path)
    if current_sha == snapshot.sha256:
        return "unchanged", []
    if path.stat().st_size == 0:
        return "invalid_after_shell", [{"code": "ARTIFACT_EMPTY_AFTER_SHELL", "message": "artifact is empty"}]
    report = validate_artifact(ArtifactAcceptanceRequest(path=path, workspace_root=workspace_root))
    findings = [_finding_to_dict(finding) for finding in report.findings]
    return ("changed_ready" if report.ok else "invalid_after_shell", findings)


def _finding_to_dict(value: object) -> dict[str, Any]:
    if hasattr(value, "to_dict") and callable(value.to_dict):  # type: ignore[attr-defined]
        return dict(value.to_dict())
    if hasattr(value, "__dict__"):
        return dict(vars(value))
    return {"message": str(value)}


def _sha256_file(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_name(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in value)
    return cleaned or "artifact"


# 方案 Z(2026-08-15 3×3 死循环根治): 命令失败但工作区用户可见文件零变化时,
# 执行器声明 effect=not_started(重做安全), 不误判 UNKNOWN。
#
# 双席 seq2103/2104 阻断项 1-3 修正(2026-08-15):
# - 快照完整性: 遍历/stat/相对路径转换任一失败 → 整体 None(绝不产出部分清单,
#   部分清单相等后不得推出 not_started——违反 IO 失败保守语义)。
# - 排除范围: 只排除 connector-owned 的根级 .sandbox-tmp(执行器沙箱挂载点,
#   其写入是执行器自身的临时产物); 不再按 basename 在任意深度排除
#   .git/__pycache__ 等(形成盲区——命令改版本库内部可逃过检测)。
# - 逃逸检测: 清单记录 (文件类型, size, mtime_ns, symlink_target, 小文件
#   sha256)——写入后恢复同样 size/mtime、替换 symlink、hardlink 改写都能被
#   检测; 小文件(≤1MB, 源码场景全命中)加内容 digest 防「改内容+恢复 mtime」
#   逃逸。快照信号是判定输入, 不单独作为因果证明(safe_to_retry 由上层裁决)。
_SNAPSHOT_EXCLUDE_DIRS = frozenset({".sandbox-tmp"})
# 快照上限: workspace 极大(>50000 文件)时跳过快照, 保守不声明(沿通用合同)。
_SNAPSHOT_MAX_FILES = 50000
# 内容 digest 阈值: ≤1MB 的文件记录 sha256(源码/配置场景全命中, 防
# 「改内容后恢复 size/mtime」逃逸); 大文件只记 size/mtime(保守方向: 无法
# 证明内容未变时由上层按快照属性决定, 不影响完整性标记)。
_SNAPSHOT_CONTENT_DIGEST_MAX_BYTES = 1_000_000


def snapshot_workspace_tree(root: str | Path) -> dict[str, tuple] | None:
    """执行前快照 workspace 文件+目录清单: relpath -> (kind, size, mtime_ns, sha256, link_target)。

    返回 None 表示快照不可用(目录缺失/超上限/**任一 IO/遍历/转换失败**)——
    调用方必须保守, 不基于该信号声明 not_started。绝不出部分清单:
    一个文件 stat 失败即整体 None(双席 seq2103 阻断项 1)。

    双席 seq2118 补充: ①目录项也进 manifest("dir:" 前缀条目)——mkdir/rmdir
    空目录变化可检测(mkdir empty && exit 1 不再漏判); ②特殊文件(socket/
    fifo/device, 非 regular 非 symlink)→ 快照整体 None(不可证明); ③
    .sandbox-tmp 只按**根级**排除(canonical 根路径, 非任意深度 basename——
    用户子目录建同名目录不能形成盲区)。
    """
    try:
        base = Path(root).expanduser().resolve(strict=False)
        if not base.is_dir():
            return None
        manifest: dict[str, tuple] = {}
        for dirpath, dirnames, filenames in os.walk(base, onerror=_snapshot_walk_error):
            dirpath_path = Path(dirpath)
            # 根级 .sandbox-tmp 排除: 相对根的第一段 == .sandbox-tmp 才排除
            # (connector-owned 沙箱挂载点); 嵌套同名目录照常纳入(防盲区)。
            dirnames[:] = [
                d
                for d in dirnames
                if not (
                    d == ".sandbox-tmp"
                    and dirpath_path.relative_to(base).parts == ()
                )
            ]
            # 目录项进 manifest(mkdir/rmdir 检测)
            try:
                rel_dir = dirpath_path.relative_to(base)
            except ValueError:
                return None
            if rel_dir.parts:
                try:
                    dstat = dirpath_path.lstat()
                except OSError:
                    return None
                manifest["dir:" + str(rel_dir)] = (
                    "dir",
                    int(dstat.st_size),
                    int(dstat.st_mtime_ns),
                    "",
                    "",
                )
            for filename in filenames:
                path = dirpath_path / filename
                try:
                    lstat = path.lstat()
                    rel = path.relative_to(base)
                except (OSError, ValueError):
                    return None
                mode_type = lstat.st_mode & 0o170000
                if mode_type == 0o120000:  # symlink
                    entry: tuple = (
                        "symlink",
                        int(lstat.st_size),
                        int(lstat.st_mtime_ns),
                        "",
                        "",
                    )
                    try:
                        entry = entry[:4] + (str(path.readlink()),)
                    except OSError:
                        return None
                elif mode_type == 0o100000:  # regular file
                    entry = (
                        "file",
                        int(lstat.st_size),
                        int(lstat.st_mtime_ns),
                        "",
                        "",
                    )
                    if int(lstat.st_size) <= _SNAPSHOT_CONTENT_DIGEST_MAX_BYTES:
                        try:
                            entry = entry[:3] + (_sha256_file(path), "") + entry[4:]
                        except OSError:
                            return None
                else:
                    # 特殊文件(socket/fifo/device/dir): 不可证明内容未变 → 整体 None
                    return None
                manifest[str(rel)] = entry
                if len(manifest) > _SNAPSHOT_MAX_FILES:
                    return None
        # 双席 seq2118: 大文件(>1MB, 无内容 digest)占比过半 → 内容未变不可
        # 证明 → 整体 None(保守, 不基于 size/mtime 推断内容未变)。
        file_entries = [v for k, v in manifest.items() if not k.startswith("dir:")]
        digest_coverage = sum(1 for v in file_entries if v[3])
        if file_entries and digest_coverage * 2 < len(file_entries):
            return None
        return manifest
    except (OSError, ValueError):
        return None


def _snapshot_walk_error(_exc: OSError) -> None:
    """os.walk 遍历失败必须使快照整体失败——onerror 回调里抛错中断遍历。"""
    raise _exc


def workspace_tree_unchanged(
    before: dict[str, tuple] | None,
    after: dict[str, tuple] | None,
) -> bool:
    """执行前后清单一致 = 用户可见文件零变化(结构化, 不解析命令输出)。

    必须 before/after 都非 None 才可能返回 True; 任一缺失返回 False(保守:
    快照不可用时不得据此声明 not_started)。清单含文件类型/symlink 目标/
    小文件内容 digest——写入后恢复 size/mtime、替换 symlink 均判变化。
    """
    if before is None or after is None:
        return False
    return before == after


def snapshot_digest_coverage(manifest: dict[str, tuple] | None) -> dict[str, object]:
    """快照的证据强度元数据(双席 seq2127: 未 hash 大文件不能作为通用安全证明)。

    返回 digest_coverage_pct + unhashed_count + unhashed_paths(前 10)——调用方
    写入 effect contract, 供 supervisor/safe_to_retry 层裁决「部分证明」的
    信任度; 不把 (size, mtime) 相等当作内容未变的因果证明。
    """
    if not manifest:
        return {
            "snapshot_available": bool(manifest),
            "digest_coverage_pct": 0,
            "unhashed_count": 0,
            "unhashed_paths": [],
        }
    file_entries = [k for k, v in manifest.items() if not k.startswith("dir:")]
    unhashed = [k for k in file_entries if not manifest[k][3]]
    total = len(file_entries)
    return {
        "snapshot_available": True,
        "digest_coverage_pct": round(100 * (total - len(unhashed)) / total) if total else 100,
        "unhashed_count": len(unhashed),
        "unhashed_paths": unhashed[:10],
    }


__all__ = [
    "ShellArtifactSnapshot",
    "reconcile_shell_artifacts",
    "shell_artifact_protection_note",
    "snapshot_ready_artifacts",
]
