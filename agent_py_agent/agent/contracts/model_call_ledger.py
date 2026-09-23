
from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

from .model_call_budget import (
    ModelCallBudgetError,
    ModelCallInputBudget,
    ModelCallInputBudgetMethods,
    ModelCallInputBudgetState,
)

# LLM: 本模块是调用、累计用量及显式输入预留的唯一事实源；预算只在原锁/容器扩展，普通调用行为及首个终态保持。
# 模块用途: 记录模型调用、真实用量和后台传输事实，按 request/run 提供主调用、辅助调用及决策统计。

# 门槛2 终审边界②(steward seq1622-2): stage 合同单一事实源下沉到账本层。
# ProviderTimeoutError 构造入口与 ModelCallLedger.timeout 写入入口共用同一
# 集合——异常生产入口封闭不能只靠异常构造器, 账本写入也必须 fail-closed,
# 否则旁路调用可写入未登记 stage 污染账本语义。四值 = 三值 + legacy
# provider_wall(历史兼容读取, 读取端按 wall_clock 族处理)。
TIMEOUT_STAGES = frozenset(
    {"first_event", "stream_idle", "wall_clock", "provider_declared", "provider_wall"}
)
_TERMINAL_STATUSES = frozenset({"failed", "finished", "timed_out"})
_USAGE_FIELDS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_write_input_tokens")
_PURPOSE_BUCKETS = ("main", "auxiliary", "decision")


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


# LLM: provider_usage_fields=None 保留旧信封级来源；显式字段列表只声明已报告事实，不能用估算补供应商缺值。
# 类用途: 描述一次成功收尾的用量和结束事实，允许输入与输出各自保留不同来源。
@dataclass(frozen=True)
class ModelCallFinishParams:
    call_id: str
    output_tokens: int = 0
    input_tokens: int | None = None
    cached_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    provider_usage_reported: bool = False
    cache_suspected: bool = False
    # LLM: 流末结构化事实：空正文/断流类事故必须能从调用账本直接读出，而不是事后从别的事件反推。
    # 字段用途: 记录 provider stop_reason、运行时归一原因与本轮结束原因（诊断用，不参与裁决）。
    stop_reason: str = ""
    runtime_reason: str = ""
    turn_end_reason: str = ""
    truncated: bool = False
    provider_usage_fields: tuple[str, ...] | None = None


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
    request_surface: dict[str, Any] = field(default_factory=dict)


# LLM: 调用身份及首个终态固定；可追加传输事实，provider_usage_fields 缺失仍沿历史全有或全无来源解释。
# 类用途: 保存一次物理模型调用的生命周期、token 来源和 HTTP 观察结果，供原累计账本更新。
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
    # LLM: 流末结构化事实（诊断）：空正文/断流事故不再需要从 attempt 事件侧反推。
    # 字段用途: 保存 provider stop_reason、运行时归一原因与本轮结束原因。
    stop_reason: str = ""
    runtime_reason: str = ""
    turn_end_reason: str = ""
    truncated: bool = False
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
    provider_usage_fields: tuple[str, ...] | None = None

    # LLM: 只投影已有结构化事实；None 与空列表分别表示历史来源未知和本次未报告任何已知 token 字段。
    # 函数用途: 生成可序列化的调用明细，保留按字段用量来源以便诊断部分报告。
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
            "provider_usage_fields": list(self.provider_usage_fields) if self.provider_usage_fields is not None else None,
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


# LLM: 这是原累计容器及其用途分区共用的计数实现，不建立第二账本；逻辑身份包含用途，字段来源缺失保留历史解释。
# 类用途: 增量累计调用、HTTP 尝试和 token，供总计与各用途分区使用同一计算规则。
@dataclass
class _ModelCallTotals:
    logical_call_counts: dict[tuple[str, str], int] = field(default_factory=dict)
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
    provider_input_tokens_reported_call_count: int = 0
    provider_output_tokens_reported_call_count: int = 0
    provider_cache_read_input_tokens_reported_call_count: int = 0
    provider_cache_write_input_tokens_reported_call_count: int = 0

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

    # LLM: 按原记录与新记录的差值更新；信封级调用次数保持历史意义，字段级计次只累计显式已报事实。
    # 函数用途: 增量更新 token 总量及来源，部分报告不会把缺失字段的估算误写成供应商真值。
    def _observe_usage_delta(
        self,
        previous: ModelCallRecord | None,
        current: ModelCallRecord,
    ) -> None:
        old = previous or ModelCallRecord("", "", "", 0)
        for field_name in ("accounted_input_tokens", "output_tokens", "cached_input_tokens", "cache_creation_input_tokens"):
            setattr(self, field_name, max(0, getattr(self, field_name) + getattr(current, field_name) - getattr(old, field_name)))
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
        for field_name in new_usage:
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

    # LLM: 旧总量和信封调用计数保持意义；新字段计数仅代表明确报告，不能把 legacy None 解释成真实零。
    # 函数用途: 生成公开统计快照，帮助消费端区分部分报告、完全缺失与真实零 token。
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
                    **{f"{name}_reported_call_count": max(0, getattr(self, f"provider_{name}_reported_call_count")) for name in _USAGE_FIELDS},
                },
                "estimated": {
                    "input_tokens": max(0, self.estimated_input_tokens),
                    "output_tokens": max(0, self.estimated_output_tokens),
                    "call_count": max(0, self.estimated_usage_call_count),
                },
            },
        }


# LLM: 显式 provider_usage_fields 按字段拆账；None 只为旧调用保留原信封级分类，不能推断旧字段是否真实报告。
# 函数用途: 将成功调用的每个 token 字段归到供应商或本地估算，并独立记录已报告字段的次数。
def _partitioned_usage(record: ModelCallRecord) -> dict[str, int]:
    empty = {
        "provider_input_tokens": 0,
        "provider_output_tokens": 0,
        "provider_cache_read_input_tokens": 0,
        "provider_cache_write_input_tokens": 0,
        "estimated_input_tokens": 0,
        "estimated_output_tokens": 0,
        **{f"provider_{name}_reported_call_count": 0 for name in _USAGE_FIELDS},
    }
    if record.status != "finished":
        return empty
    explicit_fields = record.provider_usage_fields
    reported = set(explicit_fields) if explicit_fields is not None else set(_USAGE_FIELDS if record.provider_usage_reported else ())
    values = (record.accounted_input_tokens, record.output_tokens, record.cached_input_tokens, record.cache_creation_input_tokens)
    for name, value in zip(_USAGE_FIELDS, values, strict=True):
        if name in reported:
            empty[f"provider_{name}"] = max(0, int(value))
        elif name in {"input_tokens", "output_tokens"}:
            empty[f"estimated_{name}"] = max(0, int(value))
        empty[f"provider_{name}_reported_call_count"] = int(explicit_fields is not None and name in reported)
    return empty


# LLM: 用途来自宿主结构化 metadata；未声明 decision 的辅助调用仍归辅助，普通调用默认归主桶。
# 函数用途: 为累计账选一个互斥用途分区，不读取提示词、后端名或响应正文猜测用途。
def _model_call_purpose(record: ModelCallRecord) -> str:
    if record.metadata.get("purpose") == "decision":
        return "decision"
    return "auxiliary" if record.metadata.get("auxiliary") is True else "main"


# LLM: 用途分区和显式输入预算附着原 scope 容器；普通累计守原 LRU 配额，预算仅在固定期限前额外保留，不建另一事实源。
# 类用途: 在原累计账内维护用途总量或宿主显式创建的输入预算。
@dataclass
class _ModelCallAggregate(_ModelCallTotals):
    input_budget: ModelCallInputBudgetState | None = None
    usage_scope_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    purpose_totals: dict[str, _ModelCallTotals] = field(default_factory=lambda: {name: _ModelCallTotals() for name in _PURPOSE_BUCKETS})

    # LLM: 同一新 call_id 同步进入总账和唯一用途桶；两处更新均由外层 ledger 锁保护。
    # 函数用途: 登记一次新调用，并保留其用途分区计数。
    def observe_new(self, record: ModelCallRecord) -> None:
        super().observe_new(record)
        self.purpose_totals[_model_call_purpose(record)].observe_new(record)

    # LLM: started 后身份及用途不再改变；后续终态和迟到 HTTP 事实按同一新旧记录同步更新原分区。
    # 函数用途: 更新调用的总计和用途计数，不新建调用或改变统计代次。
    def observe_update(self, previous: ModelCallRecord, current: ModelCallRecord) -> None:
        super().observe_update(previous, current)
        self.purpose_totals[_model_call_purpose(current)].observe_update(previous, current)

    # LLM: 分区只有统计字段，不含独立代次或嵌套分区；旧持久事件未声明分区时不能事后猜测用途。
    # 函数用途: 在原累计摘要中附加三类调用的用量和尝试统计。
    def to_summary(self) -> dict[str, object]:
        return {
            **super().to_summary(),
            "purpose_breakdown": {
                "schema": "model_call_purpose_breakdown.v1",
                **{name: totals.to_summary() for name, totals in self.purpose_totals.items()},
            },
        }


# LLM: 只供无累计态的旧明细和空摘要使用；运行中的 request/run 必须优先读取原 ledger 的增量累计态。
# 函数用途: 复用累计规则投影一组保留明细，避免兼容入口另维护用量来源和用途规则。
def summarize_model_call_records(records: tuple[ModelCallRecord, ...] | list[ModelCallRecord]) -> dict[str, object]:
    aggregate = _ModelCallAggregate()
    for record in records:
        aggregate.observe_new(record)
    return aggregate.to_summary()


# LLM: 句柄只能由 ledger.retain_call 建立；每个持有者独立释放，worker 句柄必须随真实退出释放，不能由超时 caller 代劳。
# 类用途: 在有界 worker 尚未退出时保留准确调用明细及累计作用域，支持 finally 释放或上下文管理。
class ModelCallRetention:
    # LLM: 构造只绑定原账本与 call_id，登记由持锁的 retain_call 完成；不得直接构造并假定已有保留权。
    # 函数用途: 创建精确保留令牌，由原账本登记并管理其有效期。
    def __init__(self, ledger: ModelCallLedger, call_id: str) -> None:
        self._ledger = ledger
        self._call_id = call_id

    # LLM: 已释放句柄不可重用，不允许同名新调用继承旧保留权；有效性只读取原账本令牌集合。
    # 函数用途: 进入实际 worker 或 caller 的保留区间，拒绝已失效句柄。
    def __enter__(self) -> ModelCallRetention:
        with self._ledger._lock:
            if self not in self._ledger._retained_calls.get(self._call_id, ()):
                raise RuntimeError("模型调用保留句柄已释放。")
        return self

    # LLM: 异常退出也必须释放准确令牌；不吞异常，不改变调用成功、失败或超时状态。
    # 函数用途: 离开上下文时解除本持有者的保留，让原异常继续传播。
    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.release()

    # LLM: 释放在原 ledger 锁内幂等执行；多个持有者互不代替，最后一个释放才允许裁剪。
    # 函数用途: 在实际资源生命周期结束的 finally 中解除自己的保留，不启动清理线程。
    def release(self) -> None:
        self._ledger._release_retained_call(self)


# LLM: ledger 是唯一调用事实源；显式预算原语不接普通调用，HTTP 观察不充当发送硬门，保留仍沿原 worker 生命周期。
# 类用途: 线程安全地登记调用及用途用量，并保留仍有物理观察来源的准确记录，避免迟到响应重新打开终态。
class ModelCallLedger(ModelCallInputBudgetMethods):
    # LLM: 普通 scope 保持原有界 LRU；实例代次只用于拒绝旧实验重建余额，初始化不建立任何实验容器。
    # 函数用途: 初始化原调用账、累计统计和保留令牌集合，不另建执行池或记账入口。
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
        self._retained_calls: dict[str, set[ModelCallRetention]] = {}
        self._lock = threading.RLock()
        self.ledger_id = uuid.uuid4().hex

    # LLM: 仅宿主明确新授权建立原累计容器；同编号绝不重置，异进程代次拒绝，普通调用不进入此方法。
    # 函数用途: 在原模型账中登记固定预算；丢失账本后的旧授权不能重新使用。
    def begin_input_budget(self, limits: ModelCallInputBudget) -> dict:
        if not isinstance(limits, ModelCallInputBudget) or limits.ledger_id != self.ledger_id:
            raise ModelCallBudgetError("budget_ledger_mismatch")
        with self._lock:
            key = ("input_budget", limits.budget_id)
            aggregate = self._scope_aggregates.get(key)
            if aggregate is not None:
                if aggregate.input_budget is None or aggregate.input_budget.limits != limits:
                    raise ModelCallBudgetError("budget_conflict")
                return aggregate.input_budget.snapshot(float(self.context.now()))
            if float(self.context.now()) >= limits.deadline:
                raise ModelCallBudgetError("budget_expired")
            state = ModelCallInputBudgetState(limits)
            self._scope_aggregates[key] = _ModelCallAggregate(input_budget=state)
            self._trim_scope_aggregates()
            return state.snapshot(float(self.context.now()))

    # LLM: 只保留已经存在的 call；caller 和 worker 可各持令牌，各自 finally 释放，worker 总量沿原 bounded 准入限制。
    # 函数用途: 立即为准确调用登记一个保留句柄，避免正常裁剪先于逻辑收尾或物理退出。
    def retain_call(self, call_id: str) -> ModelCallRetention:
        with self._lock:
            self._require_record(call_id)
            token = ModelCallRetention(self, call_id)
            self._retained_calls.setdefault(call_id, set()).add(token)
            return token

    # LLM: 建立调用与 caller 保留必须处在同一原锁内，避免首次登记后被其他线程裁剪；此入口不预分配 worker 保留。
    # 函数用途: 原子建立一次调用并返回 caller 句柄，供有界调用等待和逻辑收尾使用。
    def started_retained(self, params: ModelCallStartedParams) -> tuple[ModelCallRecord, ModelCallRetention]:
        with self._lock:
            record = self.started(params)
            return record, self.retain_call(record.call_id)

    # LLM: 仅移除本令牌；重复释放无副作用，最后一个持有者离开后同步恢复明细及 scope 的原裁剪上限。
    # 函数用途: 回收一个准确调用保留权，不替其他 caller 或 worker 提前释放。
    def _release_retained_call(self, token: ModelCallRetention) -> None:
        with self._lock:
            tokens = self._retained_calls.get(token._call_id)
            if tokens is None or token not in tokens:
                return
            tokens.remove(token)
            if not tokens:
                del self._retained_calls[token._call_id]
            self._trim_records()
            self._trim_scope_aggregates()

    # LLM: call_id 是一次物理调用的唯一身份；重复启动不得改身份、清空用量或重开终态，真正重试必须使用新 call_id。
    # 函数用途: 建立调用明细与累计记录，重复登记时返回原记录。
    def started(self, params: ModelCallStartedParams) -> ModelCallRecord:
        with self._lock:
            if params.call_id in self._index:
                return self._require_record(params.call_id)
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

    # LLM: 首 token 只推进活动调用；超时或失败后的迟到流事件不能把终态重开，也不能改变终态时延。
    # 函数用途: 为尚未终结的调用登记首段输出及延迟，重复或迟到通知直接返回原记录。
    def first_token(self, params: ModelCallFirstTokenParams) -> ModelCallRecord:
        with self._lock:
            record = self._require_record(params.call_id)
            if record.first_token_at is not None or record.status in _TERMINAL_STATUSES:
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

    # LLM: 流活动只更新活动调用，不增加事件数组；终态时钟和输出计数不能被迟到片段改写。
    # 函数用途: 更新尚在运行的流式输出进度，供闲置超时判断使用。
    def activity(self, params: ModelCallActivityParams) -> ModelCallRecord:
        """Record streamed output activity without growing the event ledger."""

        with self._lock:
            record = self._require_record(params.call_id)
            if record.status in _TERMINAL_STATUSES:
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

    # LLM: 首个终态获胜；重复成功和取消后迟到成功均不改写原状态、用量或时间，字段来源随首次收尾冻结。
    # 函数用途: 一次性记录成功调用及各 token 字段来源，保留旧调用未声明来源时的历史解释。
    def finished(self, params: ModelCallFinishParams) -> ModelCallRecord:
        with self._lock:
            record = self._require_record(params.call_id)
            if record.status in _TERMINAL_STATUSES:
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
                provider_usage_fields=tuple(params.provider_usage_fields) if params.provider_usage_fields is not None else None,
                cache_suspected=record.cache_suspected or params.cache_suspected,
                stop_reason=str(params.stop_reason or record.stop_reason or ""),
                runtime_reason=str(params.runtime_reason or record.runtime_reason or ""),
                turn_end_reason=str(params.turn_end_reason or record.turn_end_reason or ""),
                truncated=bool(params.truncated or record.truncated),
                events=_append_event(record.events, "finished"),
            )
            self._replace(updated)
            return updated

    # LLM: 超时阶段必须属于原合同；只有活动调用能首次终结，迟到或重复 timeout 不覆盖成功与失败。
    # 函数用途: 保存首次超时的阶段和时长，保持调用终态单调。
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
            if record.status in _TERMINAL_STATUSES:
                return record
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

    # LLM: 失败只终结活动调用；不能把已成功或已超时记录改成另一失败，首次错误码和耗时保持稳定。
    # 函数用途: 一次性登记调用失败，重复或迟到异常仅返回原记录。
    def failed(self, params: ModelCallFailureParams) -> ModelCallRecord:
        with self._lock:
            record = self._require_record(params.call_id)
            if record.status in _TERMINAL_STATUSES:
                return record
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

    # LLM: 物理观察允许晚于逻辑终态，但只修改 HTTP 事实及诊断；终态时钟、状态和用量不变，请求面仅按同线程或 run 比较。
    # 函数用途: 记录真实 HTTP 尝试与缓存前缀变化，保留取消后仍在退出的 worker 观察结果。
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
            metadata = dict(record.metadata)
            if params.request_surface and "request_surface" not in metadata:
                from ..backends.cache_diagnostics import compare_request_surfaces

                thread_id = metadata.get("thread_id")
                previous = next((item for item in reversed(self._records) if item.call_id != record.call_id
                    and item.metadata.get("request_surface")
                    and (item.metadata.get("thread_id") == thread_id if thread_id else item.run_id == record.run_id)), None)
                metadata["request_surface"] = params.request_surface
                metadata["cache_diagnostic"] = compare_request_surfaces(
                    previous.metadata["request_surface"] if previous else {}, params.request_surface,
                )
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
                last_activity_at=record.last_activity_at if record.status in _TERMINAL_STATUSES else now,
                provider_attempt_count=len(attempts),
                metadata=metadata,
                provider_attempts=tuple(attempts),
                events=_append_event(record.events, f"provider_attempt_{params.status}"),
            )
            self._replace(updated)
            return updated

    def records(self) -> tuple[ModelCallRecord, ...]:
        with self._lock:
            return tuple(self._records)

    # LLM: request_id 优先；累计容器的唯一代次随摘要返回，进程重启或 LRU 重建不得沿用旧游标。
    # 函数用途: 查询一次请求的完整累计用量及统计代次，前后台交接复用、真正新调用另计。
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
            return {**aggregate.to_summary(), "usage_scope_id": aggregate.usage_scope_id} if aggregate is not None else None

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

    # LLM: 原 scope 容器身份不随 pin 改变；保留调用的 request/run 在物理观察结束前不得被 LRU 淘汰。
    # 函数用途: 取得调用所属累计容器，并按普通窗口与准确保留权清理历史统计。
    def _aggregates_for(self, record: ModelCallRecord) -> tuple[_ModelCallAggregate, ...]:
        aggregates: list[_ModelCallAggregate] = []
        for scope_key in _model_call_scope_keys(record):
            aggregate = self._scope_aggregates.pop(scope_key, None)
            if aggregate is None:
                aggregate = _ModelCallAggregate()
            self._scope_aggregates[scope_key] = aggregate
            aggregates.append(aggregate)
        self._trim_scope_aggregates()
        return tuple(aggregates)

    # LLM: 普通 scope 保持原 LRU；原输入预算在固定期限前保留，包括撤销态，避免裁剪后同编号重建额度。
    # 函数用途: 清理历史累计容器，同时保留物理调用来源及尚未过期的显式预算原账。
    def _trim_scope_aggregates(self) -> None:
        max_scopes = max(2, max(1, int(self.options.max_records)) * 2)
        if len(self._scope_aggregates) <= max_scopes:
            return
        budgets = {key: aggregate.input_budget for key, aggregate in self._scope_aggregates.items()
                   if aggregate.input_budget is not None}
        keep = set(tuple(key for key in self._scope_aggregates if key not in budgets)[-max_scopes:])
        if budgets:
            now = float(self.context.now())
            keep.update(key for key, budget in budgets.items() if now < budget.limits.deadline)
        for call_id in self._retained_calls:
            keep.update(_model_call_scope_keys(self._require_record(call_id)))
        for scope_key in tuple(self._scope_aggregates):
            if scope_key not in keep:
                del self._scope_aggregates[scope_key]

    def _require_record(self, call_id: str) -> ModelCallRecord:
        if call_id not in self._index:
            raise KeyError(f"unknown model call id: {call_id}")
        return self._records[self._index[call_id]]

    # LLM: 普通明细仍只保留最新 max_records；额外记录必须有显式活令牌，解除后立即恢复裁剪，不保留全部终态。
    # 函数用途: 清理过期明细，但让尚未收尾的 caller 或 worker 继续向准确调用写事实。
    def _trim_records(self) -> None:
        max_records = max(1, int(self.options.max_records))
        if len(self._records) <= max_records:
            return
        keep = set(self._retained_calls) | {record.call_id for record in self._records[-max_records:]}
        self._records = [record for record in self._records if record.call_id in keep]
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


# LLM: logical_call_id 只在同一用途桶中去重；不同用途不能因宿主误用同名 ID 而少计，也不能从自然语言推断身份。
# 函数用途: 取得带用途命名空间的逻辑回合键，使总计与各分区可以准确相加。
def _logical_call_id(record: ModelCallRecord) -> tuple[str, str]:
    return _model_call_purpose(record), str(record.metadata.get("logical_call_id") or record.call_id)


# LLM: 引用计数归零时删除键，防逻辑回合或状态集合残留幽灵项。
# 函数用途: 安全减少内部引用计数，供罕见的同 call_id 身份或状态替换使用。
def _decrement_counter(counter: dict[Any, int], key: Any) -> None:
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
    "ModelCallRetention",
    "ModelCallProviderAttemptParams",
    "ModelCallStartedParams",
    "ModelCallTimeoutParams",
    "summarize_model_call_records",
]
