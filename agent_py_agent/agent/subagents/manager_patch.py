# LLM: Subagent orchestration module; keep task workspace, manager facade, and report contracts stable.
# 模块用途: 支撑主代理派发、跟踪、验收、汇总子代理任务。

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
from .patch.patch_apply_helpers import validate_patch_test_command
from .patch.patch_apply_task import ApplyPatchTaskParams
from .patch.patch_renderer import build_unified_diff
from .utils import _read_json_object  # noqa: F401 - re-exported for backward compat

# LLM: 补丁管理器只转发归一选项，解析细节留给辅助函数以控制混入体积。
if TYPE_CHECKING:
    from ..local_store import LocalStore


# LLM: _SubAgentPatchFacade 属于子代理任务管理的类边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 类用途: 拆分subagent补丁门面流程片段，复用宿主对象上的状态和服务依赖；关键副作用: 方法可能触发任务状态、执行器结果、验收和报告展示相关副作用，需保持公开契约稳定。
class _SubAgentPatchFacade:
    """Thin facade for patch review, apply, and diff rendering.

    All actual logic is delegated to PatchReviewService and PatchApplyService.
    """

    # LLM: _init_patch_services 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 处理init补丁services相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、执行器结果、验收和报告展示上的返回值和副作用边界稳定。
    def _init_patch_services(self):
        """Initialize patch services after manager construction."""
        self._patch_review_service = PatchReviewService(self)
        self._patch_apply_service = PatchApplyService(self)

    # LLM: review_patches 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 处理审查patches相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、执行器结果、验收和报告展示上的返回值和副作用边界稳定。
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

    # LLM: write_patch_review_report 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 写入补丁审查报告的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动任务状态、执行器结果、验收和报告展示，调用方依赖写入顺序和文件格式。
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

    # LLM: apply_patches 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 更新patches对应的任务或运行状态，并保留既有字段语义；关键副作用: 会更新任务状态、执行器结果、验收和报告展示，需避免破坏既有状态机约定。
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

    # LLM: write_patch_apply_report 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 写入补丁应用报告的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动任务状态、执行器结果、验收和报告展示，调用方依赖写入顺序和文件格式。
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

    # LLM: resolve_patch_target 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 读取或查询补丁target需要的状态，返回调用方可继续处理的快照；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
    def resolve_patch_target(self, raw_path):
        """Public patch target path resolution (delegated to apply service)."""

        if hasattr(self, "_patch_apply_service") and self._patch_apply_service is not None:
            return self._patch_apply_service._resolve_patch_target(raw_path)
        return self._resolve_patch_target(raw_path)

    # LLM: _resolve_patch_target 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 读取或查询补丁target需要的状态，返回调用方可继续处理的快照；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
    def _resolve_patch_target(self, raw_path):
        """Private patch target path resolution (for backward compatibility)."""
        return _resolve_patch_target_path(raw_path, self.workspace_root)

    # LLM: _build_unified_diff 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 构建unifieddiff所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 主要返回派生结构或文本，需保持字段名、顺序和空值处理稳定。
    @staticmethod
    def _build_unified_diff(path: str, before_text: str, after_text: str) -> str:
        return build_unified_diff(path, before_text, after_text)

    # LLM: _validate_patch_test_command 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 校验补丁testcommand需要的输入和状态，不满足时把错误明确反馈给调用方；关键副作用: 主要返回判断或抛出明确异常，调用方依赖布尔语义稳定。
    @staticmethod
    def _validate_patch_test_command(command: str) -> str:
        return validate_patch_test_command(command)

    # LLM: _review_patch_task 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 处理审查补丁任务相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、执行器结果、验收和报告展示上的返回值和副作用边界稳定。
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

    # LLM: _apply_patch_task 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 更新补丁任务对应的任务或运行状态，并保留既有字段语义；关键副作用: 会更新任务状态、执行器结果、验收和报告展示，需避免破坏既有状态机约定。
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

    # LLM: _normalize_patch_apply_spec 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 解析并归一化补丁应用spec的输入形态，让下游只处理稳定结构；关键副作用: 会更新任务状态、执行器结果、验收和报告展示，需避免破坏既有状态机约定。
    def _normalize_patch_apply_spec(self, task, patch):
        """Normalize patch apply spec (delegated to patch apply service)."""
        return normalize_patch_apply_spec_via_manager(self, task, patch)

    # LLM: _rollback_patch_apply 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 处理rollback补丁应用相关的数据流，连接当前职责的前后步骤；关键副作用: 会更新任务状态、执行器结果、验收和报告展示，需避免破坏既有状态机约定。
    @staticmethod
    def _rollback_patch_apply(touched_files):
        from .patch.patch_file_ops import rollback_patch_apply

        rollback_patch_apply(touched_files)


# LLM: SubAgentPatchMixin 属于子代理任务管理的类边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 类用途: 拆分subagent补丁混入流程片段，复用宿主对象上的状态和服务依赖；关键副作用: 方法可能触发任务状态、执行器结果、验收和报告展示相关副作用，需保持公开契约稳定。
class SubAgentPatchMixin(_SubAgentPatchFacade):
    """Public compatibility mixin; patch behavior stays in the facade class."""


# LLM: _resolve_patch_target_path 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 读取或查询补丁target路径需要的状态，返回调用方可继续处理的快照；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
def _resolve_patch_target_path(raw_path, workspace_root):
    from pathlib import Path

    target = Path(raw_path).expanduser()
    if not target.is_absolute():
        target = workspace_root / target
    return target.resolve(strict=False)
