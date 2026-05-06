from __future__ import annotations

"""任务规模预判模块。

基于 goal 关键词、plan 步骤数、工具数量估算任务轮数。
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

    # 高复杂度关键词
    high_complexity_keywords = ["翻译", "重构", "分析", "迁移", "部署", "测试", "优化", "审查", "转换"]
    # 中等复杂度关键词
    medium_complexity_keywords = ["修改", "更新", "添加", "检查", "查找", "生成", "创建", "写入"]

    keyword_bonus = 0
    goal_lower = goal.lower()
    for kw in high_complexity_keywords:
        if kw in goal:
            keyword_bonus += 3
    for kw in medium_complexity_keywords:
        if kw in goal:
            keyword_bonus += 1

    # 工具数量加权（多工具通常意味着多步骤）
    tool_bonus = max(0, len(allowed_tools) - 1)

    estimated_rounds = base_rounds + keyword_bonus + tool_bonus

    return TaskComplexityEstimate(
        estimated_rounds=estimated_rounds,
        estimated_input_tokens=estimated_rounds * 2000,
        estimated_output_tokens=estimated_rounds * 500,
        confidence=0.6,
        factors={
            "plan_steps": len(plan),
            "keyword_bonus": keyword_bonus,
            "tool_bonus": tool_bonus,
        },
    )
