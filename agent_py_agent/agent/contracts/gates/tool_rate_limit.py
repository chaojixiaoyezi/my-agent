from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from ..recovery import RecoveryAction
from .models import GateDecision, GateFinding

_CLOSED = "closed"
_OPEN = "open"
_HALF_OPEN = "half_open"
_DEFAULT_BACKOFF_SCHEDULE = (1.0, 2.0, 4.0, 8.0, 16.0, 30.0)

@dataclass(frozen=True)
class ToolRateLimitPolicy:
    max_calls: int = 60
    window_seconds: float = 60.0
    failure_threshold: int = 3
    backoff_schedule_seconds: tuple[float, ...] = _DEFAULT_BACKOFF_SCHEDULE
    max_records: int = 256

@dataclass(frozen=True)
class ToolRateLimitFacts:
    tool_name: str
    args_hash: str
    now: float
    operation_id: str = ""

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
class ToolRateLimitLedger:
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
                recommended_action=RecoveryAction.REPAIR_TOOL_CALL_IDENTITY.value,
            )
        record = self._records.get(identity_key) or ToolRateLimitRecord(*identity_key)
        circuit = circuit_decision(record, facts, self.policy)
        if circuit is not None:
            return circuit
        attempts = window_attempts(record, facts.now, window_seconds(self.policy))
        call_budget = max_calls(self.policy)
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
    def record_attempt(self, facts: ToolRateLimitFacts) -> ToolRateLimitRecord:
        identity = require_identity(facts)
        record = self._records.get(identity) or ToolRateLimitRecord(*identity)
        attempts = window_attempts(record, facts.now, window_seconds(self.policy)) + (float(facts.now),)
        updated = replace(record, attempt_timestamps=attempts)
        self._upsert(updated)
        return updated
    def record_failure(self, facts: ToolRateLimitFacts) -> ToolRateLimitRecord:
        identity = require_identity(facts)
        record = self._records.get(identity) or ToolRateLimitRecord(*identity)
        now = float(facts.now)
        failures = max(0, int(record.consecutive_failures)) + 1
        threshold = failure_threshold(self.policy)
        opened = threshold > 0 and failures >= threshold
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
    def records(self) -> tuple[ToolRateLimitRecord, ...]:
        return tuple(self._records.values())
    def _upsert(self, record: ToolRateLimitRecord) -> None:
        self._records[(record.tool_name, record.args_hash)] = record
        self._trim_records()
    def _trim_records(self) -> None:
        max_records = max(1, int(self.policy.max_records))
        if len(self._records) <= max_records:
            return
        items = tuple(self._records.items())[-max_records:]
        self._records = dict(items)
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
    # 熔断拒绝必须自带说人话的 message：没有 message 时 _gate_output 会兜底成
    # "工具未授权或未通过运行时门"——真机实锤模型把临时熔断误读成永久权限墙
    # ("解阻工具坏了")、直接放弃整条编队路。文案要点明:临时、会自愈、该改打法。
    message = (
        f"{record.tool_name} 用同样参数连续失败 {record.consecutive_failures} 次,已临时熔断,"
        f"约 {max(1, round(retry_after))} 秒后自动恢复。这不是权限问题、工具没有坏——"
        "不要原样重试:回看上一次失败返回的原因,修正参数,或改用其他工具/方式推进。"
    )
    return GateDecision(
        "tool_rate_limit",
        "BLOCKED",
        False,
        (
            GateFinding(
                "TOOL_CIRCUIT_OPEN",
                "P0",
                message,
                blocking_evidence(record, facts, "open", retry_after)
                | {
                    "consecutive_failures": record.consecutive_failures,
                    "failure_threshold": threshold,
                },
            ),
        ),
        RecoveryAction.RETRY_AFTER_BACKOFF.value,
        {},
    )
def rate_limit_block(
    record: ToolRateLimitRecord,
    facts: ToolRateLimitFacts,
    attempts: tuple[float, ...],
    window_seconds: float,
) -> GateDecision:
    oldest = min(attempts) if attempts else float(facts.now)
    retry_after = retry_after_seconds(oldest + window_seconds, facts.now)
    message = (
        f"{record.tool_name} 同样参数在 {round(window_seconds)} 秒内已调用 {len(attempts)} 次,触发频率上限,"
        f"约 {max(1, round(retry_after))} 秒后自动恢复。不要原地轮询——要等条件变化就用 wait 登记到点提醒。"
    )
    return GateDecision(
        "tool_rate_limit",
        "BLOCKED",
        False,
        (
            GateFinding(
                "TOOL_RATE_LIMIT_EXCEEDED",
                "P0",
                message,
                blocking_evidence(record, facts, effective_circuit_state(record, facts.now), retry_after)
                | {
                    "attempts_in_window": len(attempts),
                    "window_seconds": window_seconds,
                },
            ),
        ),
        RecoveryAction.RETRY_AFTER_BACKOFF.value,
        {},
    )
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
def effective_circuit_state(record: ToolRateLimitRecord, now: float) -> str:
    if record.circuit_state == _OPEN and retry_after_seconds(record.retry_after_until, now) <= 0:
        return _HALF_OPEN
    return record.circuit_state if record.circuit_state in {_OPEN, _CLOSED, _HALF_OPEN} else _CLOSED
def window_attempts(record: ToolRateLimitRecord, now: float, window_seconds: float) -> tuple[float, ...]:
    if window_seconds <= 0:
        return ()
    floor = float(now) - window_seconds
    return tuple(timestamp for timestamp in record.attempt_timestamps if float(timestamp) > floor)
def retry_after_seconds(until: float, now: float) -> float:
    return max(0.0, round(float(until) - float(now), 6))
def backoff_seconds(policy: ToolRateLimitPolicy, consecutive_failures: int) -> float:
    schedule = tuple(float(item) for item in policy.backoff_schedule_seconds if float(item) >= 0)
    if not schedule:
        schedule = _DEFAULT_BACKOFF_SCHEDULE
    index = max(0, consecutive_failures - failure_threshold(policy))
    return schedule[min(index, len(schedule) - 1)]
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
def identity(facts: ToolRateLimitFacts) -> tuple[str, str] | None:
    tool_name = text(facts.tool_name)
    args_hash = text(facts.args_hash)
    return (tool_name, args_hash) if tool_name and args_hash else None
def require_identity(facts: ToolRateLimitFacts) -> tuple[str, str]:
    item = identity(facts)
    if item is None:
        raise ValueError("tool_rate_limit identity requires tool_name and args_hash")
    return item
def valid_circuit_state(value: object) -> str:
    state = text(value).lower()
    return state if state in {_CLOSED, _OPEN, _HALF_OPEN} else _CLOSED
def text(value: object) -> str:
    return str(value or "").strip()
def max_calls(policy: ToolRateLimitPolicy) -> int:
    return max(0, int(policy.max_calls))
def window_seconds(policy: ToolRateLimitPolicy) -> float:
    return max(0.0, float(policy.window_seconds))
def failure_threshold(policy: ToolRateLimitPolicy) -> int:
    return max(0, int(policy.failure_threshold))
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
