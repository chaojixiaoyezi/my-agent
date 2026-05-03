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
