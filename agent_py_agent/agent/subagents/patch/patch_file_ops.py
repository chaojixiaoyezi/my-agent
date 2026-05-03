"""Patch file write execution with rollback support.

Human version:
这个模块处理 patch 文件的实际写入和回滚操作。
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..models import SubAgentTask


def do_apply_patches(
    patch_specs: list[dict],
    review_status_updates: list[dict],
    task: SubAgentTask,
    manager,
    applier: str,
    note: str,
) -> tuple[int, dict]:
    """Perform actual patch file writes and diff building.

    Returns (applied_count, touched_files).
    """

    import time as time_module

    from .patch_renderer import build_unified_diff

    applied_count = 0
    now = time_module.time()
    touched_files = {}

    for spec in patch_specs:
        target = spec["target"]
        before_exists = target.exists()
        before_text = target.read_text(encoding="utf-8") if before_exists else ""
        if target not in touched_files:
            touched_files[target] = {
                "before_exists": before_exists,
                "before_text": before_text,
            }
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(spec["content"], encoding="utf-8")
        spec["patch_ref"]["status"] = "applied"
        spec["patch_ref"]["apply_status"] = "APPLIED"
        spec["patch_ref"]["applied_by"] = applier
        spec["patch_ref"]["applied_at"] = now
        spec["patch_ref"]["review_status"] = "APPROVED"
        spec["patch_ref"]["reviewed_by"] = applier
        spec["patch_ref"]["reviewed_at"] = now
        if note:
            spec["patch_ref"]["apply_note"] = note
            spec["patch_ref"]["review_note"] = note
        actual_diff = build_unified_diff(
            spec["path"],
            before_text,
            spec["content"],
        )
        spec["audit"]["apply_status"] = "APPLIED"
        spec["audit"]["actual_diff"] = actual_diff
        spec["audit"]["message"] = "patch 已写入文件。"
        applied_count += 1

    return applied_count, touched_files


def rollback_patch_apply(touched_files: dict[Path, dict]) -> None:
    """Rollback patch apply by restoring original file contents."""

    for path, snapshot in touched_files.items():
        if snapshot.get("before_exists"):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(str(snapshot.get("before_text") or ""), encoding="utf-8")
        elif path.exists():
            path.unlink()
