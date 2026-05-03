from __future__ import annotations

"""LLM contract: SubAgentPatchMixin - thin facade delegating to patch services.

Human version:
这个 mixin 是 SubAgentManager 的一块业务能力，不单独实例化。
已拆分为 patch_service.py, patch_apply.py, patch_renderer.py。
本文件只做委托，不再包含业务逻辑。
"""

from pathlib import Path
from typing import TYPE_CHECKING

from .patch import PatchApplyService, PatchReviewService

if TYPE_CHECKING:
    from ..local_store import LocalStore


class SubAgentPatchMixin:
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
        apply=False,
        reviewer="parent",
        note="",
        limit=0,
    ):
        """Review runner output patches."""

        return self._patch_review_service.review_patches(
            run_ids,
            apply=apply,
            reviewer=reviewer,
            note=note,
            limit=limit,
        )

    def write_patch_review_report(
        self,
        run_ids=None,
        *,
        apply=False,
        reviewer="parent",
        note="",
        limit=0,
    ):
        """Write patch review report to disk."""

        return self._patch_review_service.write_review_report(
            run_ids,
            apply=apply,
            reviewer=reviewer,
            note=note,
            limit=limit,
        )

    def apply_patches(
        self,
        run_ids=None,
        *,
        apply=False,
        applier="parent",
        note="",
        limit=0,
    ):
        """Execute patch apply audit chain."""

        return self._patch_apply_service.apply_patches(
            run_ids,
            apply=apply,
            applier=applier,
            note=note,
            limit=limit,
        )

    def write_patch_apply_report(
        self,
        run_ids=None,
        *,
        apply=False,
        applier="parent",
        note="",
        limit=0,
    ):
        """Write patch apply report to disk."""

        return self._patch_apply_service.write_apply_report(
            run_ids,
            apply=apply,
            applier=applier,
            note=note,
            limit=limit,
        )

    def resolve_patch_target(self, raw_path):
        """Public patch target path resolution (delegated to apply service)."""

        return self._patch_apply_service._resolve_patch_target(raw_path)

    def _resolve_patch_target(self, raw_path):
        """Private patch target path resolution (for backward compatibility).

        This is an inline implementation that mimics patch_apply._resolve_patch_target.
        Tests use this directly without calling _init_patch_services.
        """
        target = Path(raw_path).expanduser()
        if not target.is_absolute():
            target = self.workspace_root / target
        return target.resolve(strict=False)
