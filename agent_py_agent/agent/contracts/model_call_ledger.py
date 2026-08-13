
from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any


@dataclass(frozen=True)
class ModelCallLedgerOptions:
    max_records: int = 128


@dataclass(frozen=True)
class ModelCallLedgerContext:
    now: Callable[[], float] = time.monotonic


@dataclass(frozen=True)
class ModelCallStartedParams:
    call_id: str
    backend: str
    model: str
    input_tokens: int
    output_tokens_estimate: int = 0
    request_id: str = ""
    run_id: str = ""
    is_probe: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ModelCallFirstTokenParams:
    call_id: str
    output_tokens_seen: int = 1
    cache_suspected: bool = False


@dataclass(frozen=True)
class ModelCallActivityParams:
    call_id: str
    output_tokens_seen: int = 0


@dataclass(frozen=True)
class ModelCallFinishParams:
    call_id: str
    output_tokens: int = 0
    cache_suspected: bool = False


@dataclass(frozen=True)
class ModelCallTimeoutParams:
    call_id: str
    timeout_seconds: float
    timeout_stage: str
    # 门槛2: 掐断时刻的真实墙钟经过(now - record.started_at), 证据链贯通——
    # record 层时间戳 + 参数层 elapsed 双持, 不再断在调用点。
    elapsed_seconds: float = 0.0
    # 门槛2: 最后活动(last_activity_at)到超时的静默时长, 纯数值可空(默认 0.0),
    # 只记秒数不混 token 延迟, 不带 URL/prompt/响应内容。
    idle_silence_seconds: float = 0.0


@dataclass(frozen=True)
class ModelCallFailureParams:
    call_id: str
    error_type: str
    error_code: str = ""


@dataclass(frozen=True)
class ModelCallProviderAttemptParams:
    call_id: str
    attempt_id: str
    status: str
    method: str = ""
    path: str = ""
    http_status: int = 0
    error_type: str = ""
    retry_scheduled: bool = False


@dataclass(frozen=True)
class ModelCallRecord:
    call_id: str
    backend: str
    model: str
    input_tokens: int
    output_tokens_estimate: int = 0
    request_id: str = ""
    run_id: str = ""
    status: str = "started"
    started_at: float = 0.0
    last_activity_at: float = 0.0
    first_token_at: float | None = None
    finished_at: float | None = None
    timeout_at: float | None = None
    failure_at: float | None = None
    first_token_latency_seconds: float | None = None
    total_latency_seconds: float | None = None
    output_tokens: int = 0
    output_tokens_seen: int = 0
    timeout_seconds: float | None = None
    timeout_stage: str = ""
    # 门槛2: 调用点算出的掐断时刻墙钟经过(now - started_at), 与 total_latency_seconds
    # 双持——total_latency 是 ledger 落账时刻自己算的, elapsed 是掐断时刻调用点算的,
    # 两者同源同值, 互相校验防账本自证。
    elapsed_seconds: float = 0.0
    # 门槛2: 最后活动(last_activity_at)到超时的静默时长, 纯数值可空(默认 0.0),
    # 只记秒数不混 token 延迟, 不带 URL/prompt/响应内容。
    idle_silence_seconds: float = 0.0
    error_type: str = ""
    error_code: str = ""
    provider_attempt_count: int = 0
    provider_attempts: tuple[dict[str, Any], ...] = ()
    is_probe: bool = False
    cache_suspected: bool = False
    events: tuple[str, ...] = ("started",)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "call_id": self.call_id,
            "backend": self.backend,
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens_estimate": self.output_tokens_estimate,
            "request_id": self.request_id,
            "run_id": self.run_id,
            "status": self.status,
            "started_at": self.started_at,
            "last_activity_at": self.last_activity_at,
            "first_token_at": self.first_token_at,
            "finished_at": self.finished_at,
            "timeout_at": self.timeout_at,
            "failure_at": self.failure_at,
            "first_token_latency_seconds": self.first_token_latency_seconds,
            "total_latency_seconds": self.total_latency_seconds,
            "output_tokens": self.output_tokens,
            "output_tokens_seen": self.output_tokens_seen,
            "timeout_seconds": self.timeout_seconds,
            "timeout_stage": self.timeout_stage,
            "elapsed_seconds": self.elapsed_seconds,
            "idle_silence_seconds": self.idle_silence_seconds,
            "error_type": self.error_type,
            "error_code": self.error_code,
            "provider_attempt_count": self.provider_attempt_count,
            "provider_attempts": [dict(item) for item in self.provider_attempts],
            "is_probe": self.is_probe,
            "cache_suspected": self.cache_suspected,
            "events": list(self.events),
            "metadata": dict(self.metadata),
        }


class ModelCallLedger:
    def __init__(
        self,
        options: ModelCallLedgerOptions | None = None,
        context: ModelCallLedgerContext | None = None,
    ) -> None:
        self.options = options or ModelCallLedgerOptions()
        self.context = context or ModelCallLedgerContext()
        self._records: list[ModelCallRecord] = []
        self._index: dict[str, int] = {}
        self._lock = threading.RLock()

    def started(self, params: ModelCallStartedParams) -> ModelCallRecord:
        with self._lock:
            now = float(self.context.now())
            record = ModelCallRecord(
                call_id=params.call_id,
                backend=params.backend,
                model=params.model,
                input_tokens=max(0, int(params.input_tokens)),
                output_tokens_estimate=max(0, int(params.output_tokens_estimate)),
                request_id=params.request_id,
                run_id=params.run_id,
                started_at=now,
                last_activity_at=now,
                is_probe=params.is_probe,
                metadata=dict(params.metadata),
            )
            self._append_or_replace(record)
            return record

    def first_token(self, params: ModelCallFirstTokenParams) -> ModelCallRecord:
        with self._lock:
            record = self._require_record(params.call_id)
            if record.first_token_at is not None:
                return record
            now = float(self.context.now())
            updated = replace(
                record,
                status="first_token",
                first_token_at=now,
                last_activity_at=now,
                first_token_latency_seconds=max(0.0, now - record.started_at),
                output_tokens_seen=max(0, int(params.output_tokens_seen)),
                cache_suspected=record.cache_suspected or params.cache_suspected,
                events=record.events + ("first_token",),
            )
            self._replace(updated)
            return updated

    def activity(self, params: ModelCallActivityParams) -> ModelCallRecord:
        """Record streamed output activity without growing the event ledger."""

        with self._lock:
            record = self._require_record(params.call_id)
            if record.status in {"failed", "finished", "timed_out"}:
                return record
            now = float(self.context.now())
            updated = replace(
                record,
                last_activity_at=now,
                output_tokens_seen=(
                    record.output_tokens_seen
                    + max(0, int(params.output_tokens_seen))
                ),
            )
            self._replace(updated)
            return updated

    def finished(self, params: ModelCallFinishParams) -> ModelCallRecord:
        with self._lock:
            record = self._require_record(params.call_id)
            if record.status in {"failed", "timed_out"}:
                return record
            now = float(self.context.now())
            updated = replace(
                record,
                status="finished",
                finished_at=now,
                last_activity_at=now,
                total_latency_seconds=max(0.0, now - record.started_at),
                output_tokens=max(0, int(params.output_tokens)),
                cache_suspected=record.cache_suspected or params.cache_suspected,
                events=_append_event(record.events, "finished"),
            )
            self._replace(updated)
            return updated

    def timeout(self, params: ModelCallTimeoutParams) -> ModelCallRecord:
        with self._lock:
            record = self._require_record(params.call_id)
            now = float(self.context.now())
            updated = replace(
                record,
                status="timed_out",
                timeout_at=now,
                last_activity_at=now,
                timeout_seconds=max(0.0, float(params.timeout_seconds)),
                timeout_stage=params.timeout_stage,
                elapsed_seconds=max(0.0, float(params.elapsed_seconds)),
                idle_silence_seconds=max(0.0, float(params.idle_silence_seconds)),
                total_latency_seconds=max(0.0, now - record.started_at),
                events=_append_event(record.events, "timeout"),
            )
            self._replace(updated)
            return updated

    def failed(self, params: ModelCallFailureParams) -> ModelCallRecord:
        with self._lock:
            record = self._require_record(params.call_id)
            now = float(self.context.now())
            updated = replace(
                record,
                status="failed",
                failure_at=now,
                last_activity_at=now,
                total_latency_seconds=max(0.0, now - record.started_at),
                error_type=params.error_type,
                error_code=params.error_code,
                events=_append_event(record.events, "failed"),
            )
            self._replace(updated)
            return updated

    def provider_attempt(
        self,
        params: ModelCallProviderAttemptParams,
    ) -> ModelCallRecord:
        with self._lock:
            record = self._require_record(params.call_id)
            attempts = [dict(item) for item in record.provider_attempts]
            index = next(
                (
                    idx
                    for idx, item in enumerate(attempts)
                    if str(item.get("attempt_id") or "") == params.attempt_id
                ),
                None,
            )
            now = float(self.context.now())
            payload = {
                "attempt_id": params.attempt_id,
                "status": params.status,
                "method": params.method,
                "path": params.path,
                "http_status": max(0, int(params.http_status or 0)),
                "error_type": params.error_type,
                "retry_scheduled": bool(params.retry_scheduled),
            }
            if index is None:
                payload["started_at"] = now
                attempts.append(payload)
            else:
                payload["started_at"] = float(attempts[index].get("started_at") or now)
                if params.status != "started":
                    payload["finished_at"] = now
                attempts[index] = payload
            updated = replace(
                record,
                last_activity_at=now,
                provider_attempt_count=len(attempts),
                provider_attempts=tuple(attempts),
                events=_append_event(record.events, f"provider_attempt_{params.status}"),
            )
            self._replace(updated)
            return updated

    def records(self) -> tuple[ModelCallRecord, ...]:
        with self._lock:
            return tuple(self._records)

    def _append_or_replace(self, record: ModelCallRecord) -> None:
        if record.call_id in self._index:
            self._replace(record)
            return
        self._records.append(record)
        self._rebuild_index()
        self._trim_records()

    def _replace(self, record: ModelCallRecord) -> None:
        self._records[self._index[record.call_id]] = record

    def _require_record(self, call_id: str) -> ModelCallRecord:
        if call_id not in self._index:
            raise KeyError(f"unknown model call id: {call_id}")
        return self._records[self._index[call_id]]

    def _trim_records(self) -> None:
        max_records = max(1, int(self.options.max_records))
        if len(self._records) <= max_records:
            return
        self._records = self._records[-max_records:]
        self._rebuild_index()

    def _rebuild_index(self) -> None:
        self._index = {record.call_id: index for index, record in enumerate(self._records)}


def _append_event(events: tuple[str, ...], event: str) -> tuple[str, ...]:
    if events and events[-1] == event:
        return events
    return events + (event,)


__all__ = [
    "ModelCallActivityParams",
    "ModelCallFailureParams",
    "ModelCallFinishParams",
    "ModelCallFirstTokenParams",
    "ModelCallLedger",
    "ModelCallLedgerContext",
    "ModelCallLedgerOptions",
    "ModelCallRecord",
    "ModelCallProviderAttemptParams",
    "ModelCallStartedParams",
    "ModelCallTimeoutParams",
]
