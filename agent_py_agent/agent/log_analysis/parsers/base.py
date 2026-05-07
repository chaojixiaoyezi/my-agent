# LLM: Log-analysis module; keep ingest, query, and detector data contracts stable.
# 模块用途: 支撑日志导入、查询、检测、案例和分析报告生成。

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol


# LLM: parser 层把外部日志格式规范化成统一事件字段；修改 ParserError 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 封装 ParserError 的状态和协作方法，作为当前模块对外复用的领域对象。
class ParserError(ValueError):
    """Raised when one raw record cannot be parsed into a normalized event."""


# LLM: parser 层把外部日志格式规范化成统一事件字段；修改 ParsedRecord 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 ParsedRecord 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass(frozen=True)
class ParsedRecord:
    """One successfully parsed log record."""

    event: dict[str, Any]
    parser_id: str
    parser_confidence: float
    raw_ref: str
    line_no: int | None = None


# LLM: parser 层把外部日志格式规范化成统一事件字段；修改 ParseFailure 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 ParseFailure 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass(frozen=True)
class ParseFailure:
    """A parse failure ready to be written to dead-letter storage."""

    reason: str
    raw_ref: str
    line_no: int | None = None
    raw_line: str | None = None
    raw_fields: Mapping[str, Any] = field(default_factory=dict)
    parser_id: str | None = None


# LLM: parser 层把外部日志格式规范化成统一事件字段；修改 ParseContext 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 ParseContext 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass(frozen=True)
class ParseContext:
    raw_ref: str
    source_id: str | None = None
    source_product: str | None = None
    line_no: int | None = None


# LLM: parser 层把外部日志格式规范化成统一事件字段；修改 LogParser 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 声明 LogParser 的接口契约，让调用方依赖方法签名而非具体实现。
class LogParser(Protocol):
    """Protocol implemented by log parsers used by the ingest pipeline."""

    parser_id: str
    schema: str
    supported_formats: tuple[str, ...]

    # LLM: parser 层把外部日志格式规范化成统一事件字段；修改 parse_record 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 从外部数据还原 parse record 需要的领域对象，统一缺省值和兼容字段。
    def parse_record(
        self,
        record: Mapping[str, Any],
        *,
        request: ParseContext | None = None,
        context: ParseContext | None = None,
        raw_ref: str = "",
        source_id: str | None = None,
        source_product: str | None = None,
        line_no: int | None = None,
    ) -> ParsedRecord:
        """Parse one already decoded mapping."""

    # LLM: parser 层把外部日志格式规范化成统一事件字段；修改 parse_json_line 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 从外部数据还原 parse json line 需要的领域对象，统一缺省值和兼容字段。
    def parse_json_line(
        self,
        line: str,
        *,
        request: ParseContext | None = None,
        context: ParseContext | None = None,
        raw_ref: str = "",
        source_id: str | None = None,
        source_product: str | None = None,
        line_no: int | None = None,
    ) -> ParsedRecord:
        """Parse one JSONL line."""

    # LLM: parser 层把外部日志格式规范化成统一事件字段；修改 parse_csv_row 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 从外部数据还原 parse csv row 需要的领域对象，统一缺省值和兼容字段。
    def parse_csv_row(
        self,
        row: Mapping[str, Any],
        *,
        request: ParseContext | None = None,
        context: ParseContext | None = None,
        raw_ref: str = "",
        source_id: str | None = None,
        source_product: str | None = None,
        line_no: int | None = None,
    ) -> ParsedRecord:
        """Parse one CSV row from csv.DictReader."""
