from __future__ import annotations

"""Patch review/apply facade service for SubAgentManager."""

from ...manager_patch_delegate import (
    PatchReviewDelegateParams,
    apply_patch_task_via_manager,
    normalize_patch_apply_spec_via_manager,
    review_patch_task_via_manager,
)
from ...patch import (
    PatchApplyOptions,
    PatchApplyService,
    PatchReviewOptions,
    PatchReviewService,
    build_unified_diff,
)
from ...patch.patch_apply_helpers import validate_patch_test_command
from ...patch.patch_apply_task import ApplyPatchTaskParams


class SubAgentPatchService:
    """Coordinate patch review/apply services behind the manager facade."""

    def __init__(self, manager):
        self.manager = manager
        self.review_service = PatchReviewService(manager)
        self.apply_service = PatchApplyService(manager)
        # Compatibility for helper functions and older tests that still probe these attrs.
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
        params: PatchReviewDelegateParams | None = None,
        output: dict | None = None,
        patches: list[dict] | None = None,
        apply: bool = False,
        reviewer: str = "parent",
        note: str = "",
    ):
        return review_patch_task_via_manager(
            self.manager,
            task,
            params=params or PatchReviewDelegateParams(
                output=output,
                patches=patches,
                apply=apply,
                reviewer=reviewer,
                note=note,
            ),
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
        return apply_patch_task_via_manager(
            self.manager,
            task,
            params=params,
            output=output,
            patches=patches,
            apply=apply,
            applier=applier,
            note=note,
        )

    def _normalize_patch_apply_spec(self, task, patch):
        return normalize_patch_apply_spec_via_manager(self.manager, task, patch)

    @staticmethod
    def _rollback_patch_apply(touched_files):
        from ...patch.patch_file_ops import rollback_patch_apply

        rollback_patch_apply(touched_files)
