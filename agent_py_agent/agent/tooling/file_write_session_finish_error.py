# LLM: File write session finish errors are manifest facts used by generic repair flows.
# 模块用途: 将 finish 失败的错误码和可执行修复动作写回 manifest，避免 runtime 主文件继续变胖。

from __future__ import annotations

from typing import Any

from .file_write_session_io import write_manifest
from .file_write_session_models import FileWriteSessionPaths
from .models import ToolExecutionResult


# LLM: record_finish_error persists failed staged commits without trusting prompt text.
# 函数用途: finish 校验失败时，把错误码和 reset/abort 动作写回 manifest 供下一轮结构化修复。
def record_finish_error(
    session_id: str,
    paths: FileWriteSessionPaths,
    manifest: dict[str, Any],
    error: ToolExecutionResult,
) -> None:
    envelope = error.result_envelope if isinstance(error.result_envelope, dict) else {}
    manifest["last_finish_error"] = {
        "code": str(envelope.get("code") or error.error_code or ""),
        "error_code": error.error_code,
        "recommended_action": str(envelope.get("recommended_action") or ""),
        "artifact_integrity": envelope.get("artifact_integrity") or {},
        "target_path": envelope.get("target_path") or manifest.get("target_path") or {},
        "temp_path": envelope.get("temp_path") or str(paths.temp_path),
        "reset_tool_call": _reset_tool_call(session_id),
        "abort_tool_call": _abort_tool_call(session_id),
    }
    write_manifest(paths.manifest_path, manifest)


def _reset_tool_call(session_id: str) -> dict[str, object]:
    return {
        "tool": "file_write_session",
        "action": "reset",
        "session_id": session_id,
        "discard_chunks": True,
    }


def _abort_tool_call(session_id: str) -> dict[str, object]:
    return {
        "tool": "file_write_session",
        "action": "abort",
        "session_id": session_id,
        "discard_chunks": True,
    }

