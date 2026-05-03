"""LLM contract: Patch review, apply, and rendering subpackage.

Human version:
这个子包把 patch 相关功能拆成三个独立模块：
- patch_service.py: 审核工作流决策
- patch_apply.py: 文件写入（含边界检查）
- patch_renderer.py: diff 展示
"""

from __future__ import annotations

from .patch_apply import PatchApplyService
from .patch_renderer import (
    build_unified_diff,
    render_patch_apply_markdown,
    render_patch_apply_record_markdown,
    render_patch_review_markdown,
    render_patch_review_record_markdown,
)
from .patch_service import PatchReviewService

__all__ = [
    "PatchApplyService",
    "PatchReviewService",
    "build_unified_diff",
    "render_patch_apply_markdown",
    "render_patch_apply_record_markdown",
    "render_patch_review_markdown",
    "render_patch_review_record_markdown",
]
