"""Dispatch 循环闭环保证机制。

循环调用 dispatch_subagents，直到没有可调度的任务或达到最大轮数上限。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..subagent import SubAgent
    from ..capabilities import CapabilityRouter
    from ..capability_config import CapabilityConfig


@dataclass
class DispatchLoopReport:
    """Dispatch 循环报告。"""

    rounds_count: int = 0
    total_records: int = 0
    final_pending_count: int = 0
    stopped_by_limit: bool = False
    rounds: list[dict] = field(default_factory=list)


def dispatch_loop(
    agent,
    router: "CapabilityRouter",
    capability_config: "CapabilityConfig | None" = None,
    *,
    max_consecutive_rounds: int = 20,
    apply: bool = False,
    execute_runners: bool = False,
    planner: bool = False,
    workflow_mode: str = "off",
    max_runners: int = 1,
    limit: int = 20,
    reviewer: str = "parent-dispatch",
    note: str = "",
    runner_instruction: str = "",
    max_cards: int = 0,
    probe: bool = True,
    take_over_by: str = "",
    locked_files: list[str] | None = None,
) -> DispatchLoopReport:
    """循环执行 dispatch 直到没有可调度任务或达到上限。

    Args:
        agent: SimpleAgent 实例（包含 dispatch_subagents 方法）
        router: CapabilityRouter 实例
        capability_config: CapabilityConfig 实例
        max_consecutive_rounds: 最大连续调度轮数
        其他参数同 dispatch_subagents

    Returns:
        DispatchLoopReport 包含轮数、记录总数和最终待处理数
    """
    report = DispatchLoopReport()
    max_rounds = max_consecutive_rounds

    for round_num in range(1, max_rounds + 1):
        # 执行一轮 dispatch
        dispatch_report = agent.dispatch_subagents(
            router,
            capability_config,
            apply=apply,
            execute_runners=execute_runners,
            planner=planner,
            workflow_mode=workflow_mode,
            max_runners=max_runners,
            limit=limit,
            reviewer=reviewer,
            note=note,
            runner_instruction=runner_instruction,
            max_cards=max_cards,
            probe=probe,
            take_over_by=take_over_by,
            locked_files=locked_files or [],
        )

        # 更新报告
        report.rounds_count = round_num
        report.total_records += len(dispatch_report.records)
        report.rounds.append({
            "round": round_num,
            "record_count": len(dispatch_report.records),
            "ok": all(item.ok for item in dispatch_report.records),
        })

        # 检查是否有待处理工作
        if not agent._has_pending_work:
            # 没有可调度任务，退出循环
            break

        # 检查是否达到最大轮数限制
        if round_num >= max_rounds:
            report.stopped_by_limit = True
            break

    # 获取最终待处理数
    runner_max_attempts = 2  # 默认值
    try:
        from .runner_dispatch import _runner_max_attempts, _dispatch_runner_candidates

        runner_max_attempts = _runner_max_attempts(agent.config.runner_failure_policy)
        candidates = _dispatch_runner_candidates(
            agent.subagents.list_runs(),
            max_runners=999,
            runner_max_attempts=runner_max_attempts,
        )
        report.final_pending_count = len(candidates)
    except Exception:
        report.final_pending_count = 0

    return report


__all__ = ["DispatchLoopReport", "dispatch_loop"]