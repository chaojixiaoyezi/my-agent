
from __future__ import annotations

import json
from pathlib import Path

from .staged_checkpoint_tabular_shape import (
    contains_nonempty_list,
    tabular_json_shape_issue,
)


def artifact_path(ref: str, task_workspace: Path) -> Path:
    preferred = Path(str(ref or ""))
    path = preferred.resolve(strict=False) if preferred.is_absolute() else (task_workspace / preferred).resolve(strict=False)
    try:
        path.relative_to(task_workspace.resolve(strict=False))
    except ValueError as exc:
        raise ValueError("staged artifact path outside task workspace") from exc
    return path


def json_checkpoint_status(
    path: Path,
    required_columns: list[str] | None = None,
    required_sheets_min: int = 0,
) -> dict[str, str]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        return {"code": "STAGED_JSON_INVALID", "parse_error": str(exc)}
    except json.JSONDecodeError as exc:
        return {"code": "STAGED_JSON_INVALID", "parse_error": str(exc)}
    if not contains_nonempty_list(value):
        return {"code": "STAGED_JSON_NO_ROWS"}
    if shape_issue := tabular_json_shape_issue(
        value,
        required_columns=required_columns,
        required_sheets_min=required_sheets_min,
    ):
        return shape_issue
    return {"code": "OK"}


__all__ = ["artifact_path", "json_checkpoint_status"]
