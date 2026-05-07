# LLM: Log-analysis module; keep ingest, query, and detector data contracts stable.
# 模块用途: 支撑日志导入、查询、检测、案例和分析报告生成。

from __future__ import annotations

from dataclasses import dataclass, field

from .base import LogParser, ParserError
from .common import DEFAULT_PAYLOAD_MAX_CHARS
from .security_alert_v1 import SecurityAlertV1Parser


# LLM: parser 层把外部日志格式规范化成统一事件字段；修改 ParserRegistry 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 ParserRegistry 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass
class ParserRegistry:
    """Small parser registry for local ingest."""

    parsers: dict[str, LogParser] = field(default_factory=dict)

    # LLM: parser 层把外部日志格式规范化成统一事件字段；修改 register 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 完成 register 在当前模块中的核心转换或协调步骤，衔接 parser 层把外部日志格式规范化成统一事件字段。
    def register(self, parser: LogParser) -> None:
        self.parsers[parser.parser_id] = parser

    # LLM: parser 层把外部日志格式规范化成统一事件字段；修改 get 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 完成 get 在当前模块中的核心转换或协调步骤，衔接 parser 层把外部日志格式规范化成统一事件字段。
    def get(self, parser_id: str) -> LogParser:
        try:
            return self.parsers[parser_id]
        except KeyError as exc:
            raise ParserError(f"unknown parser_id: {parser_id}") from exc

    # LLM: parser 层把外部日志格式规范化成统一事件字段；修改 choose 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 完成 choose 在当前模块中的核心转换或协调步骤，衔接 parser 层把外部日志格式规范化成统一事件字段。
    def choose(self, *, parser_id: str = "auto", file_format: str | None = None) -> LogParser:
        if parser_id and parser_id != "auto":
            parser = self.get(parser_id)
            if file_format and file_format not in parser.supported_formats:
                raise ParserError(f"parser {parser_id} does not support {file_format}")
            return parser

        for parser in self.parsers.values():
            if file_format is None or file_format in parser.supported_formats:
                return parser
        raise ParserError(f"no parser supports format: {file_format or 'unknown'}")


# LLM: parser 层把外部日志格式规范化成统一事件字段；修改 default_registry 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 default registry 在当前模块中的核心转换或协调步骤，衔接 parser 层把外部日志格式规范化成统一事件字段。
def default_registry(*, payload_max_chars: int = DEFAULT_PAYLOAD_MAX_CHARS) -> ParserRegistry:
    registry = ParserRegistry()
    registry.register(SecurityAlertV1Parser(payload_max_chars=payload_max_chars))
    return registry


# LLM: parser 层把外部日志格式规范化成统一事件字段；修改 get_default_parser 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 读取 get default parser 需要的文件、记录或配置，并整理成调用方可直接使用的结果。
def get_default_parser(*, payload_max_chars: int = DEFAULT_PAYLOAD_MAX_CHARS) -> LogParser:
    return default_registry(payload_max_chars=payload_max_chars).get("security_alert_v1")

