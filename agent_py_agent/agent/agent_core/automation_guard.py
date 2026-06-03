
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


class SubagentAutomationGuard:

    # 级别对应的派子代理阈值（预估轮数）
    _THRESHOLDS = {1: 2, 2: 4, 3: 8}

    def __init__(self, config: AgentConfig):
        self.level = config.subagent_automation_level
        self.threshold = self._THRESHOLDS.get(self.level, 8)

    def should_delegate(self, complexity: TaskComplexityEstimate) -> bool:
        return complexity.estimated_rounds >= self.threshold

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
