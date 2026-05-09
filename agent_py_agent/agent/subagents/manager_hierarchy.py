# LLM: Subagent hierarchy manager facade; keep scheduling bundle-only and explicit.
# 模块用途: 给 SubAgentManager 增加受控创建子/孙代理的入口，不让 runner 文本直接生成层级任务。

from __future__ import annotations

"""Explicit hierarchy scheduling facade for subagent task trees."""

from .models import SubAgentLeadershipRecoveryPlanOptions
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
from .services.leadership_recovery import (
    LeadershipRecoveryPlanReport,
    SubAgentLeadershipRecoveryPlanner,
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

    # LLM: plan_leadership_recovery previews batch handoffs for stale coordinators without mutating the tree.
    # 函数用途: 为批量 coordinator 挂掉场景生成 leader 分摊计划，当前只读不执行。
    def plan_leadership_recovery(
        self,
        *,
        params: SubAgentLeadershipRecoveryPlanOptions,
    ) -> LeadershipRecoveryPlanReport:
        return SubAgentLeadershipRecoveryPlanner(self).plan(params)

    # LLM: write_leadership_recovery_plan persists the same dry-run report requested by CLI/handoff tools.
    # 函数用途: 写入批量领导权恢复计划 JSON 和 Markdown，方便外部评审与人工确认。
    def write_leadership_recovery_plan(
        self,
        *,
        params: SubAgentLeadershipRecoveryPlanOptions,
    ) -> LeadershipRecoveryPlanReport:
        import json
        from dataclasses import asdict

        report = self.plan_leadership_recovery(params=params)
        (self.workspace / "subagent_leadership_recovery_plan.json").write_text(
            json.dumps(asdict(report), ensure_ascii=False, indent=2), encoding="utf-8",
        )
        (self.workspace / "SUBAGENT_LEADERSHIP_RECOVERY_PLAN.md").write_text(
            _render_leadership_recovery_plan(report), encoding="utf-8",
        )
        return report


# LLM: _render_leadership_recovery_plan keeps the human report refs-only and compact.
# 函数用途: 将批量接管计划转成 Markdown 摘要，不读取任何子任务 artifact 正文。
def _render_leadership_recovery_plan(report: LeadershipRecoveryPlanReport) -> str:
    lines = [
        "# Subagent Leadership Recovery Plan",
        "",
        f"- root_id: `{report.root_id}`",
        f"- max_children_per_leader: `{report.max_children_per_leader}`",
        f"- summary: `{report.summary}`",
        "",
        "## Assignments",
    ]
    if not report.assignments:
        lines.append("- none")
    for item in report.assignments:
        lines.append(
            f"- coordinator `{item.coordinator_id}` -> leader `{item.leader_id}` "
            f"children={item.child_ids} apply_supported={item.apply_supported}"
        )
    lines.extend(["", "## Unassigned"])
    if not report.unassigned:
        lines.append("- none")
    for item in report.unassigned:
        lines.append(f"- coordinator `{item.coordinator_id}` children={item.child_ids} reason={item.reason}")
    return "\n".join(lines) + "\n"
