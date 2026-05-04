"""Patch apply task helper for backward-compatible inline implementation."""

from __future__ import annotations

import time

from agent_py_agent.agent.subagents.reports import PatchApplyRecord


class PatchApplyTaskHelper:
    """Handles inline patch apply task logic for backward compatibility.

    This helper is used when _patch_apply_service is not initialized.
    """

    @staticmethod
    def apply_patch_task(task, *, output, patches, apply, applier, note):
        """Apply patches for a single task (inline minimal implementation)."""
        now = time.time()
        blocked = [
            item for item in patches
            if str(item.get("status", "")).lower() in {"planned", "blocked"}
        ]
        invalid = [
            item for item in patches
            if str(item.get("status", "")).lower() not in {"applied", "planned", "blocked"}
        ]
        ok = not blocked and not invalid
        decision = "APPLY" if ok else "BLOCK"
        if not patches:
            decision = "NO_PATCHES"
        return PatchApplyRecord(
            id=f"apply-{task.id if hasattr(task, 'id') else 'unknown'}-{int(now)}",
            run_id=task.id if hasattr(task, "id") else "",
            dry_run=not apply,
            applied=False,
            ok=ok,
            decision=decision,
            message=f"{len(patches)} patches, {len(blocked)} blocked, {len(invalid)} invalid",
            patch_count=len(patches),
            applied_count=0,
            blocked_count=len(blocked),
            test_commands=[],
            test_results=[],
            patches=[],
            created_at=now,
        )