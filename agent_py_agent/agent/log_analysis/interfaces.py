# LLM: Log-analysis module; keep ingest, query, and detector data contracts stable.
# 模块用途: 支撑日志导入、查询、检测、案例和分析报告生成。

from __future__ import annotations

"""Public protocols for pluggable log analysis implementations."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from .models import (
    CaseRecord,
    Checkpoint,
    EvidenceRef,
    Finding,
    NormalizedEvent,
    RawBatch,
    SourceSpec,
)


# LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 ParseResult 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 ParseResult 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass
class ParseResult:
    batch_id: str
    parser_id: str
    events: list[NormalizedEvent] = field(default_factory=list)
    malformed_count: int = 0
    dead_letter_refs: list[str] = field(default_factory=list)
    parser_confidence: float = 0.0
    schema_summary: dict[str, Any] = field(default_factory=dict)
    sample_rows: list[dict[str, Any]] = field(default_factory=list)


# LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 QueryResult 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 QueryResult 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass
class QueryResult:
    query_id: str
    rows: list[dict[str, Any]] = field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    evidence_ref: EvidenceRef | None = None
    elapsed_ms: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


# LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 MLScoreResult 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 MLScoreResult 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass
class MLScoreResult:
    scoring_engine: str
    model_id: str
    model_version: str = ""
    feature_schema_version: str = ""
    risk_score: float = 0.0
    labels: list[str] = field(default_factory=list)
    explanations: list[dict[str, Any]] = field(default_factory=list)
    evidence_refs: list[EvidenceRef] = field(default_factory=list)
    error: str = ""


# LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 DispatchResult 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 DispatchResult 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass
class DispatchResult:
    case_id: str
    status: str
    run_id: str = ""
    queued_at: str = ""
    message: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


# LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 ResponseActionResult 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 ResponseActionResult 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass
class ResponseActionResult:
    action_id: str
    connector_id: str
    status: str
    dry_run: bool = True
    message: str = ""
    evidence_refs: list[EvidenceRef] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


# LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 LogSource 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 声明 LogSource 的接口契约，让调用方依赖方法签名而非具体实现。
@runtime_checkable
class LogSource(Protocol):
    # LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 source_spec 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 完成 source spec 在当前模块中的核心转换或协调步骤，衔接 日志分析模块围绕事件、查询、案例和报告传递结构化事实。
    @property
    def source_spec(self) -> SourceSpec:
        ...

    # LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 read_batch 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 读取 read batch 需要的文件、记录或配置，并整理成调用方可直接使用的结果。
    def read_batch(self, checkpoint: Checkpoint | None = None) -> RawBatch | None:
        ...

    # LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 commit 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 完成 commit 在当前模块中的核心转换或协调步骤，衔接 日志分析模块围绕事件、查询、案例和报告传递结构化事实。
    def commit(self, checkpoint: Checkpoint) -> None:
        ...

    # LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 health 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 完成 health 在当前模块中的核心转换或协调步骤，衔接 日志分析模块围绕事件、查询、案例和报告传递结构化事实。
    def health(self) -> dict[str, Any]:
        ...


# LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 Parser 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 声明 Parser 的接口契约，让调用方依赖方法签名而非具体实现。
@runtime_checkable
class Parser(Protocol):
    # LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 parser_id 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 计算 parser id 的稳定值、时间窗口或标识符，供去重、排序和检索使用。
    @property
    def parser_id(self) -> str:
        ...

    # LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 can_parse 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 完成 can parse 在当前模块中的核心转换或协调步骤，衔接 日志分析模块围绕事件、查询、案例和报告传递结构化事实。
    def can_parse(self, sample: str | bytes | Sequence[str], source_spec: SourceSpec) -> float:
        ...

    # LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 parse_batch 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 从外部数据还原 parse batch 需要的领域对象，统一缺省值和兼容字段。
    def parse_batch(self, raw_batch: RawBatch) -> ParseResult:
        ...


# LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 EventStore 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 声明 EventStore 的接口契约，让调用方依赖方法签名而非具体实现。
@runtime_checkable
class EventStore(Protocol):
    # LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 write_events 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 写入或登记 write events 相关记录，集中处理目标路径、格式化和状态更新。
    def write_events(self, events: Sequence[NormalizedEvent], *, batch: RawBatch | None = None) -> int:
        ...

    # LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 get_event 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 读取 get event 需要的文件、记录或配置，并整理成调用方可直接使用的结果。
    def get_event(self, event_id: str) -> NormalizedEvent | None:
        ...

    # LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 health 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 完成 health 在当前模块中的核心转换或协调步骤，衔接 日志分析模块围绕事件、查询、案例和报告传递结构化事实。
    def health(self) -> dict[str, Any]:
        ...


# LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 QueryEngine 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 声明 QueryEngine 的接口契约，让调用方依赖方法签名而非具体实现。
@runtime_checkable
class QueryEngine(Protocol):
    # LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 query 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 完成 query 在当前模块中的核心转换或协调步骤，衔接 日志分析模块围绕事件、查询、案例和报告传递结构化事实。
    def query(
        self,
        query: str,
        parameters: Mapping[str, Any] | None = None,
        *,
        limit: int = 100,
    ) -> QueryResult:
        ...

    # LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 topn 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 完成 topn 在当前模块中的核心转换或协调步骤，衔接 日志分析模块围绕事件、查询、案例和报告传递结构化事实。
    def topn(
        self,
        field_name: str,
        *,
        window: tuple[str, str] | None = None,
        limit: int = 20,
    ) -> QueryResult:
        ...

    # LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 sample 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 完成 sample 在当前模块中的核心转换或协调步骤，衔接 日志分析模块围绕事件、查询、案例和报告传递结构化事实。
    def sample(
        self,
        filters: Mapping[str, Any],
        *,
        limit: int = 100,
    ) -> QueryResult:
        ...


# LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 Detector 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 声明 Detector 的接口契约，让调用方依赖方法签名而非具体实现。
@runtime_checkable
class Detector(Protocol):
    # LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 detector_id 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 基于规则或事件字段计算 detector id 的判定结果，避免把推测当作事实写入。
    @property
    def detector_id(self) -> str:
        ...

    # LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 detect 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 基于规则或事件字段计算 detect 的判定结果，避免把推测当作事实写入。
    def detect(
        self,
        query_engine: QueryEngine,
        *,
        window: tuple[str, str],
        context: Mapping[str, Any] | None = None,
    ) -> Sequence[Finding]:
        ...


# LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 ScoringEngine 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 声明 ScoringEngine 的接口契约，让调用方依赖方法签名而非具体实现。
@runtime_checkable
class ScoringEngine(Protocol):
    # LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 score 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 基于规则或事件字段计算 score 的判定结果，避免把推测当作事实写入。
    def score(
        self,
        features: Mapping[str, Any],
        *,
        model_ref: str = "",
        context: Mapping[str, Any] | None = None,
    ) -> MLScoreResult:
        ...


# LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 DispatchEngine 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 声明 DispatchEngine 的接口契约，让调用方依赖方法签名而非具体实现。
@runtime_checkable
class DispatchEngine(Protocol):
    # LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 enqueue_case 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 推进 enqueue case 对应的调度、执行或处理步骤，并返回可追踪的状态结果。
    def enqueue_case(
        self,
        case: CaseRecord,
        *,
        context: Mapping[str, Any] | None = None,
    ) -> DispatchResult:
        ...

    # LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 health 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 完成 health 在当前模块中的核心转换或协调步骤，衔接 日志分析模块围绕事件、查询、案例和报告传递结构化事实。
    def health(self) -> dict[str, Any]:
        ...


# LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 ResponseConnector 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 声明 ResponseConnector 的接口契约，让调用方依赖方法签名而非具体实现。
@runtime_checkable
class ResponseConnector(Protocol):
    # LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 connector_id 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 计算 connector id 的稳定值、时间窗口或标识符，供去重、排序和检索使用。
    @property
    def connector_id(self) -> str:
        ...

    # LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 plan 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 完成 plan 在当前模块中的核心转换或协调步骤，衔接 日志分析模块围绕事件、查询、案例和报告传递结构化事实。
    def plan(
        self,
        case: CaseRecord,
        action: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        ...

    # LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 execute 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 完成 execute 在当前模块中的核心转换或协调步骤，衔接 日志分析模块围绕事件、查询、案例和报告传递结构化事实。
    def execute(
        self,
        case: CaseRecord,
        action: Mapping[str, Any],
        *,
        dry_run: bool = True,
    ) -> ResponseActionResult:
        ...


__all__ = [
    "Detector",
    "DispatchEngine",
    "DispatchResult",
    "EventStore",
    "LogSource",
    "MLScoreResult",
    "ParseResult",
    "Parser",
    "QueryEngine",
    "QueryResult",
    "ResponseActionResult",
    "ResponseConnector",
    "ScoringEngine",
]
