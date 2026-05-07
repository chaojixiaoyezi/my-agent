from __future__ import annotations

"""Patch manager compatibility delegate helpers."""

from dataclasses import dataclass

from .patch import PatchReviewTaskRequest
from .patch.patch_apply_task import ApplyPatchTaskParams
from .services.patch_apply_helper import PatchApplyTaskHelper
from .services.patch_review_helper import PatchReviewTaskHelper
from .services.patch_spec_normalizer import PatchApplySpecNormalizer


@dataclass(frozen=True)
class PatchReviewDelegateParams:
    """Bundle for manager patch review delegation."""

    output: dict | None = None
    patches: list[dict] | None = None
    apply: bool = False
    reviewer: str = "parent"
    note: str = ""


def review_patch_task_via_manager(
    manager,
    task,
    *,
    params: PatchReviewDelegateParams | None = None,
):
    params = params or PatchReviewDelegateParams()
    service = getattr(manager, "_patch_review_service", None)
    if service is not None:
        if isinstance(task, PatchReviewTaskRequest):
            return service._review_patch_task(task)
        return service._review_patch_task(
            task,
            output=params.output,
            patches=params.patches,
            apply=params.apply,
            reviewer=params.reviewer,
            note=params.note,
        )
    return PatchReviewTaskHelper.review_patch_task(
        task,
        output=params.output,
        patches=params.patches,
        apply=params.apply,
        reviewer=params.reviewer,
        note=params.note,
    )


def apply_patch_task_via_manager(
    manager,
    task,
    *,
    params: ApplyPatchTaskParams | None = None,
    output: dict | None = None,
    patches: list[dict] | None = None,
    apply: bool = False,
    applier: str = "parent",
    note: str = "",
):
    service = getattr(manager, "_patch_apply_service", None)
    if service is not None:
        if params is not None:
            return service._apply_patch_task(task, params=params)
        return service._apply_patch_task(
            task,
            output=output,
            patches=patches,
            apply=apply,
            applier=applier,
            note=note,
        )
    return PatchApplyTaskHelper.apply_patch_task(
        task,
        params=params,
        output=output,
        patches=patches,
        apply=apply,
        applier=applier,
        note=note,
    )


def normalize_patch_apply_spec_via_manager(manager, task, patch):
    service = getattr(manager, "_patch_apply_service", None)
    if service is not None:
        return service._normalize_patch_apply_spec(task, patch)
    return PatchApplySpecNormalizer.normalize(
        task,
        patch,
        manager.workspace_root,
        manager._build_unified_diff,
    )
