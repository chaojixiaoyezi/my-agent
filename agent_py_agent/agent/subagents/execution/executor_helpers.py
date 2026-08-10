
from __future__ import annotations

"""Small helpers used by the closeout test executor.

审计 R0:command 执行路径已删除,所有 command 专用 helper(_split_command /
_execution_argv / _command_name / _command_*_record / _with_working_dir)一并
移除;仅保留进程内文件检查(file_check / content_check)所需的 helper。
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .records import TestExecutionRecord


@dataclass(frozen=True)
class ContentMatchRecordRequest:
    test: dict[str, Any]
    path: Path
    pattern: str
    exact: bool = False
    expect_absent: bool = False


def _test_name(test: dict[str, Any], default: str = "") -> str:
    """Return a readable test name for records."""

    return str(test.get("name") or default or "unknown").strip()[:120]


def _file_result(path: Path, exists: bool) -> dict[str, Any]:
    """Return metadata for a file_check validation."""

    result: dict[str, Any] = {"ok": exists, "exists": exists, "path": str(path)}
    if exists:
        stat = path.stat()
        result.update({"size": stat.st_size, "modified_at": stat.st_mtime})
    return result


def _content_match_record(request: ContentMatchRecordRequest) -> TestExecutionRecord:
    """Return a content_check validation record."""

    text = request.path.read_text(encoding="utf-8", errors="replace")
    matched = text == request.pattern if request.exact else request.pattern in text
    ok = not matched if request.expect_absent else matched
    mode = "exact" if request.exact else "contains"
    if request.expect_absent:
        mode = "not_contains"
    return TestExecutionRecord(
        test_name=_test_name(request.test),
        executed=True,
        exit_code=0 if ok else 1,
        executed_at=_utc_now_iso(),
        validation_method="content_check",
        validation_result={
            "ok": ok,
            "matched": matched,
            "path": str(request.path),
            "pattern": request.pattern,
            "match_mode": mode,
            "expect_absent": request.expect_absent,
        },
        error="" if ok else _content_match_error(request),
    )


def _content_match_error(request: ContentMatchRecordRequest) -> str:
    if request.expect_absent:
        return "内容不应出现"
    return "内容不相等" if request.exact else "内容未匹配"


def _file_record(
    test: dict[str, Any],
    method: str,
    *,
    error: str,
    path: Path | None = None,
) -> TestExecutionRecord:
    """Return a failed file/content validation record."""

    result: dict[str, Any] = {"ok": False, "reason": error}
    if path is not None:
        result["path"] = str(path)
    return TestExecutionRecord(
        test_name=_test_name(test),
        executed=False,
        exit_code=1,
        executed_at=_utc_now_iso(),
        error=error,
        validation_method=method,
        validation_result=result,
    )


def _utc_now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
