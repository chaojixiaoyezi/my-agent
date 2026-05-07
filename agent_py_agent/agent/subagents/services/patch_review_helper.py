"""Patch review task helper for backward-compatible inline implementation."""

from __future__ import annotations

import time


class PatchReviewTaskHelper:
    """Handles inline patch review task logic for backward compatibility.

    This helper is used when _patch_review_service is not initialized.
    """

    @staticmethod
    def review_patch_task(
        task,
        **legacy,
    ):
        """Review a single task's patches (inline implementation)."""
        # LLM: fallback accepts the same request bundle as PatchReviewService for takeover safety.
        task, patches, apply, reviewer, note = _coerce_review_inputs(task, legacy)
        now = time.time()
        blocked, invalid, applied_patches = _partition_patch_review_items(patches)
        ok = bool(patches) and not blocked and not invalid
        decision, message = _patch_review_decision(patches, blocked, invalid, applied_patches, ok)

        from agent_py_agent.agent.subagents.reports import PatchReviewRecord

        return PatchReviewRecord(
            id=f"review-{task.id if hasattr(task, 'id') else 'unknown'}-{int(now)}",
            run_id=task.id if hasattr(task, "id") else "",
            dry_run=not apply,
            applied=False,
            ok=ok,
            decision=decision,
            message=message,
            patch_count=len(patches),
            approved_count=len(applied_patches),
            blocked_count=len(blocked),
            reviewer=reviewer,
            note=note,
            evidence_paths=[
                task.output_json if hasattr(task, "output_json") else "",
                task.work_log_file if hasattr(task, "work_log_file") else "",
            ],
            patches=[dict(item) for item in patches],
            created_at=now,
        )


def _coerce_review_inputs(task, legacy):
    from agent_py_agent.agent.subagents.patch.patch_service import PatchReviewTaskRequest

    if isinstance(task, PatchReviewTaskRequest):
        request = task
        return (
            request.task,
            request.patches,
            request.options.apply,
            request.options.reviewer,
            request.options.note,
        )
    return (
        task,
        legacy.get("patches") or [],
        bool(legacy.get("apply", False)),
        legacy.get("reviewer", "parent"),
        legacy.get("note", ""),
    )


def _partition_patch_review_items(patches):
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
    applied = [item for item in patches if str(item.get("status", "")).lower() == "applied"]
    return blocked, invalid, applied


def _patch_review_decision(patches, blocked, invalid, applied_patches, ok):
    if not patches:
        return "NO_PATCHES", "没有 patch 需要审核。"
    if blocked or invalid:
        parts = []
        if blocked:
            parts.append(f"{len(blocked)} 个 patch 处于 planned/blocked")
        if invalid:
            parts.append(f"{len(invalid)} 个 patch 状态未知")
        return "REJECT", "；".join(parts) + "，不能审核通过。"
    return "APPROVE" if ok else "REJECT", f"{len(applied_patches)} 个 patch 已声明 applied，可审核通过。"
