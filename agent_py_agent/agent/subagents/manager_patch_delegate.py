# LLM: Subagent orchestration module; keep task workspace, manager facade, and report contracts stable.
# 模块用途: 支撑主代理派发、跟踪、验收、汇总子代理任务。

from __future__ import annotations

"""Patch manager compatibility delegate helpers."""

from dataclasses import dataclass

from .patch import PatchReviewOptions, PatchReviewTaskRequest
from .patch.patch_apply_task import ApplyPatchTaskParams
from .services.patch_apply_helper import PatchApplyTaskHelper
from .services.patch_review_helper import PatchReviewTaskHelper
from .services.patch_spec_normalizer import PatchApplySpecNormalizer


# LLM: PatchReviewDelegateParams 属于子代理任务管理的类边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 类用途: 集中保存补丁审查delegate参数字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass(frozen=True)
class PatchReviewDelegateParams:
    """Bundle for manager patch review delegation."""

    output: dict | None = None
    patches: list[dict] | None = None
    apply: bool = False
    reviewer: str = "parent"
    note: str = ""


# LLM: review_patch_task_via_manager 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 处理审查补丁任务via管理器相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、执行器结果、验收和报告展示上的返回值和副作用边界稳定。
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
            PatchReviewTaskRequest(
                task=task,
                output=params.output or {},
                patches=params.patches or [],
                options=PatchReviewOptions(apply=params.apply, reviewer=params.reviewer, note=params.note),
            )
        )
    return PatchReviewTaskHelper.review_patch_task(
        task if isinstance(task, PatchReviewTaskRequest) else PatchReviewTaskRequest(
            task=task,
            output=params.output or {},
            patches=params.patches or [],
            options=PatchReviewOptions(apply=params.apply, reviewer=params.reviewer, note=params.note),
        ),
    )


# LLM: apply_patch_task_via_manager 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 更新补丁任务via管理器对应的任务或运行状态，并保留既有字段语义；关键副作用: 会更新任务状态、执行器结果、验收和报告展示，需避免破坏既有状态机约定。
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


# LLM: normalize_patch_apply_spec_via_manager 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 解析并归一化补丁应用specvia管理器的输入形态，让下游只处理稳定结构；关键副作用: 会更新任务状态、执行器结果、验收和报告展示，需避免破坏既有状态机约定。
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
