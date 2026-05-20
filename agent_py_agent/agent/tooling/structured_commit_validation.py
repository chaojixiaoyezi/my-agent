# LLM: structured_commit_validation keeps file_write_session finish honest for structured targets without task-specific rules.
# 模块用途: 在 session 提交最终文件前校验结构化目标内容，目前先覆盖 JSON，并保留统一扩展入口。

from __future__ import annotations

import json
from pathlib import Path

from ..contracts.staged_checkpoint_acceptance import json_checkpoint_status
from .file_write_session_io import failure
from .models import ToolExecutionResult


# LLM: validate_structured_commit is the generic pre-commit hook for session-backed structured files.
# 函数用途: 在 file_write_session finish 前校验临时文件是否满足结构化格式；失败时返回结构化错误并保持 session 打开。
def validate_structured_commit(
    *,
    session_id: str,
    target: Path,
    temp_path: Path,
) -> ToolExecutionResult | None:
    suffix = target.suffix.lower()
    if suffix != ".json":
        return None
    try:
        value = json.loads(temp_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return failure(
            "TOOL_INVALID_ARGUMENTS",
            "STRUCTURED_FILE_INVALID",
            f"structured file validation failed for {target.name}: {exc}",
            {
                "session_id": session_id,
                "format": "json",
                "target_path": str(target),
                "temp_path": str(temp_path),
                "parse_error": str(exc),
                "recommended_action": "continue_or_repair_before_finish",
            },
        )
    if isinstance(value, dict) and "sheets" in value:
        status = json_checkpoint_status(temp_path)
        if status["code"] != "OK":
            return failure(
                "TOOL_INVALID_ARGUMENTS",
                status["code"],
                f"structured file validation failed for {target.name}: {status['code']}",
                {
                    **status,
                    "session_id": session_id,
                    "format": "json",
                    "target_path": str(target),
                    "temp_path": str(temp_path),
                    "recommended_action": "repair_structured_checkpoint_json",
                },
            )
    return None
