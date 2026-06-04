from __future__ import annotations

"""Patch review/apply service for SubAgentManager."""

from dataclasses import dataclass

from ...patch import (
    PatchApplyOptions,
    PatchApplyService,
    PatchReviewOptions,
    PatchReviewService,
    PatchReviewTaskRequest,
    build_unified_diff,
)
from ...patch.patch_apply_helpers import validate_patch_test_command
from ...patch.patch_apply_task import ApplyPatchTaskParams


@dataclass(frozen=True)
class PatchReviewTaskParams:
    """Bundle for one-task patch review."""

    output: dict | None = None
    patches: list[dict] | None = None
    apply: bool = False
    reviewer: str = "parent"
    note: str = ""


class SubAgentPatchService:
    """Coordinate patch review/apply services."""

    def __init__(self, manager):
        self.manager = manager
        self.review_service = PatchReviewService(manager)
        self.apply_service = PatchApplyService(manager)
        manager._patch_review_service = self.review_service
        manager._patch_apply_service = self.apply_service

    def review_patches(
        self,
        run_ids=None,
        *,
        options: PatchReviewOptions | None = None,
        apply=False,
        reviewer="parent",
        note="",
        limit=0,
    ):
        return self.review_service.review_patches(
            run_ids,
            options=options,
            apply=apply,
            reviewer=reviewer,
            note=note,
            limit=limit,
        )

    def write_patch_review_report(
        self,
        run_ids=None,
        *,
        options: PatchReviewOptions | None = None,
        apply=False,
        reviewer="parent",
        note="",
        limit=0,
    ):
        return self.review_service.write_review_report(
            run_ids,
            options=options,
            apply=apply,
            reviewer=reviewer,
            note=note,
            limit=limit,
        )

    def apply_patches(
        self,
        run_ids=None,
        *,
        options: PatchApplyOptions | None = None,
        apply=False,
        applier="parent",
        note="",
        limit=0,
    ):
        return self.apply_service.apply_patches(
            run_ids,
            options=options,
            apply=apply,
            applier=applier,
            note=note,
            limit=limit,
        )

    def write_patch_apply_report(
        self,
        run_ids=None,
        *,
        options: PatchApplyOptions | None = None,
        apply=False,
        applier="parent",
        note="",
        limit=0,
    ):
        return self.apply_service.write_apply_report(
            run_ids,
            options=options,
            apply=apply,
            applier=applier,
            note=note,
            limit=limit,
        )

    def resolve_patch_target(self, raw_path):
        return self.apply_service._resolve_patch_target(raw_path)

    def _resolve_patch_target(self, raw_path):
        return self.resolve_patch_target(raw_path)

    @staticmethod
    def _build_unified_diff(path: str, before_text: str, after_text: str) -> str:
        return build_unified_diff(path, before_text, after_text)

    @staticmethod
    def _validate_patch_test_command(command: str) -> str:
        return validate_patch_test_command(command)

    def _review_patch_task(
        self,
        task,
        *,
        params: PatchReviewTaskParams | None = None,
        output: dict | None = None,
        patches: list[dict] | None = None,
        apply: bool = False,
        reviewer: str = "parent",
        note: str = "",
    ):
        review_params = params or PatchReviewTaskParams(
            output=output,
            patches=patches,
            apply=apply,
            reviewer=reviewer,
            note=note,
        )
        if isinstance(task, PatchReviewTaskRequest):
            return self.review_service._review_patch_task(task)
        return self.review_service._review_patch_task(
            PatchReviewTaskRequest(
                task=task,
                output=review_params.output or {},
                patches=review_params.patches or [],
                options=PatchReviewOptions(
                    apply=review_params.apply,
                    reviewer=review_params.reviewer,
                    note=review_params.note,
                ),
            )
        )

    def _apply_patch_task(
        self,
        task,
        *,
        params: ApplyPatchTaskParams | None = None,
        output: dict | None = None,
        patches: list[dict] | None = None,
        apply: bool = False,
        applier: str = "parent",
        note: str = "",
    ):
        return self.apply_service._apply_patch_task(
            task,
            params=params,
            output=output,
            patches=patches,
            apply=apply,
            applier=applier,
            note=note,
        )

    def _normalize_patch_apply_spec(self, task, patch):
        return self.apply_service._normalize_patch_apply_spec(task, patch)

    @staticmethod
    def _rollback_patch_apply(touched_files):
        from ...patch.patch_file_ops import rollback_patch_apply

        rollback_patch_apply(touched_files)
