from __future__ import annotations

"""LLM contract: SubAgentPatchMixin - thin facade delegating to patch services.

Human version:
这个 mixin 是 SubAgentManager 的一块业务能力，不单独实例化。
已拆分为 patch_service.py, patch_apply.py, patch_renderer.py。
本文件只做委托，不再包含业务逻辑。
"""

from typing import TYPE_CHECKING

from .manager_patch_delegate import (
    PatchReviewDelegateParams,
    apply_patch_task_via_manager,
    normalize_patch_apply_spec_via_manager,
    review_patch_task_via_manager,
)
from .patch import (
    PatchApplyOptions,
    PatchApplyService,
    PatchReviewOptions,
    PatchReviewService,
)
from .patch.patch_apply_helpers import extract_patch_test_command, validate_patch_test_command
from .patch.patch_apply_task import ApplyPatchTaskParams
from .patch.patch_renderer import build_unified_diff
from .utils import _read_json_object  # noqa: F401 - re-exported for backward compat

# LLM: patch manager forwards bundle options to services; parsing helpers keep this mixin below soft size.
if TYPE_CHECKING:
    from ..local_store import LocalStore


class _SubAgentPatchFacade:
    """Thin facade for patch review, apply, and diff rendering.

    All actual logic is delegated to PatchReviewService and PatchApplyService.
    """

    def _init_patch_services(self):
        """Initialize patch services after manager construction."""
        self._patch_review_service = PatchReviewService(self)
        self._patch_apply_service = PatchApplyService(self)

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
        """Review runner output patches."""

        return self._patch_review_service.review_patches(
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
        """Write patch review report to disk."""

        return self._patch_review_service.write_review_report(
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
        """Execute patch apply audit chain."""

        return self._patch_apply_service.apply_patches(
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
        """Write patch apply report to disk."""

        return self._patch_apply_service.write_apply_report(
            run_ids,
            options=options,
            apply=apply,
            applier=applier,
            note=note,
            limit=limit,
        )

    def resolve_patch_target(self, raw_path):
        """Public patch target path resolution (delegated to apply service)."""

        if hasattr(self, "_patch_apply_service") and self._patch_apply_service is not None:
            return self._patch_apply_service._resolve_patch_target(raw_path)
        return self._resolve_patch_target(raw_path)

    def _resolve_patch_target(self, raw_path):
        """Private patch target path resolution (for backward compatibility)."""
        return _resolve_patch_target_path(raw_path, self.workspace_root)

    @staticmethod
    def _build_unified_diff(path: str, before_text: str, after_text: str) -> str:
        return build_unified_diff(path, before_text, after_text)

    @staticmethod
    def _extract_patch_test_command(check: str) -> str:
        return extract_patch_test_command(check)

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
        """Review a single task's patches (delegated to patch review service)."""
        return review_patch_task_via_manager(
            self,
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
        """Apply patches for a single task (delegated to patch apply service)."""
        return apply_patch_task_via_manager(
            self,
            task,
            params=params,
            output=output,
            patches=patches,
            apply=apply,
            applier=applier,
            note=note,
        )

    def _normalize_patch_apply_spec(self, task, patch):
        """Normalize patch apply spec (delegated to patch apply service)."""
        return normalize_patch_apply_spec_via_manager(self, task, patch)

    @staticmethod
    def _rollback_patch_apply(touched_files):
        from .patch.patch_file_ops import rollback_patch_apply

        rollback_patch_apply(touched_files)


class SubAgentPatchMixin(_SubAgentPatchFacade):
    """Public compatibility mixin; patch behavior stays in the facade class."""


def _resolve_patch_target_path(raw_path, workspace_root):
    from pathlib import Path

    target = Path(raw_path).expanduser()
    if not target.is_absolute():
        target = workspace_root / target
    return target.resolve(strict=False)
