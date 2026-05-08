# LLM: Tool-output fail-safe checkpoints run before large outputs are externalized.
# 模块用途: 在黑盒/大工具输出写 artifact 前留下恢复快照，避免外置失败时什么都没留下。

from __future__ import annotations

"""Fail-safe recovery snapshots for large tool outputs."""

import hashlib
from pathlib import Path
from typing import Any

from ..memory_archive import ExternalizeToolOutputRequest, snapshots

FAIL_SAFE_NEXT_ACTION = "先读取工具输出 artifact 摘要和 fail-safe checkpoint，再决定是否把内容切片读回 prompt。"


# LLM: write_tool_output_fail_safe_checkpoint writes metadata only, never the full tool output body.
# 函数用途: 大工具输出外置前写恢复快照，返回可合并到归档记录的状态字段。
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


# LLM: _empty_checkpoint returns the stable no-checkpoint shape used by archive metadata.
# 函数用途: 在无需写快照或写快照失败时返回统一字段，避免调用方分支处理。
def _empty_checkpoint() -> dict[str, object]:
    return {
        "fail_safe_checkpoint_written": False,
        "fail_safe_checkpoint_path": "",
        "fail_safe_checkpoint_id": "",
        "fail_safe_checkpoint_error": "",
    }


# LLM: _tool_call_snapshot records output identity without copying the output body.
# 函数用途: 构建工具调用恢复元数据，只保存 hash、大小、工具名和调用 id。
def _tool_call_snapshot(request: ExternalizeToolOutputRequest, output: str) -> dict[str, Any]:
    return {
        "tool": request.tool,
        "id": request.call_id,
        "ok": request.ok,
        "output_hash": hashlib.sha256(output.encode("utf-8")).hexdigest(),
        "output_size_bytes": len(output.encode("utf-8")),
        "output_externalized": "pending",
    }
