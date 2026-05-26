# LLM: Shared helpers for conversation ledger modules.
# 模块用途: 提供长期会话账本内部的时间、JSONL、路径名和 wake 字段整理函数。

from __future__ import annotations

import json
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from .models import ObservationEvent


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    rows: list[dict[str, Any]] = []
    for line in lines:
        row = _json_row(line)
        if row is not None:
            rows.append(row)
    return rows


def now(value: float | None = None) -> float:
    return float(time.time() if value is None else value)


def float_value(value: object) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def safe_file_stem(value: str) -> str:
    text = str(value or "").strip()
    return "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in text) or "unknown"


def wake_urgency(value: str) -> str:
    return "urgent" if str(value or "").strip().lower() == "urgent" else "normal"


def with_handled_at(event: ObservationEvent, handled: dict[str, float]) -> ObservationEvent:
    handled_at = float(handled.get(event.observation_id) or event.handled_at or 0.0)
    return event if handled_at == event.handled_at else replace(event, handled_at=handled_at)


def wake_evidence_refs(observation: ObservationEvent | None, explicit_refs: object) -> tuple[str, ...]:
    if explicit_refs is not None:
        refs = explicit_refs
    elif observation is not None:
        refs = observation.evidence_refs
    else:
        refs = ()
    return tuple(str(item) for item in refs if str(item or "").strip()) if isinstance(refs, (list, tuple)) else ()


def _json_row(line: str) -> dict[str, Any] | None:
    try:
        row = json.loads(line)
    except json.JSONDecodeError:
        return None
    return row if isinstance(row, dict) else None
