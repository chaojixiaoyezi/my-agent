# LLM: Tool rate-limit rules compute window and circuit decisions without owning storage.
# 模块用途: 为 ToolRateLimitLedger 提供纯函数判断，便于单测、回放和未来持久化复用。

from __future__ import annotations

from collections.abc import Mapping

from .models import GateDecision
from .tool_rate_limit_models import (
    _CLOSED,
    _DEFAULT_BACKOFF_SCHEDULE,
    _HALF_OPEN,
    _OPEN,
    ToolRateLimitFacts,
    ToolRateLimitPolicy,
    ToolRateLimitRecord,
)


# LLM: circuit_decision blocks an identity while its failure circuit is still open.
# 函数用途: 按连续失败、阈值和 retry_after_until 计算熔断 GateDecision；未熔断返回 None。
def circuit_decision(
    record: ToolRateLimitRecord,
    facts: ToolRateLimitFacts,
    policy: ToolRateLimitPolicy,
) -> GateDecision | None:
    threshold = failure_threshold(policy)
    if threshold <= 0:
        return None
    if record.circuit_state != _OPEN and record.consecutive_failures < threshold:
        return None
    retry_after_until = record.retry_after_until
    if retry_after_until <= 0 and record.last_failure_at > 0:
        retry_after_until = record.last_failure_at + backoff_seconds(policy, record.consecutive_failures)
    retry_after = retry_after_seconds(retry_after_until, facts.now)
    if retry_after <= 0:
        return None
    return GateDecision.block(
        "tool_rate_limit",
        "TOOL_CIRCUIT_OPEN",
        evidence=blocking_evidence(record, facts, "open", retry_after)
        | {
            "consecutive_failures": record.consecutive_failures,
            "failure_threshold": threshold,
        },
        recommended_action="retry_after_backoff",
    )


# LLM: rate_limit_block returns the standard over-budget GateDecision.
# 函数用途: 根据窗口内尝试次数计算 retry_after_seconds，并保持证据字段稳定。
def rate_limit_block(
    record: ToolRateLimitRecord,
    facts: ToolRateLimitFacts,
    attempts: tuple[float, ...],
    window_seconds: float,
) -> GateDecision:
    oldest = min(attempts) if attempts else float(facts.now)
    retry_after = retry_after_seconds(oldest + window_seconds, facts.now)
    return GateDecision.block(
        "tool_rate_limit",
        "TOOL_RATE_LIMIT_EXCEEDED",
        evidence=blocking_evidence(record, facts, effective_circuit_state(record, facts.now), retry_after)
        | {
            "attempts_in_window": len(attempts),
            "window_seconds": window_seconds,
        },
        recommended_action="retry_after_backoff",
    )


# LLM: coerce_records accepts persisted primitive rows and dataclass rows.
# 函数用途: 从 write_boundary/ledger 记录恢复 ToolRateLimitRecord，坏行被忽略而不是当成功事实。
def coerce_records(
    records: tuple[ToolRateLimitRecord | Mapping[str, object], ...],
) -> tuple[ToolRateLimitRecord, ...]:
    normalized: list[ToolRateLimitRecord] = []
    for item in records:
        if isinstance(item, ToolRateLimitRecord):
            normalized.append(item)
            continue
        record = record_from_mapping(item)
        if record is not None:
            normalized.append(record)
    return tuple(normalized)


# LLM: record_from_mapping converts one persisted mapping into a typed rate-limit row.
# 函数用途: 校验 tool_name/args_hash 必需字段，并规范化 circuit 和计数字段。
def record_from_mapping(item: object) -> ToolRateLimitRecord | None:
    if not isinstance(item, Mapping):
        return None
    tool_name = text(item.get("tool_name"))
    args_hash = text(item.get("args_hash"))
    if not (tool_name and args_hash):
        return None
    return ToolRateLimitRecord(
        tool_name=tool_name,
        args_hash=args_hash,
        attempt_timestamps=float_tuple(item.get("attempt_timestamps")),
        consecutive_failures=max(0, int(item.get("consecutive_failures") or 0)),
        circuit_state=valid_circuit_state(item.get("circuit_state")),
        circuit_opened_at=float(item.get("circuit_opened_at") or 0.0),
        retry_after_until=float(item.get("retry_after_until") or 0.0),
        last_failure_at=float(item.get("last_failure_at") or 0.0),
        last_success_at=float(item.get("last_success_at") or 0.0),
        total_failures=max(0, int(item.get("total_failures") or 0)),
        metadata=dict(item.get("metadata") or {}),
    )


# LLM: blocking_evidence is the shared evidence for rate and circuit blocks.
# 函数用途: 保持 tool_name/args_hash/operation_id/circuit/retry_after 字段稳定。
def blocking_evidence(
    record: ToolRateLimitRecord,
    facts: ToolRateLimitFacts,
    circuit_state: str,
    retry_after: float,
) -> dict[str, object]:
    return {
        "tool_name": record.tool_name,
        "args_hash": record.args_hash,
        "operation_id": text(facts.operation_id),
        "circuit_state": circuit_state,
        "retry_after_seconds": retry_after,
    }


# LLM: effective_circuit_state projects open records to half-open after backoff expires.
# 函数用途: 给 allow 证据和 rate-limit block 提供当前 circuit 状态，不修改 ledger。
def effective_circuit_state(record: ToolRateLimitRecord, now: float) -> str:
    if record.circuit_state == _OPEN and retry_after_seconds(record.retry_after_until, now) <= 0:
        return _HALF_OPEN
    return record.circuit_state if record.circuit_state in {_OPEN, _CLOSED, _HALF_OPEN} else _CLOSED


# LLM: window_attempts returns only timestamps inside the configured window.
# 函数用途: 根据 now/window_seconds 过滤尝试记录，避免历史无限累积影响当前预算。
def window_attempts(record: ToolRateLimitRecord, now: float, window_seconds: float) -> tuple[float, ...]:
    if window_seconds <= 0:
        return ()
    floor = float(now) - window_seconds
    return tuple(timestamp for timestamp in record.attempt_timestamps if float(timestamp) > floor)


# LLM: retry_after_seconds computes a non-negative rounded wait time.
# 函数用途: 给模型/调度器一个机器可读 retry_after_seconds，而不是自然语言解释。
def retry_after_seconds(until: float, now: float) -> float:
    return max(0.0, round(float(until) - float(now), 6))


# LLM: backoff_seconds chooses a schedule slot from consecutive failure count.
# 函数用途: 达到 failure_threshold 后按退避数组选择等待时长。
def backoff_seconds(policy: ToolRateLimitPolicy, consecutive_failures: int) -> float:
    schedule = tuple(float(item) for item in policy.backoff_schedule_seconds if float(item) >= 0)
    if not schedule:
        schedule = _DEFAULT_BACKOFF_SCHEDULE
    index = max(0, consecutive_failures - failure_threshold(policy))
    return schedule[min(index, len(schedule) - 1)]


# LLM: float_tuple normalizes persisted timestamp arrays.
# 函数用途: 非数组返回空元组，坏成员被过滤，不让脏记录破坏整个限流门。
def float_tuple(value: object) -> tuple[float, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    values: list[float] = []
    for item in value:
        try:
            values.append(float(item))
        except (TypeError, ValueError):
            continue
    return tuple(values)


# LLM: identity returns a valid tool/args key or None.
# 函数用途: 缺 tool_name/args_hash 时让 gate 输出 identity missing，而不是默默放行。
def identity(facts: ToolRateLimitFacts) -> tuple[str, str] | None:
    tool_name = text(facts.tool_name)
    args_hash = text(facts.args_hash)
    return (tool_name, args_hash) if tool_name and args_hash else None


# LLM: require_identity raises for mutating ledger methods that must have a key.
# 函数用途: record_attempt/failure/success 不接受缺身份事实，避免写入不可复用记录。
def require_identity(facts: ToolRateLimitFacts) -> tuple[str, str]:
    item = identity(facts)
    if item is None:
        raise ValueError("tool_rate_limit identity requires tool_name and args_hash")
    return item


# LLM: valid_circuit_state normalizes persisted circuit states.
# 函数用途: 只接受 closed/open/half_open，其它值按 closed 降级。
def valid_circuit_state(value: object) -> str:
    state = text(value).lower()
    return state if state in {_CLOSED, _OPEN, _HALF_OPEN} else _CLOSED


# LLM: text normalizes scalar machine fields for rate-limit identities.
# 函数用途: 将结构字段转成去空白字符串。
def text(value: object) -> str:
    return str(value or "").strip()


# LLM: max_calls clamps negative max call budgets to zero.
# 函数用途: 为 check 提供稳定预算口径；0 表示不限制同一窗口内调用次数。
def max_calls(policy: ToolRateLimitPolicy) -> int:
    return max(0, int(policy.max_calls))


# LLM: window_seconds clamps negative windows to zero.
# 函数用途: 为窗口过滤提供稳定秒数。
def window_seconds(policy: ToolRateLimitPolicy) -> float:
    return max(0.0, float(policy.window_seconds))


# LLM: failure_threshold clamps negative circuit thresholds to zero.
# 函数用途: 为连续失败熔断提供稳定预算；0 表示不启用失败次数熔断。
def failure_threshold(policy: ToolRateLimitPolicy) -> int:
    return max(0, int(policy.failure_threshold))


__all__ = [
    "backoff_seconds",
    "circuit_decision",
    "coerce_records",
    "effective_circuit_state",
    "failure_threshold",
    "identity",
    "max_calls",
    "rate_limit_block",
    "require_identity",
    "text",
    "window_attempts",
    "window_seconds",
]
