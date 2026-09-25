from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..common.json_io import read_json_object, read_json_object_report, write_json_object
from .home_layout import MyAgentHomePaths


@dataclass(frozen=True)
class HomeBackupManifest:
    backup_dir: Path
    manifest_path: Path
    reason: str
    included_roots: tuple[str, ...]

@dataclass(frozen=True)
class HomeBackupRestoreResult:
    backup_dir: Path
    restored_count: int
    restored_paths: tuple[str, ...]

@dataclass(frozen=True)
class HomeBackupRestorePlan:
    backup_dir: Path
    restore_count: int
    restore_paths: tuple[str, ...]
    missing_backup: bool = False


@dataclass(frozen=True)
class HomeBackupSnapshotsReport:
    snapshots: list[dict[str, object]]
    load_errors: list[dict[str, object]]


def create_home_backup_manifest(home: MyAgentHomePaths, *, reason: str = "") -> HomeBackupManifest:
    backup_dir = home.system_backups_dir / f"backup_{_timestamp()}"
    included = (
        str(home.owner_memory_dir),
        str(home.owner_tasks_dir),
        str(home.owner_runs_dir),
        str(home.owner_agents_dir),
        str(home.identity_dir),
        str(home.global_index_dir),
    )
    payload = {
        "schema_version": "home-backup-manifest.v1",
        "reason": str(reason or ""),
        "created_at": _now_iso(),
        "home_root": str(home.root),
        "included_roots": list(included),
        "mode": "manifest_only",
    }
    backup_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = backup_dir / "manifest.json"
    write_json_object(manifest_path, payload)
    return HomeBackupManifest(backup_dir=backup_dir, manifest_path=manifest_path, reason=str(reason or ""), included_roots=included)


def create_home_backup_snapshot(home: MyAgentHomePaths, *, reason: str = "") -> HomeBackupManifest:
    manifest = create_home_backup_manifest(home, reason=reason)
    files_root = manifest.backup_dir / "files"
    copied: list[str] = []
    for root_text in manifest.included_roots:
        source = Path(root_text)
        if not source.exists():
            continue
        rel = _relative_to_home(home, source)
        target = files_root / rel
        if source.is_dir():
            shutil.copytree(source, target, dirs_exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        copied.append(str(rel))
    # Phase 4B(学 参考实现):存每文件 sha256+字节数清单,供恢复前完整性校验(防备份损坏/被篡改)。
    _update_manifest(
        manifest.manifest_path,
        {"mode": "snapshot", "copied_roots": copied, "checksums": _compute_checksums(files_root)},
    )
    return manifest


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _compute_checksums(files_root: Path) -> dict[str, dict[str, object]]:
    """备份区每文件 → {sha256, size}(相对 files_root 的路径为键)。"""
    out: dict[str, dict[str, object]] = {}
    for path in _snapshot_files(files_root):
        out[str(path.relative_to(files_root))] = {"sha256": _file_sha256(path), "size": path.stat().st_size}
    return out


@dataclass(frozen=True)
class HomeBackupVerifyResult:
    backup_dir: Path
    ok: bool
    checked: int
    mismatches: tuple[str, ...]  # 哈希/大小不符或缺失的文件相对路径


def verify_home_backup_snapshot(backup_dir: str | Path) -> HomeBackupVerifyResult:
    """恢复前完整性校验:重算备份区每文件 sha256/size 与 manifest 比对(学 参考实现 恢复前校验)。"""
    root = Path(backup_dir)
    files_root = root / "files"
    manifest = read_json_object(root / "manifest.json")
    recorded = manifest.get("checksums") if isinstance(manifest, dict) else {}
    recorded = recorded if isinstance(recorded, dict) else {}
    mismatches: list[str] = []
    for rel, meta in recorded.items():
        target = files_root / rel
        if not target.is_file():
            mismatches.append(rel)
            continue
        if not isinstance(meta, dict) or _file_sha256(target) != str(meta.get("sha256", "")):
            mismatches.append(rel)
    return HomeBackupVerifyResult(
        backup_dir=root, ok=not mismatches, checked=len(recorded), mismatches=tuple(mismatches)
    )


def restore_home_backup_snapshot_checked(home: MyAgentHomePaths, backup_dir: str | Path) -> HomeBackupRestoreResult:
    """先完整性校验、通过才恢复(fail-closed:校验不过抛 ValueError,绝不用损坏备份覆盖现状)。"""
    verdict = verify_home_backup_snapshot(backup_dir)
    if not verdict.ok:
        raise ValueError(f"备份完整性校验未通过,拒绝恢复;问题文件:{list(verdict.mismatches)[:5]}")
    return restore_home_backup_snapshot(home, backup_dir)


def restore_home_backup_snapshot(home: MyAgentHomePaths, backup_dir: str | Path) -> HomeBackupRestoreResult:
    plan = plan_home_backup_restore(home, backup_dir)
    root = plan.backup_dir
    if plan.missing_backup:
        return HomeBackupRestoreResult(backup_dir=root, restored_count=0, restored_paths=())
    files_root = root / "files"
    restored: list[str] = []
    for source in _snapshot_files(files_root):
        if not source.is_file():
            continue
        rel = source.relative_to(files_root)
        target = home.root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        restored.append(str(target))
    return HomeBackupRestoreResult(backup_dir=root, restored_count=len(restored), restored_paths=tuple(restored))


def plan_home_backup_restore(home: MyAgentHomePaths, backup_dir: str | Path) -> HomeBackupRestorePlan:
    root = Path(backup_dir)
    files_root = root / "files"
    if not files_root.exists():
        return HomeBackupRestorePlan(backup_dir=root, restore_count=0, restore_paths=(), missing_backup=True)
    targets = [str(home.root / source.relative_to(files_root)) for source in _snapshot_files(files_root)]
    return HomeBackupRestorePlan(backup_dir=root, restore_count=len(targets), restore_paths=tuple(targets))


def latest_home_backup_snapshots(home: MyAgentHomePaths, *, limit: int = 10) -> list[dict[str, object]]:
    return latest_home_backup_snapshots_report(home, limit=limit).snapshots


def latest_home_backup_snapshots_report(home: MyAgentHomePaths, *, limit: int = 10) -> HomeBackupSnapshotsReport:
    if not home.system_backups_dir.exists():
        return HomeBackupSnapshotsReport([], [])
    rows: list[dict[str, object]] = []
    load_errors: list[dict[str, object]] = []
    for manifest_path in sorted(home.system_backups_dir.glob("backup_*/manifest.json"), reverse=True):
        report = read_json_object_report(manifest_path, context="home_backup.manifest")
        if report.load_error:
            load_errors.append(report.load_error)
        payload = report.payload
        if payload.get("mode") != "snapshot":
            continue
        rows.append(
            {
                "backup_dir": str(manifest_path.parent),
                "manifest_path": str(manifest_path),
                "created_at": str(payload.get("created_at") or ""),
                "updated_at": str(payload.get("updated_at") or ""),
                "reason": str(payload.get("reason") or ""),
                "copied_roots": list(payload.get("copied_roots") or []),
            }
        )
        if limit > 0 and len(rows) >= limit:
            break
    return HomeBackupSnapshotsReport(rows, load_errors)


def _snapshot_files(files_root: Path) -> list[Path]:
    if not files_root.exists():
        return []
    return sorted(path for path in files_root.rglob("*") if path.is_file())


def _relative_to_home(home: MyAgentHomePaths, path: Path) -> Path:
    try:
        return path.resolve().relative_to(home.root.resolve())
    except ValueError:
        return Path("_external") / path.name


def _update_manifest(path: Path, updates: dict[str, object]) -> None:
    payload = read_json_object(path)
    if not isinstance(payload, dict):
        payload = {}
    payload.update(updates)
    payload["updated_at"] = _now_iso()
    write_json_object(path, payload)


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "HomeBackupManifest",
    "HomeBackupSnapshotsReport",
    "HomeBackupRestoreResult",
    "HomeBackupRestorePlan",
    "HomeBackupVerifyResult",
    "create_home_backup_manifest",
    "create_home_backup_snapshot",
    "latest_home_backup_snapshots",
    "latest_home_backup_snapshots_report",
    "plan_home_backup_restore",
    "restore_home_backup_snapshot",
    "restore_home_backup_snapshot_checked",
    "verify_home_backup_snapshot",
]
