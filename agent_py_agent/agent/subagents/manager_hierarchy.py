# LLM: Subagent hierarchy manager facade; keep scheduling bundle-only and explicit.
# 模块用途: 给 SubAgentManager 增加受控创建子/孙代理的入口，不让 runner 文本直接生成层级任务。

from __future__ import annotations

"""Explicit hierarchy scheduling facade for subagent task trees."""

from .services.hierarchy_recovery import (
    HierarchyRecoveryRequest,
    HierarchyRecoveryResult,
    SubAgentHierarchyRecoveryService,
)
from .services.hierarchy_scheduler import (
    HierarchyScheduleRequest,
    HierarchyScheduleResult,
    SubAgentHierarchyScheduler,
)


# LLM: SubAgentHierarchyMixin keeps hierarchy scheduling as a small manager capability.
# 类用途: 提供 schedule_child_runs 方法，调用方必须传入 HierarchyScheduleRequest bundle。
class SubAgentHierarchyMixin:
    """Facade for explicit child-run scheduling."""

    # LLM: schedule_child_runs materializes or previews descendants through the hierarchy scheduler.
    # 函数用途: 按 bundle 请求 dry-run 或创建子/孙代理，并复用已有任务持久化路径。
    def schedule_child_runs(self, *, params: HierarchyScheduleRequest) -> HierarchyScheduleResult:
        return SubAgentHierarchyScheduler(self).schedule_children(params)

    # LLM: build_hierarchy_recovery_packet summarizes a nested task tree without reading artifact bodies.
    # 函数用途: 为 root run 生成多层恢复交接包，列出需要接管/恢复的子孙节点和 refs。
    def build_hierarchy_recovery_packet(self, *, params: HierarchyRecoveryRequest) -> HierarchyRecoveryResult:
        return SubAgentHierarchyRecoveryService(self).build_packet(params)
