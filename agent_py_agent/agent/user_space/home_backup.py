# LLM: Home backup manifests record what would be protected before migration without copying large data eagerly.
# 模块用途: 为 owner home/schema 迁移生成轻量备份清单；后续可由外层工具按清单复制或打包。

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .home_layout import MyAgentHomePaths


@dataclass(frozen=True)
class HomeBackupManifest:
    backup_dir: Path
    manifest_path: Path
    reason: str
    included_roots: tuple[str, ...]


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


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = ["HomeBackupManifest", "create_home_backup_manifest"]
