# LLM: Agent core orchestration module; keep planning, dispatch, tool-loop, and finalization contracts stable.
# 模块用途: 支撑主代理运行循环、计划、工具调用、子代理调度和收尾。

from __future__ import annotations

"""主代理代劳防护模块。

防止主代理在高自动化级别下绕过子代理直接执行多轮任务。
"""

import logging
from typing import TYPE_CHECKING

from .task_complexity import TaskComplexityEstimate

if TYPE_CHECKING:
    from ..settings.config import AgentConfig


logger = logging.getLogger(__name__)


# LLM: SubagentAutomationGuard 属于 SimpleAgent 核心运行的类边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 类用途: 封装子代理automation保护相关状态和行为，维持当前模块的职责边界；关键副作用: 方法可能触发运行循环、工具调用、调度记录和最终响应相关副作用，需保持公开契约稳定。
class SubagentAutomationGuard:

    # 级别对应的派子代理阈值（预估轮数）
    _THRESHOLDS = {1: 2, 2: 4, 3: 8}

    # LLM: __init__ 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 初始化实例依赖和配置字段，为后续方法调用准备共享状态；关键副作用: 需保持运行循环、工具调用、调度记录和最终响应上的返回值和副作用边界稳定。
    def __init__(self, config: AgentConfig):
        self.level = config.subagent_automation_level
        self.threshold = self._THRESHOLDS.get(self.level, 8)

    # LLM: should_delegate 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 判断delegate条件是否成立，作为后续调度或分支决策的门禁；关键副作用: 主要返回判断或抛出明确异常，调用方依赖布尔语义稳定。
    def should_delegate(self, complexity: TaskComplexityEstimate) -> bool:
        return complexity.estimated_rounds >= self.threshold

    # LLM: warn_if_not_delegating 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 处理warnifnotdelegating相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持运行循环、工具调用、调度记录和最终响应上的返回值和副作用边界稳定。
    def warn_if_not_delegating(
        self,
        complexity: TaskComplexityEstimate,
        delegated: bool,
    ) -> None:
        if self.should_delegate(complexity) and not delegated:
            logger.warning(
                f"自动化级别 {self.level}：任务预估 {complexity.estimated_rounds} 轮，"
                f"超过阈值 {self.threshold}，建议派子代理完成。"
            )
