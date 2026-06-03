
from __future__ import annotations

"""任务规模预判模块。

只基于结构化 plan 步骤数和工具数量估算任务轮数；不从用户自然语言关键词猜业务复杂度。
"""

from dataclasses import dataclass
from typing import Any


@dataclass
class TaskComplexityEstimate:

    estimated_rounds: int
    estimated_input_tokens: int
    estimated_output_tokens: int
    confidence: float
    factors: dict[str, Any]


def estimate_task_complexity(
    goal: str,
    plan: list[str],
    allowed_tools: list[str],
) -> TaskComplexityEstimate:
    # 基础分：plan 步骤数，最小为1
    base_rounds = max(1, len(plan))

    # 工具数量加权（多工具通常意味着多步骤）
    tool_bonus = max(0, len(allowed_tools) - 1)

    estimated_rounds = base_rounds + tool_bonus

    return TaskComplexityEstimate(
        estimated_rounds=estimated_rounds,
        estimated_input_tokens=estimated_rounds * 2000,
        estimated_output_tokens=estimated_rounds * 500,
        confidence=0.6,
        factors={
            "plan_steps": len(plan),
            "goal_signal_bonus": 0,
            "keyword_bonus": 0,
            "tool_bonus": tool_bonus,
        },
    )
