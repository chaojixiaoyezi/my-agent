# LLM: Parent acceptance facade methods for SubAgentManager live outside the main acceptance mixin.
# 模块用途: 承接父级验收 controller/auto/follow-up 的公开方法，保持 manager_acceptance.py 瘦身。

from __future__ import annotations

from .manager_parent_acceptance import (
    ParentAcceptanceApplyResult,
    ParentAcceptanceAutoExecutionOptions,
    ParentAcceptanceAutoExecutionResult,
    ParentAcceptanceAutoPolicy,
    ParentAcceptanceDecision,
    ParentAcceptanceFollowUpControlOptions,
    ParentAcceptanceFollowUpControlResult,
    ParentAcceptanceNextAction,
    manager_apply_parent_acceptance_decision,
    manager_apply_parent_acceptance_followup,
    manager_plan_parent_acceptance,
    manager_plan_parent_acceptance_auto_execution,
    manager_plan_parent_acceptance_auto_policy,
    manager_plan_parent_acceptance_followup,
    manager_plan_parent_acceptance_next_action,
    manager_write_parent_acceptance_decision,
)


# LLM: _ParentAcceptanceFacade is a thin compatibility surface over parent acceptance services.
# 类用途: 给 SubAgentManager 暴露父级验收、自动策略和 follow-up 方法；实际业务逻辑都在服务函数中。
class _ParentAcceptanceFacade:
    # LLM: plan_parent_acceptance is a dry-run upper-agent controller entrypoint.
    # 函数用途: 让父代理先查看验收下一步建议；只读任务事实源，不写状态、不跑命令。
    def plan_parent_acceptance(self, run_id: str) -> ParentAcceptanceDecision:
        return manager_plan_parent_acceptance(self, run_id)

    # LLM: write_parent_acceptance_decision persists the dry-run plan but does not apply it.
    # 函数用途: 写入父级验收 dry-run 决策审计文件；不执行 tests、不修改 task 状态。
    def write_parent_acceptance_decision(self, run_id: str) -> ParentAcceptanceDecision:
        return manager_write_parent_acceptance_decision(self, run_id)

    # LLM: apply_parent_acceptance_decision only bridges inspect_only into existing acceptance apply.
    # 函数用途: 显式应用父级验收决策；不安全决策只写拦截审计，不自动跑命令或救援。
    def apply_parent_acceptance_decision(
        self,
        run_id: str,
        *,
        reviewer: str = "parent",
        note: str = "",
    ) -> ParentAcceptanceApplyResult:
        return manager_apply_parent_acceptance_decision(self, run_id, reviewer=reviewer, note=note)

    # LLM: plan_parent_acceptance_next_action returns a scheduler-facing recommendation.
    # 函数用途: 为父/上级代理生成下一步显式动作建议；不运行 tests、不 rescue、不改状态。
    def plan_parent_acceptance_next_action(self, run_id: str) -> ParentAcceptanceNextAction:
        return manager_plan_parent_acceptance_next_action(self, run_id)

    # LLM: plan_parent_acceptance_auto_policy is a dry-run policy gate for future automation.
    # 函数用途: 生成父级验收自动策略审计；不执行建议动作、不改状态。
    def plan_parent_acceptance_auto_policy(self, run_id: str) -> ParentAcceptanceAutoPolicy:
        return manager_plan_parent_acceptance_auto_policy(self, run_id)

    # LLM: plan_parent_acceptance_auto_execution is guarded and dry-run unless tests are explicit.
    # 函数用途: 生成父级验收自动执行计划审计；只有 execute_tests 显式为 true 时才运行 tests。
    def plan_parent_acceptance_auto_execution(
        self,
        run_id: str,
        *,
        options: ParentAcceptanceAutoExecutionOptions | None = None,
    ) -> ParentAcceptanceAutoExecutionResult:
        return manager_plan_parent_acceptance_auto_execution(self, run_id, options=options)

    # LLM: plan_parent_acceptance_followup previews the post-test manual gate without mutation.
    # 函数用途: 读取 follow-up 文件并展示下一步显式命令；不 apply、不 rescue。
    def plan_parent_acceptance_followup(self, run_id: str) -> ParentAcceptanceFollowUpControlResult:
        return manager_plan_parent_acceptance_followup(self, run_id)

    # LLM: apply_parent_acceptance_followup is the controlled manual gate after explicit tests.
    # 函数用途: 显式处理 follow-up；通过 tests 时 apply，失败 tests 时走受控接管入口。
    def apply_parent_acceptance_followup(
        self,
        run_id: str,
        *,
        options: ParentAcceptanceFollowUpControlOptions | None = None,
    ) -> ParentAcceptanceFollowUpControlResult:
        return manager_apply_parent_acceptance_followup(self, run_id, options=options)
