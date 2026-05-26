# LLM: Small shared helpers for the collaboration ledger; keep them enum-free.
# 模块用途: 提供协作账本内部通用的时间、JSONL 和开放字符串整理函数。

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


def strings(value: object) -> tuple[str, ...]:
    """Normalize optional open-world string lists without enum validation."""
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item) for item in value if str(item or "").strip())


def dict_items(value: object) -> tuple[dict[str, Any], ...]:
    """Keep valid open-world clue/hint objects and ignore only dirty items."""
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(dict(item) for item in value if isinstance(item, dict))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read tolerant JSONL rows; malformed rows do not drop the whole ledger."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    rows: list[dict[str, Any]] = []
    for line in lines:
        row = _json_object_row(line)
        if row is not None:
            rows.append(row)
    return rows


def now(value: float | None = None) -> float:
    """Return explicit test time or wall-clock time."""
    return float(time.time() if value is None else value)


def float_value(value: object) -> float:
    """Parse numeric metadata fields defensively."""
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _json_object_row(line: str) -> dict[str, Any] | None:
    try:
        row = json.loads(line)
    except json.JSONDecodeError:
        return None
    return row if isinstance(row, dict) else None
