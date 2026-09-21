# LLM: 后台进度策略的纯计算；只接收计数和策略身份，不读 Store、时钟或调度器状态。
# 调整时同步 runtime 的成功/失败落账及后台策略回归；不能在此领取租约、等待或写账。
# 模块用途: 计算无进展次数和失败后的等待时间，让调度编排与数值规则各有一个实现。
from __future__ import annotations

import hashlib

_FAILURE_BASE_BACKOFF_SECONDS = 300
_FAILURE_MAX_BACKOFF_SECONDS = 3600


# LLM: 成功写入等物质进展由调用方的结构化工具事实计数，不从回复正文推断；不修改原 metadata。
# 函数用途: 有进展时清零，否则递增原次数；缺失、坏值和负数沿既有规则从零起算。
def next_no_progress_streak(previous_streak: object, material_progress_count: int) -> int:
    if material_progress_count > 0:
        return 0
    try:
        previous = int(previous_streak or 0)
    except (TypeError, ValueError):
        previous = 0
    return max(0, previous) + 1


# LLM: 保留策略 ID 派生的确定性错峰和原舍入顺序；相同输入跨进程结果一致，不使用随机数或时钟。
# 函数用途: 从五分钟开始按失败次数翻倍，基数封顶一小时后再作约正负百分之十的错峰。
def policy_failure_backoff(failures: int, policy_id: str) -> float:
    base = min(
        _FAILURE_BASE_BACKOFF_SECONDS * (2 ** max(0, int(failures) - 1)),
        _FAILURE_MAX_BACKOFF_SECONDS,
    )
    digest = hashlib.md5(str(policy_id).encode("utf-8")).hexdigest()
    ratio = 0.9 + (int(digest[:4], 16) % 2000) / 10000.0
    return round(base * ratio, 3)
