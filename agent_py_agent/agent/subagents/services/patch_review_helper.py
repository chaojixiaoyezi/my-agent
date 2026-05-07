"""Patch review task helper for backward-compatible inline implementation."""

from __future__ import annotations

import time
from dataclasses import dataclass

from agent_py_agent.agent.subagents.patch.patch_service import PatchReviewTaskRequest


@dataclass(frozen=True)
class PatchReviewGroups:
    """Partitioned patch groups used to make a review decision."""

    blocked: list[dict]
    invalid: list[dict]
    applied: list[dict]


class PatchReviewTaskHelper:
    """Handles inline patch review task logic for backward compatibility.

    This helper is used when _patch_review_service is not initialized.
    """

    @staticmethod
    def review_patch_task(
        request: PatchReviewTaskRequest,
    ):
        """Review a single task's patches (inline implementation)."""
        # LLM: fallback accepts the same request bundle as PatchReviewService for takeover safety.
        task = request.task
        patches = request.patches
        opts = request.options
        now = time.time()
        groups = _partition_patch_review_items(patches)
        ok = bool(patches) and not groups.blocked and not groups.invalid
        decision, message = _patch_review_decision(patches, groups, ok)

        from agent_py_agent.agent.subagents.reports import PatchReviewRecord

        return PatchReviewRecord(
            id=f"review-{task.id if hasattr(task, 'id') else 'unknown'}-{int(now)}",
            run_id=task.id if hasattr(task, "id") else "",
            dry_run=not opts.apply,
            applied=False,
            ok=ok,
            decision=decision,
            message=message,
            patch_count=len(patches),
            approved_count=len(groups.applied),
            blocked_count=len(groups.blocked),
            reviewer=opts.reviewer,
            note=opts.note,
            evidence_paths=[
                task.output_json if hasattr(task, "output_json") else "",
                task.work_log_file if hasattr(task, "work_log_file") else "",
            ],
            patches=[dict(item) for item in patches],
            created_at=now,
        )


def _partition_patch_review_items(patches) -> PatchReviewGroups:
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
    return PatchReviewGroups(blocked, invalid, applied)


def _patch_review_decision(patches, groups: PatchReviewGroups, ok):
    if not patches:
        return "NO_PATCHES", "没有 patch 需要审核。"
    if groups.blocked or groups.invalid:
        parts = []
        if groups.blocked:
            parts.append(f"{len(groups.blocked)} 个 patch 处于 planned/blocked")
        if groups.invalid:
            parts.append(f"{len(groups.invalid)} 个 patch 状态未知")
        return "REJECT", "；".join(parts) + "，不能审核通过。"
    return "APPROVE" if ok else "REJECT", f"{len(groups.applied)} 个 patch 已声明 applied，可审核通过。"
