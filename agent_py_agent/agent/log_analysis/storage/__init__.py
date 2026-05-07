# LLM: Log-analysis module; keep ingest, query, and detector data contracts stable.
# 模块用途: 支撑日志导入、查询、检测、案例和分析报告生成。

from __future__ import annotations

from .base import (
    DEFAULT_QUERY_LIMIT,
    MAX_QUERY_LIMIT,
    SUPPORTED_QUERY_FIELDS,
    Case,
    EvidenceRef,
    Finding,
    JsonlReadAudit,
    LogAnalysisStore,
    NormalizedEvent,
    QueryCriteria,
    QueryRecord,
    QueryResult,
)
from .local_store import LocalLogStore


# Lazy import to break circular dependency:
# query.py → cases.evidence → storage.base
# LLM: 日志分析存储层读写本地事件、finding、case 和 evidence 投影；修改 __getattr__ 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 getattr 在当前模块中的核心转换或协调步骤，衔接 日志分析存储层读写本地事件、finding、case 和 evidence 投影。
def __getattr__(name: str):
    if name in ("execute_security_query", "sanitize_event_for_preview", "summarize_rows"):
        from .query import (
            execute_security_query,
            sanitize_event_for_preview,
            summarize_rows,
        )
        globals()["execute_security_query"] = execute_security_query
        globals()["sanitize_event_for_preview"] = sanitize_event_for_preview
        globals()["summarize_rows"] = summarize_rows
        return globals()[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = [
    "Case",
    "DEFAULT_QUERY_LIMIT",
    "EvidenceRef",
    "Finding",
    "JsonlReadAudit",
    "LocalLogStore",
    "LogAnalysisStore",
    "MAX_QUERY_LIMIT",
    "NormalizedEvent",
    "QueryCriteria",
    "QueryRecord",
    "QueryResult",
    "SUPPORTED_QUERY_FIELDS",
    "execute_security_query",
    "sanitize_event_for_preview",
    "summarize_rows",
]
