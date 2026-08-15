
from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

MAX_TRACE_DEPTH = 3


def trace_entry(
    stage: str,
    *,
    ref: str = "",
    path: str | Path = "",
    code: str = "",
) -> dict[str, str]:
    entry = {"stage": str(stage or "").strip()}
    if ref:
        entry["ref"] = str(ref)
    if path:
        entry["path"] = str(path)
    if code:
        entry["code"] = str(code)
    return {key: value for key, value in entry.items() if value}


def with_contract_trace(
    finding: dict[str, Any],
    entries: Iterable[dict[str, object]] = (),
    max_depth: int = MAX_TRACE_DEPTH,
) -> dict[str, object]:
    merged = dict(finding)
    trace = _trace_items(merged.get("trace"))
    trace.extend(_trace_items(list(entries)))
    if trace:
        merged["trace"] = trace[:max(1, int(max_depth))]
    return merged


def _trace_items(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict) and str(item.get("stage") or "").strip()]


__all__ = ["MAX_TRACE_DEPTH", "trace_entry", "with_contract_trace"]
