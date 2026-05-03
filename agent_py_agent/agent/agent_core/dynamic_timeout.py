from __future__ import annotations

"""LLM: dynamic timeout calculation based on model speed profile.

给人看的解释：
根据输入大小和速度模型计算动态超时。
避免小任务和大任务用同一个超时时间。
"""

import math
from pathlib import Path
from typing import TYPE_CHECKING

from ..model_speed import SpeedProfile, load_speed_profile

if TYPE_CHECKING:
    from ..settings import AgentConfig


def calculate_dynamic_timeout(
    config: AgentConfig,
    estimated_input_tokens: int = 0,
    estimated_output_tokens: int = 0,
    safety_margin: float | None = None,
    min_timeout: float | None = None,
    max_timeout: float | None = None,
) -> float:
    """根据输入大小和速度模型计算动态超时。

    Args:
        config: Agent 配置
        estimated_input_tokens: 预估输入 token 数
        estimated_output_tokens: 预估输出 token 数
        safety_margin: 安全边际系数，默认使用配置值
        min_timeout: 最小超时秒数，默认使用配置值
        max_timeout: 最大超时秒数，默认使用配置值

    Returns:
        动态超时秒数
    """

    effective_safety_margin = safety_margin if safety_margin is not None else config.dynamic_timeout_safety_margin
    effective_min_timeout = min_timeout if min_timeout is not None else float(config.dynamic_timeout_min)
    effective_max_timeout = max_timeout if max_timeout is not None else float(config.dynamic_timeout_max)

    # 尝试加载速度模型
    speed_profile_path = Path(config.model_speed_profile_path)
    speed_profile = load_speed_profile(speed_profile_path)

    if speed_profile is None or not speed_profile.samples:
        # 没有速度模型时，使用经验公式
        # 基础时间：每 1000 token 给 10 秒，加上安全边际
        total_tokens = estimated_input_tokens + estimated_output_tokens
        if total_tokens == 0:
            total_tokens = 2000  # 默认假设 2K token
        base_latency = (total_tokens / 1000) * 10.0
    else:
        # 使用速度模型插值
        base_latency = speed_profile.interpolate(estimated_input_tokens, estimated_output_tokens)

    # 应用安全边际
    timeout = base_latency * effective_safety_margin

    # 应用下限和上限
    return max(effective_min_timeout, min(effective_max_timeout, timeout))


def estimate_tokens_from_text(text: str) -> int:
    """粗略估算文本的 token 数。

    这是一个简化估算，不是精确的 tokenizer。
    英文大约 4 字符 = 1 token，中文大约 1.5 字符 = 1 token。
    """

    if not text:
        return 0

    # 统计中文字符数
    chinese_chars = sum(1 for c in text if "一" <= c <= "鿿")
    # 统计非中文字符数
    other_chars = len(text) - chinese_chars

    # 粗略估算
    chinese_tokens = int(chinese_chars / 1.5)
    other_tokens = int(other_chars / 4)

    return chinese_tokens + other_tokens


def estimate_task_tokens(goal: str, plan: list[str] | None = None) -> tuple[int, int]:
    """根据任务描述和计划估算输入和输出 token 数。

    Args:
        goal: 任务目标描述
        plan: 计划步骤列表

    Returns:
        (estimated_input_tokens, estimated_output_tokens)
    """

    # 估算输入 token：goal + plan
    plan_text = " ".join(plan) if plan else ""
    input_text = goal + " " + plan_text
    estimated_input_tokens = estimate_tokens_from_text(input_text)

    # 估算输出 token：根据输入大小按一定比例估算
    # 通常输出是输入的 20%-50%
    output_ratio = 0.3
    estimated_output_tokens = max(500, int(estimated_input_tokens * output_ratio))

    return estimated_input_tokens, estimated_output_tokens
