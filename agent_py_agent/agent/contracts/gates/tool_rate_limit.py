# LLM: Runtime tool rate limit gates bound repeated calls and back off broken tool identities.
# 模块用途: 按 tool + args_hash 维护内存速率预算和 circuit breaker，返回机器可读 retry_after。

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace

from .models import GateDecision
from .tool_rate_limit_models import ToolRateLimitFacts, ToolRateLimitPolicy, ToolRateLimitRecord
from .tool_rate_limit_rules import (
    backoff_seconds,
    circuit_decision,
    coerce_records,
    effective_circuit_state,
    failure_threshold,
    identity,
    max_calls,
    rate_limit_block,
    require_identity,
    text,
    window_attempts,
    window_seconds,
)


# LLM: ToolRateLimitLedger keeps bounded in-memory rows keyed by tool plus args_hash.
# 类用途: 提供 check/record_attempt/record_failure/record_success 接口，不执行 I/O。
class ToolRateLimitLedger:
    # LLM: __init__ accepts persisted dict rows or dataclass rows.
    # 函数用途: 从可序列化历史记录恢复内存索引，便于后续接入持久化。
    def __init__(
        self,
        *,
        policy: ToolRateLimitPolicy | None = None,
        records: tuple[ToolRateLimitRecord | Mapping[str, object], ...] = (),
    ) -> None:
        self.policy = policy or ToolRateLimitPolicy()
        self._records: dict[tuple[str, str], ToolRateLimitRecord] = {}
        for record in coerce_records(records):
            self._records[(record.tool_name, record.args_hash)] = record
        self._trim_records()

    # LLM: check returns the runtime gate decision without mutating ledger state.
    # 函数用途: 在执行工具前阻断缺 identity、超速率或 circuit open 的调用。
    def check(self, facts: ToolRateLimitFacts) -> GateDecision:
        identity_key = identity(facts)
        if identity_key is None:
            return GateDecision.deny(
                "tool_rate_limit",
                "TOOL_RATE_LIMIT_IDENTITY_MISSING",
                evidence={
                    "tool_name": text(facts.tool_name),
                    "args_hash": text(facts.args_hash),
                    "operation_id": text(facts.operation_id),
                },
                recommended_action="repair_tool_call_identity",
            )
        record = self._records.get(identity_key) or ToolRateLimitRecord(*identity_key)
        circuit = circuit_decision(record, facts, self.policy)
        if circuit is not None:
            return circuit
        attempts = window_attempts(record, facts.now, window_seconds(self.policy))
        call_budget = max_calls(self.policy)
        if call_budget <= 0 and attempts:
            return rate_limit_block(record, facts, attempts, window_seconds(self.policy))
        if call_budget > 0 and len(attempts) >= call_budget:
            return rate_limit_block(record, facts, attempts, window_seconds(self.policy))
        return GateDecision.allow(
            "tool_rate_limit",
            evidence={
                "tool_name": identity_key[0],
                "args_hash": identity_key[1],
                "operation_id": text(facts.operation_id),
                "attempts_in_window": len(attempts),
                "window_seconds": window_seconds(self.policy),
                "circuit_state": effective_circuit_state(record, facts.now),
                "consecutive_failures": record.consecutive_failures,
            },
        )

    # LLM: record_attempt stores an allowed call timestamp for rate limiting.
    # 函数用途: 执行前通过 gate 后写入一次尝试，下一次 check 可按窗口预算阻断。
    def record_attempt(self, facts: ToolRateLimitFacts) -> ToolRateLimitRecord:
        identity = require_identity(facts)
        record = self._records.get(identity) or ToolRateLimitRecord(*identity)
        attempts = window_attempts(record, facts.now, window_seconds(self.policy)) + (float(facts.now),)
        updated = replace(record, attempt_timestamps=attempts)
        self._upsert(updated)
        return updated

    # LLM: record_failure advances consecutive failure state and opens circuit at threshold.
    # 函数用途: 记录 retryable 失败；达到阈值后按 schedule 计算 retry_after_until。
    def record_failure(self, facts: ToolRateLimitFacts) -> ToolRateLimitRecord:
        identity = require_identity(facts)
        record = self._records.get(identity) or ToolRateLimitRecord(*identity)
        now = float(facts.now)
        failures = max(0, int(record.consecutive_failures)) + 1
        threshold = failure_threshold(self.policy)
        opened = failures >= threshold
        updated = replace(
            record,
            attempt_timestamps=window_attempts(record, now, window_seconds(self.policy)),
            consecutive_failures=failures,
            circuit_state="open" if opened else "closed",
            circuit_opened_at=now if opened else 0.0,
            retry_after_until=now + backoff_seconds(self.policy, failures) if opened else 0.0,
            last_failure_at=now,
            total_failures=max(0, int(record.total_failures)) + 1,
        )
        self._upsert(updated)
        return updated

    # LLM: record_success closes any half-open/open circuit and resets failure counters.
    # 函数用途: 成功结果是恢复信号，清零连续失败和 retry_after。
    def record_success(self, facts: ToolRateLimitFacts) -> ToolRateLimitRecord:
        identity = require_identity(facts)
        record = self._records.get(identity) or ToolRateLimitRecord(*identity)
        now = float(facts.now)
        updated = replace(
            record,
            attempt_timestamps=window_attempts(record, now, window_seconds(self.policy)),
            consecutive_failures=0,
            circuit_state="closed",
            circuit_opened_at=0.0,
            retry_after_until=0.0,
            last_success_at=now,
        )
        self._upsert(updated)
        return updated

    # LLM: records exposes an immutable snapshot for persistence or audit tests.
    # 函数用途: 返回当前内存行，不暴露内部 dict。
    def records(self) -> tuple[ToolRateLimitRecord, ...]:
        return tuple(self._records.values())

    # LLM: _upsert keeps this contract helper structure-first and stable.
    # 函数用途: 支撑本模块的机器字段校验、转换或汇总，不读取普通自然语言作为事实。
    def _upsert(self, record: ToolRateLimitRecord) -> None:
        self._records[(record.tool_name, record.args_hash)] = record
        self._trim_records()

    # LLM: _trim_records keeps this contract helper structure-first and stable.
    # 函数用途: 支撑本模块的机器字段校验、转换或汇总，不读取普通自然语言作为事实。
    def _trim_records(self) -> None:
        max_records = max(1, int(self.policy.max_records))
        if len(self._records) <= max_records:
            return
        items = tuple(self._records.items())[-max_records:]
        self._records = dict(items)


# LLM: evaluate_tool_rate_limit_gate is a pure functional adapter for callers without a ledger instance.
# 函数用途: 从传入记录构建临时内存 ledger 并返回同一 GateDecision 形状。
def evaluate_tool_rate_limit_gate(
    facts: ToolRateLimitFacts,
    *,
    policy: ToolRateLimitPolicy | None = None,
    records: tuple[ToolRateLimitRecord | Mapping[str, object], ...] = (),
) -> GateDecision:
    return ToolRateLimitLedger(policy=policy, records=records).check(facts)


__all__ = [
    "ToolRateLimitFacts",
    "ToolRateLimitLedger",
    "ToolRateLimitPolicy",
    "ToolRateLimitRecord",
    "evaluate_tool_rate_limit_gate",
]
