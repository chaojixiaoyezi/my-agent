from __future__ import annotations

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
    _update_manifest(manifest.manifest_path, {"mode": "snapshot", "copied_roots": copied})
    return manifest


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
    "create_home_backup_manifest",
    "create_home_backup_snapshot",
    "latest_home_backup_snapshots",
    "latest_home_backup_snapshots_report",
    "plan_home_backup_restore",
    "restore_home_backup_snapshot",
]
