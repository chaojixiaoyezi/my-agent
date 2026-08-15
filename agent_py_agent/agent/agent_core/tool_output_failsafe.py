
from __future__ import annotations

"""Fail-safe recovery snapshots for large tool outputs."""

import hashlib
from pathlib import Path
from typing import Any

from ..memory_archive import ExternalizeToolOutputRequest, snapshots

FAIL_SAFE_NEXT_ACTION = "先读取工具输出 artifact 摘要和 fail-safe checkpoint，再决定是否把内容切片读回 prompt。"


def write_tool_output_fail_safe_checkpoint(request: ExternalizeToolOutputRequest) -> dict[str, object]:
    output = str(request.output or "")
    if len(output) < max(0, int(request.min_chars)):
        return _empty_checkpoint()
    try:
        result = snapshots.write_recovery_snapshot(
            Path(request.root),
            session_id=request.request_id or request.run_id or request.task_id or request.call_id,
            user_prompt="",
            response_text="工具输出即将外置为 artifact，当前记录为外置前 fail-safe checkpoint。",
            backend="tool",
            source="tool_output_externalizer",
            request_id=request.request_id,
            run_id=request.run_id,
            task_id=request.task_id,
            status="checkpoint_before_externalize",
            tool_calls=[_tool_call_snapshot(request, output)],
            task_refs=[request.run_id, request.task_id],
            next_actions=[FAIL_SAFE_NEXT_ACTION],
            archive_level=3,
        )
    except Exception as exc:
        return {**_empty_checkpoint(), "fail_safe_checkpoint_error": str(exc)}
    return {
        "fail_safe_checkpoint_written": bool(result.ok),
        "fail_safe_checkpoint_path": result.path,
        "fail_safe_checkpoint_id": result.snapshot_id,
        "fail_safe_checkpoint_error": result.error,
    }


def _empty_checkpoint() -> dict[str, object]:
    return {
        "fail_safe_checkpoint_written": False,
        "fail_safe_checkpoint_path": "",
        "fail_safe_checkpoint_id": "",
        "fail_safe_checkpoint_error": "",
    }


def _tool_call_snapshot(request: ExternalizeToolOutputRequest, output: str) -> dict[str, Any]:
    return {
        "tool": request.tool,
        "id": request.call_id,
        "ok": request.ok,
        "output_hash": hashlib.sha256(output.encode("utf-8")).hexdigest(),
        "output_size_bytes": len(output.encode("utf-8")),
        "output_externalized": "pending",
    }
