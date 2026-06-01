from __future__ import annotations

# LLM: Home backup manifests record what would be protected before migration without copying large data eagerly.
# 模块用途: 为 owner home/schema 迁移生成轻量备份清单；后续可由外层工具按清单复制或打包。
import json
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .home_layout import MyAgentHomePaths


# LLM: HomeBackupManifest points to a backup manifest and the roots it protects.
# 类用途: 保存备份目录、manifest 路径、原因和纳入保护的根目录。
@dataclass(frozen=True)
class HomeBackupManifest:
    backup_dir: Path
    manifest_path: Path
    reason: str
    included_roots: tuple[str, ...]

# LLM: HomeBackupRestoreResult reports the files copied back from a snapshot.
# 类用途: 保存恢复目录、恢复文件数量和目标路径列表。
@dataclass(frozen=True)
class HomeBackupRestoreResult:
    backup_dir: Path
    restored_count: int
    restored_paths: tuple[str, ...]

# LLM: HomeBackupRestorePlan previews restore effects before copying files.
# 类用途: 保存 dry-run 恢复会覆盖的路径和备份是否缺失。
@dataclass(frozen=True)
class HomeBackupRestorePlan:
    backup_dir: Path
    restore_count: int
    restore_paths: tuple[str, ...]
    missing_backup: bool = False


# LLM: create_home_backup_manifest records protected roots without copying their contents.
# 函数用途: 生成 manifest-only 备份清单。
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
    manifest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return HomeBackupManifest(backup_dir=backup_dir, manifest_path=manifest_path, reason=str(reason or ""), included_roots=included)


# LLM: create_home_backup_snapshot copies owner-home metadata roots for real restore.
# 函数用途: 按 manifest 复制 memory/tasks/runs/agents/identity/index 等目录。
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


# LLM: restore_home_backup_snapshot copies snapshot files back into the current home.
# 函数用途: 执行 snapshot 恢复，返回实际恢复路径。
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


# LLM: plan_home_backup_restore previews snapshot restore targets without mutating files.
# 函数用途: 计算备份恢复会覆盖的 owner home 路径。
def plan_home_backup_restore(home: MyAgentHomePaths, backup_dir: str | Path) -> HomeBackupRestorePlan:
    root = Path(backup_dir)
    files_root = root / "files"
    if not files_root.exists():
        return HomeBackupRestorePlan(backup_dir=root, restore_count=0, restore_paths=(), missing_backup=True)
    targets = [str(home.root / source.relative_to(files_root)) for source in _snapshot_files(files_root)]
    return HomeBackupRestorePlan(backup_dir=root, restore_count=len(targets), restore_paths=tuple(targets))


# LLM: latest_home_backup_snapshots gives doctor a refs-only backup summary.
# 函数用途: 读取最近 snapshot manifest，不打开备份正文。
def latest_home_backup_snapshots(home: MyAgentHomePaths, *, limit: int = 10) -> list[dict[str, object]]:
    if not home.system_backups_dir.exists():
        return []
    rows: list[dict[str, object]] = []
    for manifest_path in sorted(home.system_backups_dir.glob("backup_*/manifest.json"), reverse=True):
        payload = _read_json_object(manifest_path)
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
    return rows


# LLM: _snapshot_files lists only files copied into a snapshot.
# 函数用途: 返回 files/ 下的所有备份文件。
def _snapshot_files(files_root: Path) -> list[Path]:
    if not files_root.exists():
        return []
    return sorted(path for path in files_root.rglob("*") if path.is_file())


# LLM: _relative_to_home keeps snapshot paths anchored under the home root.
# 函数用途: 把源路径转换成 home 相对路径，外部路径放入 _external。
def _relative_to_home(home: MyAgentHomePaths, path: Path) -> Path:
    try:
        return path.resolve().relative_to(home.root.resolve())
    except ValueError:
        return Path("_external") / path.name


# LLM: _update_manifest records snapshot mode and copied roots after copy succeeds.
# 函数用途: 容错读取并更新 manifest JSON。
def _update_manifest(path: Path, updates: dict[str, object]) -> None:
    payload = _read_json_object(path)
    if not isinstance(payload, dict):
        payload = {}
    payload.update(updates)
    payload["updated_at"] = _now_iso()
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


# LLM: _read_json_object keeps backup metadata reads tolerant of malformed manifests.
# 函数用途: 读取 JSON object，失败时返回空字典。
def _read_json_object(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


# LLM: _timestamp creates stable backup directory names.
# 函数用途: 返回 UTC 时间戳字符串。
def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


# LLM: _now_iso centralizes UTC timestamps for backup metadata.
# 函数用途: 返回当前 UTC ISO 时间字符串。
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "HomeBackupManifest",
    "HomeBackupRestoreResult",
    "HomeBackupRestorePlan",
    "create_home_backup_manifest",
    "create_home_backup_snapshot",
    "latest_home_backup_snapshots",
    "plan_home_backup_restore",
    "restore_home_backup_snapshot",
]
