# LLM: Agent core orchestration module; keep planning, dispatch, tool-loop, and finalization contracts stable.
# 模块用途: 支撑主代理运行循环、计划、工具调用、子代理调度和收尾。

from __future__ import annotations

"""任务规模预判模块。

基于 goal 关键词、plan 步骤数、工具数量估算任务轮数。
"""

from dataclasses import dataclass
from typing import Any


# LLM: TaskComplexityEstimate 属于 SimpleAgent 核心运行的类边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 类用途: 集中保存任务complexityestimate字段，让调用方按同一参数包传递上下文；关键副作用: 方法可能触发运行循环、工具调用、调度记录和最终响应相关副作用，需保持公开契约稳定。
@dataclass
class TaskComplexityEstimate:

    estimated_rounds: int
    estimated_input_tokens: int
    estimated_output_tokens: int
    confidence: float
    factors: dict[str, Any]


# LLM: estimate_task_complexity 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 计算任务complexity的预算、数量或限制，影响后续调度节奏；关键副作用: 主要返回派生结构或文本，需保持字段名、顺序和空值处理稳定。
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
