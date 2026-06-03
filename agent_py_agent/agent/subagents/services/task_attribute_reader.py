from __future__ import annotations

"""Safe task attribute readers for recovery and takeover services."""


from pathlib import Path
from typing import Any


def task_text(task: Any, name: str) -> str:
    value = getattr(task, name, "")
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, int | float):
        return str(value)
    return ""


def task_status(task: Any) -> str:
    return task_text(task, "status").upper()


def task_role(task: Any) -> str:
    return task_text(task, "role")


def task_int(task: Any, name: str) -> int:
    try:
        return max(0, int(getattr(task, name, 0) or 0))
    except (TypeError, ValueError):
        return 0


def task_list(task: Any, name: str) -> list[str]:
    value = getattr(task, name, [])
    if not isinstance(value, list | tuple | set):
        return []
    return [str(item) for item in value if str(item or "").strip()]
