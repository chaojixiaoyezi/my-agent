# LLM: Compact layout keeps task/run/agent recovery packages structurally identical.
# 模块用途: 创建 compact_0001 这类压缩包基础文件，避免三层恢复包各自长出不同形状。

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..io import append_jsonl


@dataclass(frozen=True)
class CompactPackagePaths:
    root: Path
    package_dir: Path
    ledger_jsonl: Path
    latest_symlink: Path
    latest_pointer: Path
    compact_context_md: Path
    handoff_summary_md: Path
    work_state_snapshot_json: Path
    continue_packet_json: Path
    refs_json: Path
    metadata_json: Path


def ensure_compact_package(root: str | Path, *, compact_index: int, scope: str) -> CompactPackagePaths:
    paths = compact_package_paths(root, compact_index=compact_index)
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.package_dir.mkdir(parents=True, exist_ok=True)
    _write_seed_file(paths.compact_context_md, "# Compact Context\n\n")
    _write_seed_file(paths.handoff_summary_md, "# Handoff Summary\n\n")
    _write_seed_json(paths.work_state_snapshot_json, {"schema_version": "work-state-snapshot.v1", "scope": scope})
    _write_seed_json(paths.continue_packet_json, _continue_packet_payload(scope=scope))
    _write_seed_json(paths.refs_json, {"schema_version": "compact-refs.v1", "refs": []})
    _write_seed_json(paths.metadata_json, _metadata_payload(scope=scope, compact_index=compact_index))
    _write_latest(paths)
    append_jsonl(
        paths.ledger_jsonl,
        {
            "event_type": "compact_package_initialized",
            "compact_index": int(compact_index),
            "package_dir": str(paths.package_dir),
            "scope": scope,
            "created_at": _now_iso(),
        },
        sort_keys=True,
    )
    return paths


def compact_package_paths(root: str | Path, *, compact_index: int) -> CompactPackagePaths:
    compact_root = Path(root)
    package_dir = compact_root / f"compact_{int(compact_index):04d}"
    return CompactPackagePaths(
        root=compact_root,
        package_dir=package_dir,
        ledger_jsonl=compact_root / "compact_ledger.jsonl",
        latest_symlink=compact_root / "latest",
        latest_pointer=compact_root / "latest.txt",
        compact_context_md=package_dir / "compact_context.md",
        handoff_summary_md=package_dir / "handoff_summary.md",
        work_state_snapshot_json=package_dir / "work_state_snapshot.json",
        continue_packet_json=package_dir / "continue_packet.json",
        refs_json=package_dir / "refs.json",
        metadata_json=package_dir / "metadata.json",
    )


def _continue_packet_payload(*, scope: str) -> dict[str, object]:
    return {
        "schema_version": "continue-packet.v1",
        "scope": scope,
        "next_action": "",
        "completed_headings": [],
        "avoid_repeating": [],
        "active_refs": [],
        "pending_work": [],
    }


def _metadata_payload(*, scope: str, compact_index: int) -> dict[str, object]:
    return {
        "schema_version": "compact-package.v1",
        "scope": scope,
        "compact_index": int(compact_index),
        "created_at": _now_iso(),
    }


def _write_latest(paths: CompactPackagePaths) -> None:
    try:
        if paths.latest_symlink.exists() or paths.latest_symlink.is_symlink():
            paths.latest_symlink.unlink()
        paths.latest_symlink.symlink_to(paths.package_dir.name)
    except OSError:
        paths.latest_pointer.write_text(paths.package_dir.name + "\n", encoding="utf-8")


def _write_seed_file(path: Path, content: str) -> None:
    if not path.exists():
        path.write_text(content, encoding="utf-8")


def _write_seed_json(path: Path, payload: dict[str, object]) -> None:
    if not path.exists():
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = ["CompactPackagePaths", "compact_package_paths", "ensure_compact_package"]
