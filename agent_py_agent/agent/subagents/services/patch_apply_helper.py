"""Patch apply task helper for backward-compatible inline implementation."""

from __future__ import annotations

import time

from agent_py_agent.agent.subagents.reports import PatchApplyRecord


class PatchApplyTaskHelper:
    """Handles inline patch apply task logic for backward compatibility.

    This helper is used when _patch_apply_service is not initialized.
    """

    @staticmethod
    def apply_patch_task(
        task,
        *,
        params=None,
        output: dict | None = None,
        patches: list[dict] | None = None,
        apply: bool = False,
        applier: str = "parent",
        note: str = "",
    ):
        """Apply patches for a single task (inline minimal implementation)."""
        # LLM: fallback mirrors the bundle-first service path when the service is not initialized.
        apply, applier, note, patches = _coerce_apply_patch_inputs(
            params,
            apply=apply,
            applier=applier,
            note=note,
            patches=patches,
        )

        now = time.time()
        blocked, invalid = _blocked_and_invalid_patch_items(patches)
        ok = not blocked and not invalid
        return PatchApplyRecord(
            id=f"apply-{task.id if hasattr(task, 'id') else 'unknown'}-{int(now)}",
            run_id=task.id if hasattr(task, "id") else "",
            dry_run=not apply,
            applied=False,
            ok=ok,
            decision=_patch_apply_decision(patches, ok),
            message=f"{len(patches)} patches, {len(blocked)} blocked, {len(invalid)} invalid",
            patch_count=len(patches),
            applied_count=0,
            blocked_count=len(blocked),
            applier=applier,
            note=note,
            evidence_paths=[
                task.output_json if hasattr(task, "output_json") else "",
                task.work_log_file if hasattr(task, "work_log_file") else "",
            ],
            test_commands=[],
            test_results=[],
            patches=[],
            created_at=now,
        )


def _coerce_apply_patch_inputs(params, *, apply, applier, note, patches):
    if params is None:
        return apply, applier, note, patches or []
    return params.apply, params.applier, params.note, params.patches


def _blocked_and_invalid_patch_items(patches):
    blocked = [
        item for item in patches
        if str(item.get("status", "")).lower() in {"planned", "blocked"}
    ]
    invalid = [
        item for item in patches
        if str(item.get("status", "")).lower() not in {"applied", "planned", "blocked"}
    ]
    return blocked, invalid


def _patch_apply_decision(patches, ok):
    if not patches:
        return "NO_PATCHES"
    return "APPLY" if ok else "BLOCK"
