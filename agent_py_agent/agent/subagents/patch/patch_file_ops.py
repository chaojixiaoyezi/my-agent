
"""patch file write execution with rollback support.

Human version:
这个模块处理 patch 文件的实际写入和回滚操作。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..models import SubAgentTask


@dataclass(frozen=True)
class PatchFileApplyContext:
    patch_specs: list[dict]
    task: SubAgentTask
    applier: str
    note: str


def do_apply_patches(ctx: PatchFileApplyContext) -> tuple[int, dict]:
    """Perform actual patch file writes and diff building.

    Returns (applied_count, touched_files).
    """

    import time as time_module

    applied_count = 0
    now = time_module.time()
    touched_files = {}

    for spec in ctx.patch_specs:
        _apply_patch_spec(spec, ctx=ctx, now=now, touched_files=touched_files)
        applied_count += 1

    return applied_count, touched_files


def _apply_patch_spec(spec: dict, *, ctx: PatchFileApplyContext, now: float, touched_files: dict) -> None:
    from .patch_renderer import build_unified_diff

    target = spec["target"]
    before_exists = target.exists()
    before_text = target.read_text(encoding="utf-8") if before_exists else ""
    if target not in touched_files:
        touched_files[target] = {"before_exists": before_exists, "before_text": before_text}
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(spec["content"], encoding="utf-8")
    _mark_patch_applied(spec, applier=ctx.applier, now=now, note=ctx.note)
    spec["audit"]["apply_status"] = "APPLIED"
    spec["audit"]["actual_diff"] = build_unified_diff(spec["path"], before_text, spec["content"])
    spec["audit"]["message"] = "patch 已写入文件。"


def _mark_patch_applied(spec: dict, *, applier: str, now: float, note: str) -> None:
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


def rollback_patch_apply(touched_files: dict[Path, dict]) -> None:
    """Rollback patch apply by restoring original file contents."""

    for path, snapshot in touched_files.items():
        _rollback_touched_file(path, snapshot)


def _rollback_touched_file(path: Path, snapshot: dict) -> None:
    if snapshot.get("before_exists"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(snapshot.get("before_text") or ""), encoding="utf-8")
    elif path.exists():
        path.unlink()
