# LLM: structured_commit_validation keeps file_write_session finish honest for structured targets without task-specific rules.
# 模块用途: 在 session 提交最终文件前校验结构化目标内容，目前先覆盖 JSON，并保留统一扩展入口。

from __future__ import annotations

import json
from pathlib import Path

from ..contracts.staged_checkpoint_acceptance import json_checkpoint_status
from .artifact_integrity import ArtifactIntegrityCheckRequest, check_artifact_integrity
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
    if suffix in {".html", ".htm"}:
        return _validate_html_commit(session_id=session_id, target=target, temp_path=temp_path)
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


# LLM: HTML commit validation reuses artifact integrity codes instead of prompt-only web rules.
# 函数用途: 在 staged HTML 原子提交前拦住结构损坏；链接质量 warning 留给交付/验收合同判定。
def _validate_html_commit(
    *,
    session_id: str,
    target: Path,
    temp_path: Path,
) -> ToolExecutionResult | None:
    decision = check_artifact_integrity(
        ArtifactIntegrityCheckRequest(
            path=target,
            text=temp_path.read_text(encoding="utf-8"),
            require_complete=True,
        )
    )
    blocking_codes = _html_commit_blocking_codes(decision)
    if not blocking_codes:
        return None
    return failure(
        "TOOL_INVALID_ARGUMENTS",
        "ARTIFACT_INTEGRITY_FAILED",
        f"artifact integrity validation failed for {target.name}: {', '.join(blocking_codes)}",
        {
            "session_id": session_id,
            "format": "html",
            "target_path": str(target),
            "temp_path": str(temp_path),
            "artifact_integrity": _html_integrity_payload(target, decision, blocking_codes),
            "recommended_action": "repair_artifact_before_finish",
            "reset_tool_call": {
                "tool": "file_write_session",
                "action": "reset",
                "session_id": session_id,
                "discard_chunks": True,
            },
            "abort_tool_call": {
                "tool": "file_write_session",
                "action": "abort",
                "session_id": session_id,
                "discard_chunks": True,
            },
        },
    )


# LLM: _html_commit_blocking_codes keeps staging atomicity separate from delivery quality gates.
# 函数用途: 返回 HTML 提交必须阻塞的结构损坏 code；普通 warning 不阻断目标文件落盘。
def _html_commit_blocking_codes(decision) -> list[str]:
    return list(decision.blocker_codes)


# LLM: _html_integrity_payload bounds artifact diagnostics before they enter tool output.
# 函数用途: 将完整性检查结果压成结构化小包，供模型修复和测试断言使用。
def _html_integrity_payload(
    target: Path,
    decision,
    blocking_codes: list[str],
) -> dict[str, object]:
    return {
        "kind": decision.kind,
        "path": str(target),
        "ok": False,
        "blocker_codes": blocking_codes,
        "warning_codes": decision.warning_codes,
        "issues": [
            {
                "code": issue.code,
                "severity": issue.severity,
                "count": issue.count,
                "examples": issue.examples[:5],
                "message": issue.message,
            }
            for issue in decision.issues[:20]
        ],
    }
