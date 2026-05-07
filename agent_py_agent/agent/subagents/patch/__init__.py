# LLM: Subagent orchestration module; keep task workspace, manager facade, and report contracts stable.
# 模块用途: 支撑主代理派发、跟踪、验收、汇总子代理任务。

"""LLM contract: Patch review, apply, and rendering subpackage.

Human version:
这个子包把 patch 相关功能拆成三个独立模块：
- patch_service.py: 审核工作流决策
- patch_apply.py: 文件写入（含边界检查）
- patch_renderer.py: diff 展示
"""

from __future__ import annotations

from .patch_apply import PatchApplyOptions, PatchApplyService
from .patch_renderer import (
    build_unified_diff,
    render_patch_apply_markdown,
    render_patch_apply_record_markdown,
    render_patch_review_markdown,
    render_patch_review_record_markdown,
)
from .patch_service import PatchReviewOptions, PatchReviewService, PatchReviewTaskRequest

# LLM: 补丁参数包类型集中从这里导出，让管理器和命令行共享同一导入面。
__all__ = [
    "PatchApplyOptions",
    "PatchApplyService",
    "PatchReviewOptions",
    "PatchReviewService",
    "PatchReviewTaskRequest",
    "build_unified_diff",
    "render_patch_apply_markdown",
    "render_patch_apply_record_markdown",
    "render_patch_review_markdown",
    "render_patch_review_record_markdown",
]
