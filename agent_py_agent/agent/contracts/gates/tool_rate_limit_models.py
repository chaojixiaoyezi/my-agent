# LLM: Tool rate-limit models are serializable contracts shared by ledger and rule helpers.
# 模块用途: 保存限流策略、调用事实和持久化记录的数据结构，避免执行逻辑和模型定义混在一个文件。

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

_CLOSED = "closed"
_OPEN = "open"
_HALF_OPEN = "half_open"
_DEFAULT_BACKOFF_SCHEDULE = (1.0, 2.0, 4.0, 8.0, 16.0, 30.0)


# LLM: ToolRateLimitPolicy keeps all runtime throttling thresholds structured.
# 类用途: 描述每个 tool/args_hash 的窗口预算、失败阈值和 backoff schedule。
@dataclass(frozen=True)
class ToolRateLimitPolicy:
    max_calls: int = 60
    window_seconds: float = 60.0
    failure_threshold: int = 3
    backoff_schedule_seconds: tuple[float, ...] = _DEFAULT_BACKOFF_SCHEDULE
    max_records: int = 256


# LLM: ToolRateLimitFacts are the structured facts for one runtime tool identity check.
# 类用途: 保存工具名、参数 hash、当前时间和可选 operation_id，测试可注入固定时间。
@dataclass(frozen=True)
class ToolRateLimitFacts:
    tool_name: str
    args_hash: str
    now: float
    operation_id: str = ""


# LLM: ToolRateLimitRecord is the serializable state row for one tool/args_hash key.
# 类用途: 保存窗口内调用时间、连续失败数、circuit 状态和下次可试时间。
@dataclass(frozen=True)
class ToolRateLimitRecord:
    tool_name: str
    args_hash: str
    attempt_timestamps: tuple[float, ...] = ()
    consecutive_failures: int = 0
    circuit_state: str = _CLOSED
    circuit_opened_at: float = 0.0
    retry_after_until: float = 0.0
    last_failure_at: float = 0.0
    last_success_at: float = 0.0
    total_failures: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    # LLM: to_dict gives persistence callers a primitive record shape.
    # 函数用途: 将内存行转成普通 dict，后续可直接写入 ledger/json。
    def to_dict(self) -> dict[str, Any]:
        return {
            "tool_name": self.tool_name,
            "args_hash": self.args_hash,
            "attempt_timestamps": list(self.attempt_timestamps),
            "consecutive_failures": self.consecutive_failures,
            "circuit_state": self.circuit_state,
            "circuit_opened_at": self.circuit_opened_at,
            "retry_after_until": self.retry_after_until,
            "last_failure_at": self.last_failure_at,
            "last_success_at": self.last_success_at,
            "total_failures": self.total_failures,
            "metadata": dict(self.metadata),
        }


__all__ = [
    "ToolRateLimitFacts",
    "ToolRateLimitPolicy",
    "ToolRateLimitRecord",
    "_CLOSED",
    "_DEFAULT_BACKOFF_SCHEDULE",
    "_HALF_OPEN",
    "_OPEN",
]
