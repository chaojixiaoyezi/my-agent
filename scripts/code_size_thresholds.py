# LLM: Code-size governance helper; keep report identities, thresholds, and baseline behavior stable.
# 模块用途: 支撑代码规模守卫，统计文件/函数/类大小并生成可审查的报告。

from __future__ import annotations

"""Shared threshold helpers for the code-size checker."""

import math
from dataclasses import dataclass

from code_size_rules import NEAR_SOFT_RATIO, Finding


# LLM: FindingInput 是阈值 finding 的基础输入契约。
# 类用途: 保存 kind、路径、名称、当前值、soft limit 和提示信息。
@dataclass(frozen=True)
class FindingInput:
    kind: str
    rel: str
    name: str
    value: int
    limit: int
    message: str


# LLM: LimitFindingInput 给超限 finding 补充 hard limit。
# 类用途: 把基础 finding 输入和 hard 阈值绑定，供 severity 判断使用。
@dataclass(frozen=True)
class LimitFindingInput:
    base: FindingInput
    hard_limit: int


# LLM: near_soft_floor 定义 high-risk 起点；比例来自 code-size 规则。
# 函数用途: 根据 soft limit 和 NEAR_SOFT_RATIO 计算接近软限制的下界。
def near_soft_floor(soft_limit: int) -> int:
    return math.ceil(soft_limit * NEAR_SOFT_RATIO)


# LLM: is_near_soft 判断 high-risk 区间。
# 函数用途: 判断当前值是否已经接近 soft limit，但尚未超过 soft limit。
def is_near_soft(value: int, soft_limit: int) -> bool:
    return near_soft_floor(soft_limit) <= value <= soft_limit


# LLM: near_soft_finding 构造 high-risk finding。
# 函数用途: 把接近软限制的输入转换成 severity=high-risk 的 Finding。
def near_soft_finding(data: FindingInput) -> Finding:
    return Finding(
        data.kind,
        data.rel,
        data.name,
        data.value,
        data.limit,
        "high-risk",
        f"near soft limit: {data.message}",
    )


# LLM: limit_finding 构造超限 finding。
# 函数用途: 根据 hard limit 选择 hard 或 soft severity，并返回统一 Finding。
def limit_finding(data: LimitFindingInput) -> Finding:
    severity = "hard" if data.base.value > data.hard_limit else "soft"
    limit = data.hard_limit if severity == "hard" else data.base.limit
    return Finding(data.base.kind, data.base.rel, data.base.name, data.base.value, limit, severity, data.base.message)
