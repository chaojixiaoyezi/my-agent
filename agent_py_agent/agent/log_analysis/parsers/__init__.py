# LLM: Log-analysis module; keep ingest, query, and detector data contracts stable.
# 模块用途: 支撑日志导入、查询、检测、案例和分析报告生成。

from __future__ import annotations

from .base import LogParser, ParsedRecord, ParseFailure, ParserError
from .common import (
    CHINESE_SECURITY_ALERT_FIELD_MAP,
    DEFAULT_PAYLOAD_MAX_CHARS,
    SECURITY_ALERT_V1_KEYS,
    normalize_security_alert_v1,
)
from .registry import ParserRegistry, default_registry, get_default_parser
from .security_alert_v1 import SecurityAlertV1Parser

__all__ = [
    "CHINESE_SECURITY_ALERT_FIELD_MAP",
    "DEFAULT_PAYLOAD_MAX_CHARS",
    "LogParser",
    "ParsedRecord",
    "ParseFailure",
    "ParserError",
    "ParserRegistry",
    "SECURITY_ALERT_V1_KEYS",
    "SecurityAlertV1Parser",
    "default_registry",
    "get_default_parser",
    "normalize_security_alert_v1",
]

