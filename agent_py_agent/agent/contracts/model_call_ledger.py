# LLM: Model call ledger stores provider request timing as structured machine facts.
# 模块用途: 记录模型调用 started、first_token、finished 和 timeout 事件，供超时估算和恢复逻辑读取。

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any


# LLM: ModelCallLedgerOptions keeps ledger retention policy out of method signatures.
# 类用途: 配置账本最多保留多少条模型调用记录。
@dataclass(frozen=True)
class ModelCallLedgerOptions:
    max_records: int = 128


# LLM: ModelCallLedgerContext injects clock access for deterministic tests and runtime isolation.
# 类用途: 保存账本运行时依赖；默认使用 monotonic，不读取自然语言日志。
@dataclass(frozen=True)
class ModelCallLedgerContext:
    now: Callable[[], float] = time.monotonic


# LLM: ModelCallStartedParams bundles structured facts known before provider generation begins.
# 类用途: 作为 started 事件的参数包，记录模型、后端、输入规模和运行引用。
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


# LLM: ModelCallFirstTokenParams carries structured first-token observations.
# 类用途: 作为 first_token 事件的参数包，记录可选输出 token 数和缓存疑似标记。
@dataclass(frozen=True)
class ModelCallFirstTokenParams:
    call_id: str
    output_tokens_seen: int = 1
    cache_suspected: bool = False


# LLM: ModelCallFinishParams carries structured completion observations.
# 类用途: 作为 finished 事件的参数包，记录完成时输出 token 数和缓存疑似标记。
@dataclass(frozen=True)
class ModelCallFinishParams:
    call_id: str
    output_tokens: int = 0
    cache_suspected: bool = False


# LLM: ModelCallTimeoutParams records timeout details without parsing provider prose.
# 类用途: 作为 timeout 事件的参数包，描述命中的超时预算和阶段。
@dataclass(frozen=True)
class ModelCallTimeoutParams:
    call_id: str
    timeout_seconds: float
    timeout_stage: str


# LLM: ModelCallRecord is the immutable model-call fact row consumed by monitors.
# 类用途: 保存一次模型调用的结构化状态、时间戳、耗时和 token 规模。
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
    first_token_at: float | None = None
    finished_at: float | None = None
    timeout_at: float | None = None
    first_token_latency_seconds: float | None = None
    total_latency_seconds: float | None = None
    output_tokens: int = 0
    output_tokens_seen: int = 0
    timeout_seconds: float | None = None
    timeout_stage: str = ""
    is_probe: bool = False
    cache_suspected: bool = False
    events: tuple[str, ...] = ("started",)
    metadata: dict[str, Any] = field(default_factory=dict)

    # LLM: to_dict gives persistence and tests a stable primitive payload.
    # 函数用途: 把模型调用记录转为普通 dict，避免调用方读取 dataclass 内部实现。
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
            "first_token_at": self.first_token_at,
            "finished_at": self.finished_at,
            "timeout_at": self.timeout_at,
            "first_token_latency_seconds": self.first_token_latency_seconds,
            "total_latency_seconds": self.total_latency_seconds,
            "output_tokens": self.output_tokens,
            "output_tokens_seen": self.output_tokens_seen,
            "timeout_seconds": self.timeout_seconds,
            "timeout_stage": self.timeout_stage,
            "is_probe": self.is_probe,
            "cache_suspected": self.cache_suspected,
            "events": list(self.events),
            "metadata": dict(self.metadata),
        }


# LLM: ModelCallLedger maintains an in-memory append-only view of model-call facts.
# 类用途: 提供 started、first_token、finished、timeout 写入接口和 records 读取接口。
class ModelCallLedger:
    # LLM: __init__ wires retention options and injectable clock into the ledger.
    # 函数用途: 初始化空账本和 call_id 索引，不执行文件或网络 I/O。
    def __init__(
        self,
        options: ModelCallLedgerOptions | None = None,
        context: ModelCallLedgerContext | None = None,
    ) -> None:
        self.options = options or ModelCallLedgerOptions()
        self.context = context or ModelCallLedgerContext()
        self._records: list[ModelCallRecord] = []
        self._index: dict[str, int] = {}

    # LLM: started writes the initial structured fact row for a provider call.
    # 函数用途: 记录模型调用开始事件并返回创建的记录。
    def started(self, params: ModelCallStartedParams) -> ModelCallRecord:
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
            is_probe=params.is_probe,
            metadata=dict(params.metadata),
        )
        self._append_or_replace(record)
        return record

    # LLM: first_token records the first streamed token timing for timeout learning.
    # 函数用途: 标记模型调用已产出首 token，并计算 started 到 first_token 的耗时。
    def first_token(self, params: ModelCallFirstTokenParams) -> ModelCallRecord:
        record = self._require_record(params.call_id)
        if record.first_token_at is not None:
            return record
        now = float(self.context.now())
        updated = replace(
            record,
            status="first_token",
            first_token_at=now,
            first_token_latency_seconds=max(0.0, now - record.started_at),
            output_tokens_seen=max(0, int(params.output_tokens_seen)),
            cache_suspected=record.cache_suspected or params.cache_suspected,
            events=record.events + ("first_token",),
        )
        self._replace(updated)
        return updated

    # LLM: finished records successful provider completion and total latency.
    # 函数用途: 标记模型调用完成，并保存输出 token 数和总耗时。
    def finished(self, params: ModelCallFinishParams) -> ModelCallRecord:
        record = self._require_record(params.call_id)
        now = float(self.context.now())
        updated = replace(
            record,
            status="finished",
            finished_at=now,
            total_latency_seconds=max(0.0, now - record.started_at),
            output_tokens=max(0, int(params.output_tokens)),
            cache_suspected=record.cache_suspected or params.cache_suspected,
            events=_append_event(record.events, "finished"),
        )
        self._replace(updated)
        return updated

    # LLM: timeout records provider timeout facts for recovery and future timeout budgets.
    # 函数用途: 标记模型调用超时，并保存超时预算、阶段和总等待时长。
    def timeout(self, params: ModelCallTimeoutParams) -> ModelCallRecord:
        record = self._require_record(params.call_id)
        now = float(self.context.now())
        updated = replace(
            record,
            status="timed_out",
            timeout_at=now,
            timeout_seconds=max(0.0, float(params.timeout_seconds)),
            timeout_stage=params.timeout_stage,
            total_latency_seconds=max(0.0, now - record.started_at),
            events=_append_event(record.events, "timeout"),
        )
        self._replace(updated)
        return updated

    # LLM: records exposes a stable snapshot for monitors without sharing mutable storage.
    # 函数用途: 返回当前账本记录的不可变 tuple，调用方可安全遍历。
    def records(self) -> tuple[ModelCallRecord, ...]:
        return tuple(self._records)

    # LLM: _append_or_replace keeps call_id uniqueness and retention limits consistent.
    # 函数用途: 插入新记录或替换同 call_id 记录，并按 max_records 修剪旧记录。
    def _append_or_replace(self, record: ModelCallRecord) -> None:
        if record.call_id in self._index:
            self._replace(record)
            return
        self._records.append(record)
        self._rebuild_index()
        self._trim_records()

    # LLM: _replace swaps one immutable record in the ledger by call_id.
    # 函数用途: 用更新后的记录替换旧记录，保持列表顺序不变。
    def _replace(self, record: ModelCallRecord) -> None:
        self._records[self._index[record.call_id]] = record

    # LLM: _require_record centralizes unknown call_id errors for event updates.
    # 函数用途: 根据 call_id 取记录；不存在时抛出 KeyError。
    def _require_record(self, call_id: str) -> ModelCallRecord:
        if call_id not in self._index:
            raise KeyError(f"unknown model call id: {call_id}")
        return self._records[self._index[call_id]]

    # LLM: _trim_records enforces bounded in-memory retention.
    # 函数用途: 超出 max_records 时丢弃最旧记录并重建索引。
    def _trim_records(self) -> None:
        max_records = max(1, int(self.options.max_records))
        if len(self._records) <= max_records:
            return
        self._records = self._records[-max_records:]
        self._rebuild_index()

    # LLM: _rebuild_index derives call_id positions from the current record list.
    # 函数用途: 在插入或修剪后刷新内部索引。
    def _rebuild_index(self) -> None:
        self._index = {record.call_id: index for index, record in enumerate(self._records)}


# LLM: _append_event prevents duplicate terminal events in repeated writes.
# 函数用途: 给事件 tuple 追加新阶段；最后一个事件相同时保持原值。
def _append_event(events: tuple[str, ...], event: str) -> tuple[str, ...]:
    if events and events[-1] == event:
        return events
    return events + (event,)


__all__ = [
    "ModelCallFinishParams",
    "ModelCallFirstTokenParams",
    "ModelCallLedger",
    "ModelCallLedgerContext",
    "ModelCallLedgerOptions",
    "ModelCallRecord",
    "ModelCallStartedParams",
    "ModelCallTimeoutParams",
]
