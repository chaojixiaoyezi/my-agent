"""Patch apply decision helper."""

from __future__ import annotations


class PatchApplyDecision:
    """Determine patch apply decision based on conditions."""

    @staticmethod
    def decide(patches, patch_specs, blocked_count, apply):
        """Determine patch apply decision based on conditions."""
        if not patches:
            return ("NO_PATCHES", False, "没有 patch 可以 apply。")
        if blocked_count:
            return ("REJECT", False, f"{blocked_count} 项 patch/test 不满足 apply 条件。")
        if not apply:
            return ("WOULD_APPLY", True, f"dry-run: 将 apply {len(patch_specs)} 个 patch。")
        return ("APPLIED", True, f"已 apply {len(patch_specs)} 个 patch。")