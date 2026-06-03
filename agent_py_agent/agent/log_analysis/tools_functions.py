
from __future__ import annotations

"""Compatibility facade for log-analysis security query functions."""

from .tools.query_functions import (
    MAX_TRACE_CASE_QUERIES,
    SecurityQueryParams,
    _case_seeds,
    _extend_seed,
    _store,
    _tool_response,
    hunt_ip,
    security_hunt_domain,
    security_hunt_ip,
    security_query,
    security_trace_case,
    trace_case,
)

__all__ = [
    "MAX_TRACE_CASE_QUERIES",
    "SecurityQueryParams",
    "_case_seeds",
    "_extend_seed",
    "_store",
    "_tool_response",
    "hunt_ip",
    "security_hunt_domain",
    "security_hunt_ip",
    "security_query",
    "security_trace_case",
    "trace_case",
]
