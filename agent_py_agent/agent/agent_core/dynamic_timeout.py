# LLM: Agent core orchestration module; keep planning, dispatch, tool-loop, and finalization contracts stable.
# 模块用途: 支撑主代理运行循环、计划、工具调用、子代理调度和收尾。

from __future__ import annotations

"""dynamic timeout calculation based on model speed profile.

根据输入大小和速度模型计算动态超时。
避免小任务和大任务用同一个超时时间。
"""

import math
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ..model_speed import SpeedProfile, load_speed_profile

if TYPE_CHECKING:
    from ..settings import AgentConfig


# LLM: DynamicTimeoutParams 属于 SimpleAgent 核心运行的类边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 类用途: 集中保存动态超时参数字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass(frozen=True)
class DynamicTimeoutParams:
    # LLM: 超时输入集中成一个参数包，后续模型速度字段不会撑大公开接口。
    estimated_input_tokens: int = 0
    estimated_output_tokens: int = 0
    safety_margin: float | None = None
    min_timeout: float | None = None
    max_timeout: float | None = None


# LLM: calculate_dynamic_timeout 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 计算动态超时的预算、数量或限制，影响后续调度节奏；关键副作用: 主要返回派生结构或文本，需保持字段名、顺序和空值处理稳定。
def calculate_dynamic_timeout(
    config: AgentConfig,
    estimated_input_tokens: int = 0,
    estimated_output_tokens: int = 0,
    *,
    safety_margin: float | None = None,
    min_timeout: float | None = None,
    max_timeout: float | None = None,
    params: DynamicTimeoutParams | None = None,
) -> float:

    values = params or DynamicTimeoutParams(
        estimated_input_tokens=estimated_input_tokens,
        estimated_output_tokens=estimated_output_tokens,
        safety_margin=safety_margin,
        min_timeout=min_timeout,
        max_timeout=max_timeout,
    )
    effective_safety_margin = safety_margin if safety_margin is not None else config.dynamic_timeout_safety_margin
    effective_safety_margin = values.safety_margin if values.safety_margin is not None else config.dynamic_timeout_safety_margin
    effective_min_timeout = values.min_timeout if values.min_timeout is not None else float(config.dynamic_timeout_min)
    effective_max_timeout = values.max_timeout if values.max_timeout is not None else float(config.dynamic_timeout_max)

    # 尝试加载速度模型
    speed_profile_path = Path(config.model_speed_profile_path)
    speed_profile = load_speed_profile(speed_profile_path)

    if speed_profile is None or not speed_profile.samples:
        # 没有速度模型时，使用经验公式
        # 基础时间：每 1000 token 给 10 秒，加上安全边际
        total_tokens = values.estimated_input_tokens + values.estimated_output_tokens
        if total_tokens == 0:
            total_tokens = 2000  # 默认假设 2K token
        base_latency = (total_tokens / 1000) * 10.0
    else:
        # 使用速度模型插值
        base_latency = speed_profile.interpolate(values.estimated_input_tokens, values.estimated_output_tokens)

    # 应用安全边际
    timeout = base_latency * effective_safety_margin

    # 应用下限和上限
    return max(effective_min_timeout, min(effective_max_timeout, timeout))


# LLM: estimate_tokens_from_text 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 估算文本的令牌数量，影响后续调度节奏；关键副作用: 主要返回派生结构或文本，需保持字段名、顺序和空值处理稳定。
def estimate_tokens_from_text(text: str) -> int:

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


# LLM: estimate_task_tokens 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 估算任务目标和计划的令牌数量，影响后续调度节奏；关键副作用: 主要返回派生结构或文本，需保持字段名、顺序和空值处理稳定。
def estimate_task_tokens(goal: str, plan: list[str] | None = None) -> tuple[int, int]:

    # 估算输入 token：goal + plan
    plan_text = " ".join(plan) if plan else ""
    input_text = goal + " " + plan_text
    estimated_input_tokens = estimate_tokens_from_text(input_text)

    # 估算输出 token：根据输入大小按一定比例估算
    # 通常输出是输入的 20%-50%
    output_ratio = 0.3
    estimated_output_tokens = max(500, int(estimated_input_tokens * output_ratio))

    return estimated_input_tokens, estimated_output_tokens
