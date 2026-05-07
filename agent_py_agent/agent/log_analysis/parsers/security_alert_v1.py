# LLM: Log-analysis module; keep ingest, query, and detector data contracts stable.
# 模块用途: 支撑日志导入、查询、检测、案例和分析报告生成。

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .base import ParseContext, ParsedRecord, ParserError
from .common import DEFAULT_PAYLOAD_MAX_CHARS, normalize_security_alert_v1


# LLM: parser 层把外部日志格式规范化成统一事件字段；修改 SecurityAlertV1Parser 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 SecurityAlertV1Parser 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass(slots=True)
class SecurityAlertV1Parser:
    """Parser for the first SecurityAlertV1 CSV/JSONL alert schema."""

    payload_max_chars: int = DEFAULT_PAYLOAD_MAX_CHARS

    parser_id: str = "security_alert_v1"
    schema: str = "SecurityAlertV1"
    supported_formats: tuple[str, ...] = ("jsonl", "csv", "log")

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
        parse_context = request or context or ParseContext(str(raw_ref), source_id, source_product, line_no)
        if not isinstance(record, Mapping):
            raise ParserError("SecurityAlertV1 record must be an object")
        if not any(value not in (None, "") for value in record.values()):
            raise ParserError("SecurityAlertV1 record is empty")

        event = normalize_security_alert_v1(
            record,
            raw_ref=parse_context.raw_ref,
            source_id=parse_context.source_id,
            source_product=parse_context.source_product,
            line_no=parse_context.line_no,
            payload_max_chars=self.payload_max_chars,
        )
        return ParsedRecord(
            event=event,
            parser_id=self.parser_id,
            parser_confidence=float(event["parser_confidence"]),
            raw_ref=parse_context.raw_ref,
            line_no=parse_context.line_no,
        )

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
        parse_context = request or context or ParseContext(str(raw_ref), source_id, source_product, line_no)
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ParserError(f"invalid JSON: {exc.msg}") from exc
        if not isinstance(record, Mapping):
            raise ParserError("JSONL SecurityAlertV1 line must contain a JSON object")
        return self.parse_record(
            record,
            request=parse_context,
        )

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
        parse_context = request or context or ParseContext(str(raw_ref), source_id, source_product, line_no)
        if None in row:
            raise ParserError("CSV row has more columns than the header")
        return self.parse_record(
            row,
            request=parse_context,
        )
