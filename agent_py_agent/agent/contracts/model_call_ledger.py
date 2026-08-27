
from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

# LLM: 本模块同时维护有界模型调用明细与按 request/run 聚合的累计观测；裁剪明细不得截断最终计数。
# 模块用途: 记录模型调用、供应商 HTTP 尝试和超时等结构化事实，供运行时诊断与最终结果展示。

# 门槛2 终审边界②(steward seq1622-2): stage 合同单一事实源下沉到账本层。
# ProviderTimeoutError 构造入口与 ModelCallLedger.timeout 写入入口共用同一
# 集合——异常生产入口封闭不能只靠异常构造器, 账本写入也必须 fail-closed,
# 否则旁路调用可写入未登记 stage 污染账本语义。四值 = 三值 + legacy
# provider_wall(历史兼容读取, 读取端按 wall_clock 族处理)。
TIMEOUT_STAGES = frozenset(
    {"first_event", "stream_idle", "wall_clock", "provider_declared", "provider_wall"}
)


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
    input_tokens: int | None = None
    cached_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    provider_usage_reported: bool = False
    cache_suspected: bool = False


@dataclass(frozen=True)
class ModelCallTimeoutParams:
    call_id: str
    timeout_seconds: float
    timeout_stage: str
    # 门槛2: 掐断时刻的真实墙钟经过(now - record.started_at), 证据链贯通——
    # record 层时间戳 + 参数层 elapsed 双持, 不再断在调用点。
    elapsed_seconds: float = 0.0
    # 门槛2(终审补证 seq1613c): 最后活动到超时的静默时长; None=缺失/未计算,
    # 数值(含 0.0)=真实计算——「缺失/回退」与「真实零静默」结构化可辨识。
    idle_silence_seconds: float | None = None


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
    accounted_input_tokens: int = 0
    cached_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    provider_usage_reported: bool = False
    output_tokens_seen: int = 0
    timeout_seconds: float | None = None
    timeout_stage: str = ""
    # 门槛2: 调用点算出的掐断时刻墙钟经过(now - started_at), 与 total_latency_seconds
    # 双持——total_latency 是 ledger 落账时刻自己算的, elapsed 是掐断时刻调用点算的,
    # 两者同源同值, 互相校验防账本自证。
    elapsed_seconds: float = 0.0
    # 门槛2(终审补证 seq1613c): 最后活动到超时的静默时长; None=缺失/未计算,
    # 数值(含 0.0)=真实计算——「缺失/回退」与「真实零静默」可辨识。
    idle_silence_seconds: float | None = None
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
            "accounted_input_tokens": self.accounted_input_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "cache_creation_input_tokens": self.cache_creation_input_tokens,
            "provider_usage_reported": self.provider_usage_reported,
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


# LLM: 聚合态只保存计数、去重键和展示集合，不保存 prompt、响应正文、请求体或密钥。
# 类用途: 在详细调用记录被裁剪后继续保留某个 request/run 的准确累计统计。
@dataclass
class _ModelCallAggregate:
    logical_call_counts: dict[str, int] = field(default_factory=dict)
    physical_model_attempt_count: int = 0
    provider_http_attempt_count: int = 0
    provider_http_retry_count: int = 0
    status_counts: dict[str, int] = field(default_factory=dict)
    backends: set[str] = field(default_factory=set)
    models: set[str] = field(default_factory=set)
    accounted_input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    provider_usage_call_count: int = 0
    estimated_usage_call_count: int = 0
    provider_input_tokens: int = 0
    provider_output_tokens: int = 0
    provider_cache_read_input_tokens: int = 0
    provider_cache_write_input_tokens: int = 0
    estimated_input_tokens: int = 0
    estimated_output_tokens: int = 0

    # LLM: 每个新 call_id 恰好调用一次；同 call_id 的状态变化必须走 observe_update，避免重复累计。
    # 函数用途: 把一条新模型调用加入累计统计，并登记逻辑回合、后端、模型和初始状态。
    def observe_new(self, record: ModelCallRecord) -> None:
        logical_call_id = _logical_call_id(record)
        self.logical_call_counts[logical_call_id] = (
            self.logical_call_counts.get(logical_call_id, 0) + 1
        )
        self.physical_model_attempt_count += 1
        self.provider_http_attempt_count += max(0, record.provider_attempt_count)
        self.provider_http_retry_count += max(0, record.provider_attempt_count - 1)
        self.status_counts[record.status] = self.status_counts.get(record.status, 0) + 1
        if record.backend:
            self.backends.add(record.backend)
        if record.model:
            self.models.add(record.model)
        self._observe_usage_delta(None, record)

    # LLM: replacement delta 维护当前终态分布和尝试数；身份字段变化也必须成对撤销旧值再登记新值。
    # 函数用途: 在同一调用收到首 token、完成、失败、超时或 HTTP 尝试事件时更新累计统计。
    def observe_update(
        self,
        previous: ModelCallRecord,
        current: ModelCallRecord,
    ) -> None:
        previous_logical = _logical_call_id(previous)
        current_logical = _logical_call_id(current)
        if previous_logical != current_logical:
            _decrement_counter(self.logical_call_counts, previous_logical)
            self.logical_call_counts[current_logical] = (
                self.logical_call_counts.get(current_logical, 0) + 1
            )
        if previous.status != current.status:
            _decrement_counter(self.status_counts, previous.status)
            self.status_counts[current.status] = self.status_counts.get(current.status, 0) + 1
        provider_delta = current.provider_attempt_count - previous.provider_attempt_count
        retry_delta = max(0, current.provider_attempt_count - 1) - max(
            0,
            previous.provider_attempt_count - 1,
        )
        self.provider_http_attempt_count = max(
            0,
            self.provider_http_attempt_count + provider_delta,
        )
        self.provider_http_retry_count = max(
            0,
            self.provider_http_retry_count + retry_delta,
        )
        if current.backend:
            self.backends.add(current.backend)
        if current.model:
            self.models.add(current.model)
        self._observe_usage_delta(previous, current)

    # LLM: Aggregate token totals are maintained by replacement deltas so detail pruning never
    # truncates long-task usage and repeated finish events remain idempotent.
    # 函数用途: 按同一 call 的新旧快照增量更新 token 总账，避免重复收尾多计。
    def _observe_usage_delta(
        self,
        previous: ModelCallRecord | None,
        current: ModelCallRecord,
    ) -> None:
        old = previous or ModelCallRecord("", "", "", 0)
        self.accounted_input_tokens = max(
            0,
            self.accounted_input_tokens
            + current.accounted_input_tokens
            - old.accounted_input_tokens,
        )
        self.output_tokens = max(
            0,
            self.output_tokens + current.output_tokens - old.output_tokens,
        )
        self.cached_input_tokens = max(
            0,
            self.cached_input_tokens
            + current.cached_input_tokens
            - old.cached_input_tokens,
        )
        self.cache_creation_input_tokens = max(
            0,
            self.cache_creation_input_tokens
            + current.cache_creation_input_tokens
            - old.cache_creation_input_tokens,
        )
        old_reported = int(old.provider_usage_reported and old.status == "finished")
        new_reported = int(
            current.provider_usage_reported and current.status == "finished"
        )
        old_estimated = int(
            not old.provider_usage_reported and old.status == "finished"
        )
        new_estimated = int(
            not current.provider_usage_reported and current.status == "finished"
        )
        self.provider_usage_call_count = max(
            0, self.provider_usage_call_count + new_reported - old_reported
        )
        self.estimated_usage_call_count = max(
            0, self.estimated_usage_call_count + new_estimated - old_estimated
        )
        old_usage = _partitioned_usage(old)
        new_usage = _partitioned_usage(current)
        for field_name in (
            "provider_input_tokens",
            "provider_output_tokens",
            "provider_cache_read_input_tokens",
            "provider_cache_write_input_tokens",
            "estimated_input_tokens",
            "estimated_output_tokens",
        ):
            setattr(
                self,
                field_name,
                max(
                    0,
                    int(getattr(self, field_name))
                    + int(new_usage[field_name])
                    - int(old_usage[field_name]),
                ),
            )

    # LLM: 返回值只含公开统计，不泄露内部 logical id 集合或可变容器引用。
    # 函数用途: 生成最终结果和 runtime facts 可直接消费的累计统计快照。
    def to_summary(self) -> dict[str, object]:
        logical_count = len(self.logical_call_counts)
        physical_count = max(0, self.physical_model_attempt_count)
        statuses = {
            status: max(0, int(self.status_counts.get(status, 0)))
            for status in ("started", "first_token", "finished", "failed", "timed_out")
        }
        return {
            "logical_model_turn_count": logical_count,
            "physical_model_attempt_count": physical_count,
            "model_retry_count": max(0, physical_count - logical_count),
            "provider_http_attempt_count": max(0, self.provider_http_attempt_count),
            "provider_http_retry_count": max(0, self.provider_http_retry_count),
            "status_counts": statuses,
            "backends": sorted(self.backends),
            "models": sorted(self.models),
            "accounted_input_tokens": max(0, self.accounted_input_tokens),
            "output_tokens": max(0, self.output_tokens),
            "total_tokens": max(
                0, self.accounted_input_tokens + self.output_tokens
            ),
            "cached_input_tokens": max(0, self.cached_input_tokens),
            "cache_creation_input_tokens": max(
                0, self.cache_creation_input_tokens
            ),
            "provider_usage_call_count": max(0, self.provider_usage_call_count),
            "estimated_usage_call_count": max(0, self.estimated_usage_call_count),
            "usage_breakdown": {
                "schema": "model_usage_breakdown.v1",
                "provider": {
                    "input_tokens": max(0, self.provider_input_tokens),
                    "output_tokens": max(0, self.provider_output_tokens),
                    "cache_read_input_tokens": max(
                        0, self.provider_cache_read_input_tokens
                    ),
                    "cache_write_input_tokens": max(
                        0, self.provider_cache_write_input_tokens
                    ),
                    "call_count": max(0, self.provider_usage_call_count),
                },
                "estimated": {
                    "input_tokens": max(0, self.estimated_input_tokens),
                    "output_tokens": max(0, self.estimated_output_tokens),
                    "call_count": max(0, self.estimated_usage_call_count),
                },
            },
        }


# LLM: Provider truth and fallback estimates share legacy compatibility totals,
# but this partition is the only cost-grade view and never fills a missing
# provider field from an estimate.
# 函数用途: 将一条已结束调用拆成供应商真值或本地估算，供累计账本独立求和。
def _partitioned_usage(record: ModelCallRecord) -> dict[str, int]:
    empty = {
        "provider_input_tokens": 0,
        "provider_output_tokens": 0,
        "provider_cache_read_input_tokens": 0,
        "provider_cache_write_input_tokens": 0,
        "estimated_input_tokens": 0,
        "estimated_output_tokens": 0,
    }
    if record.status != "finished":
        return empty
    if record.provider_usage_reported:
        return {
            **empty,
            "provider_input_tokens": max(0, int(record.accounted_input_tokens)),
            "provider_output_tokens": max(0, int(record.output_tokens)),
            "provider_cache_read_input_tokens": max(
                0, int(record.cached_input_tokens)
            ),
            "provider_cache_write_input_tokens": max(
                0, int(record.cache_creation_input_tokens)
            ),
        }
    return {
        **empty,
        "estimated_input_tokens": max(0, int(record.accounted_input_tokens)),
        "estimated_output_tokens": max(0, int(record.output_tokens)),
    }


# LLM: ledger 是模型调用观测的唯一进程内事实源；明细有界，但当前 request/run 累计值必须保持准确。
# 类用途: 线程安全地登记模型调用生命周期，并向超时估算和最终响应提供记录与汇总。
class ModelCallLedger:
    # LLM: 聚合 scope 使用有界 LRU，容量至少覆盖全部 retained record 的 request/run 双键。
    # 函数用途: 初始化线程安全明细账本和不会被 128 条明细裁剪截断的累计统计。
    def __init__(
        self,
        options: ModelCallLedgerOptions | None = None,
        context: ModelCallLedgerContext | None = None,
    ) -> None:
        self.options = options or ModelCallLedgerOptions()
        self.context = context or ModelCallLedgerContext()
        self._records: list[ModelCallRecord] = []
        self._index: dict[str, int] = {}
        self._scope_aggregates: dict[tuple[str, str], _ModelCallAggregate] = {}
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
                accounted_input_tokens=max(
                    0,
                    int(
                        record.input_tokens
                        if params.input_tokens is None
                        else params.input_tokens
                    ),
                ),
                cached_input_tokens=max(0, int(params.cached_input_tokens)),
                cache_creation_input_tokens=max(
                    0, int(params.cache_creation_input_tokens)
                ),
                provider_usage_reported=bool(params.provider_usage_reported),
                cache_suspected=record.cache_suspected or params.cache_suspected,
                events=_append_event(record.events, "finished"),
            )
            self._replace(updated)
            return updated

    def timeout(self, params: ModelCallTimeoutParams) -> ModelCallRecord:
        # 门槛2 终审边界②: 账本写入 stage 封闭——未知 stage 抛 ValueError
        # (fail-closed), 与 ProviderTimeoutError 构造入口同一合同集合, 防旁路
        # 调用把未登记 stage 写进账本(legacy 读取语义由 TIMEOUT_STAGES 承载)。
        if params.timeout_stage not in TIMEOUT_STAGES:
            raise ValueError(
                f"未知 timeout_stage: {params.timeout_stage!r} "
                f"(合法: {sorted(TIMEOUT_STAGES)})"
            )
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
                idle_silence_seconds=(
                    max(0.0, float(params.idle_silence_seconds))
                    if params.idle_silence_seconds is not None
                    else None
                ),
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

    # LLM: request_id 优先于 run_id，与最终化调用方现有筛选合同一致；返回副本而非内部 aggregate。
    # 函数用途: 查询一次请求或运行的完整累计调用统计，即使早期明细已经超过上限被裁剪。
    def cumulative_summary(
        self,
        *,
        request_id: str = "",
        run_id: str = "",
    ) -> dict[str, object] | None:
        scope_key = _selected_scope_key(request_id=request_id, run_id=run_id)
        if scope_key is None:
            return None
        with self._lock:
            aggregate = self._scope_aggregates.get(scope_key)
            return aggregate.to_summary() if aggregate is not None else None

    # LLM: 新 call_id 先进入累计态再裁剪明细；重复 call_id 只走 replacement delta，不能重复计数。
    # 函数用途: 新增或替换一条模型调用记录，并维持累计统计与明细索引一致。
    def _append_or_replace(self, record: ModelCallRecord) -> None:
        if record.call_id in self._index:
            self._replace(record)
            return
        self._records.append(record)
        for aggregate in self._aggregates_for(record):
            aggregate.observe_new(record)
        self._rebuild_index()
        self._trim_records()

    # LLM: 所有生命周期更新必须经过此入口，先计算 aggregate delta，再原子替换 retained detail。
    # 函数用途: 更新同一调用的状态、时延或 HTTP 尝试，同时保持累计统计准确。
    def _replace(self, record: ModelCallRecord) -> None:
        index = self._index[record.call_id]
        previous = self._records[index]
        for aggregate in self._aggregates_for(record):
            aggregate.observe_update(previous, record)
        self._records[index] = record

    # LLM: scope LRU 只限制历史 request/run 数，不删除当前 retained detail 对应的累计键。
    # 函数用途: 取得一条调用所属 request/run 的累计容器，并控制长寿命进程的统计内存上限。
    def _aggregates_for(self, record: ModelCallRecord) -> tuple[_ModelCallAggregate, ...]:
        aggregates: list[_ModelCallAggregate] = []
        for scope_key in _model_call_scope_keys(record):
            aggregate = self._scope_aggregates.pop(scope_key, None)
            if aggregate is None:
                aggregate = _ModelCallAggregate()
            self._scope_aggregates[scope_key] = aggregate
            aggregates.append(aggregate)
        max_scopes = max(2, max(1, int(self.options.max_records)) * 2)
        while len(self._scope_aggregates) > max_scopes:
            oldest_scope = next(iter(self._scope_aggregates))
            del self._scope_aggregates[oldest_scope]
        return tuple(aggregates)

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


# LLM: scope identity 只读结构化 request_id/run_id，禁止从 prompt、错误文案或路径推断。
# 函数用途: 列出一条调用所属的请求和运行统计键，空身份不建账。
def _model_call_scope_keys(record: ModelCallRecord) -> tuple[tuple[str, str], ...]:
    keys: list[tuple[str, str]] = []
    if record.request_id:
        keys.append(("request", record.request_id))
    if record.run_id:
        keys.append(("run", record.run_id))
    return tuple(keys)


# LLM: 查询优先级必须与旧 model_call_summary 筛选条件一致，避免同传两种 identity 时重复合并。
# 函数用途: 把调用方给出的 request/run 参数解析为唯一累计统计键。
def _selected_scope_key(
    *,
    request_id: str,
    run_id: str,
) -> tuple[str, str] | None:
    normalized_request = str(request_id or "").strip()
    if normalized_request:
        return ("request", normalized_request)
    normalized_run = str(run_id or "").strip()
    if normalized_run:
        return ("run", normalized_run)
    return None


# LLM: logical_call_id 是重试去重事实；缺失时只回退稳定 call_id，不解析其它 metadata 文本。
# 函数用途: 取得累计统计用于区分逻辑回合与物理重试的唯一键。
def _logical_call_id(record: ModelCallRecord) -> str:
    return str(record.metadata.get("logical_call_id") or record.call_id)


# LLM: 引用计数归零时删除键，防逻辑回合或状态集合残留幽灵项。
# 函数用途: 安全减少内部引用计数，供罕见的同 call_id 身份或状态替换使用。
def _decrement_counter(counter: dict[str, int], key: str) -> None:
    remaining = int(counter.get(key, 0)) - 1
    if remaining > 0:
        counter[key] = remaining
    else:
        counter.pop(key, None)


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
