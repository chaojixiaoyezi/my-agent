"""Patch review task helper for backward-compatible inline implementation."""

from __future__ import annotations

import time


class PatchReviewTaskHelper:
    """Handles inline patch review task logic for backward compatibility.

    This helper is used when _patch_review_service is not initialized.
    """

    @staticmethod
    def review_patch_task(task, *, output, patches, apply, reviewer, note):
        """Review a single task's patches (inline implementation)."""
        now = time.time()
        patch_count = len(patches)
        blocked = [
            item
            for item in patches
            if str(item.get("status", "")).lower() in {"planned", "blocked"}
        ]
        invalid = [
            item
            for item in patches
            if str(item.get("status", "")).lower() not in {"applied", "planned", "blocked"}
        ]
        applied_patches = [
            item for item in patches if str(item.get("status", "")).lower() == "applied"
        ]
        ok = bool(patches) and not blocked and not invalid
        decision = "APPROVE" if ok else "REJECT"
        if not patches:
            decision = "NO_PATCHES"
            message = "没有 patch 需要审核。"
        elif blocked or invalid:
            parts = []
            if blocked:
                parts.append(f"{len(blocked)} 个 patch 处于 planned/blocked")
            if invalid:
                parts.append(f"{len(invalid)} 个 patch 状态未知")
            message = "；".join(parts) + "，不能审核通过。"
        else:
            message = f"{len(applied_patches)} 个 patch 已声明 applied，可审核通过。"
        applied = False

        from agent_py_agent.agent.subagents.reports import PatchReviewRecord

        return PatchReviewRecord(
            id=f"review-{task.id if hasattr(task, 'id') else 'unknown'}-{int(now)}",
            run_id=task.id if hasattr(task, "id") else "",
            dry_run=not apply,
            applied=applied,
            ok=ok,
            decision=decision,
            message=message,
            patch_count=patch_count,
            approved_count=len(applied_patches),
            blocked_count=len(blocked),
        )