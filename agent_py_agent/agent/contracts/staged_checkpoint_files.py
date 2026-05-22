# LLM: Staged checkpoint file helpers validate workspace paths and JSON checkpoint shape.
# 模块用途: 为阶段产物验收提供通用文件级判断，和证据合同逻辑解耦。

from __future__ import annotations

import json
from pathlib import Path

from .staged_checkpoint_tabular_shape import (
    contains_nonempty_list,
    tabular_json_shape_issue,
)


# LLM: artifact_path resolves one workspace-relative checkpoint ref without accepting prose-derived paths.
# 函数用途: 把阶段 ref 解析到任务工作区里的绝对路径，保持和主验收一致的路径语义。
def artifact_path(ref: str, task_workspace: Path) -> Path:
    preferred = Path(str(ref or ""))
    path = preferred.resolve(strict=False) if preferred.is_absolute() else (task_workspace / preferred).resolve(strict=False)
    try:
        path.relative_to(task_workspace.resolve(strict=False))
    except ValueError as exc:
        raise ValueError("staged artifact path outside task workspace") from exc
    return path


# LLM: json_checkpoint_status separates invalid JSON from valid-but-empty structured data.
# 函数用途: 返回阶段 JSON 的结构状态，避免把被截断的 JSON 误判成“只是没有数据”。
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
