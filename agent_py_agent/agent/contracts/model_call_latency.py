# LLM: 本模块只从原 ModelCallRecord 的时钟事实投影可加耗时，不拥有计时器、预算或第二本账；同步检查累计增量与显示白名单。
# 模块用途: 将模型调用总时长、等首包和首包后耗时转换为毫秒，保留未知时钟的覆盖计数。
from __future__ import annotations

import math

MODEL_LATENCY_FIELDS = (
    "model_latency_ms", "first_token_wait_ms", "post_first_token_ms",
    "timed_model_call_count", "first_token_timed_call_count", "split_latency_call_count",
)


# LLM: 仅接受原记录中的非负有限秒数；None、bool 和非法数值都保持未知，不从文字或 token 数反推时间。
# 函数用途: 将真实秒数转为整型毫秒，供原用量快照使用非负整数增量。
def _milliseconds(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    milliseconds = seconds * 1000
    return round(milliseconds) if math.isfinite(milliseconds) and seconds >= 0 else None


# LLM: 耗时是物理调用时长之和，不能当并行回合墙钟；只有总时长与首包都已记录才拆首包后时间，缺字段不补造零观测。
# 函数用途: 从一次原模型调用生成六个可加数字，时钟覆盖次数帮助区分旧账未知和真实零耗时。
def model_call_latency_facts(record: object) -> dict[str, int]:
    total = _milliseconds(getattr(record, "total_latency_seconds", None))
    first = _milliseconds(getattr(record, "first_token_latency_seconds", None))
    split = total is not None and first is not None and first <= total
    return {
        "model_latency_ms": total or 0,
        "first_token_wait_ms": first or 0,
        "post_first_token_ms": total - first if split else 0,
        "timed_model_call_count": int(total is not None),
        "first_token_timed_call_count": int(first is not None),
        "split_latency_call_count": int(split),
    }
