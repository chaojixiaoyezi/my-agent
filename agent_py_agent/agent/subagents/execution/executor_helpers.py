
from __future__ import annotations

"""Small helpers used by the closeout test executor."""

import os
import shlex
import shutil
import subprocess
import sys
import time
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


def _split_command(command: str) -> list[str]:
    """Split a command string using the current platform's shell quoting rules."""

    argv = shlex.split(command, posix=os.name != "nt")
    if os.name == "nt":
        return [_strip_wrapping_quotes(item) for item in argv]
    return argv


def _execution_argv(argv: list[str]) -> list[str]:
    if argv and _command_name(argv[0]) == "python" and shutil.which(argv[0]) is None:
        return [sys.executable, *argv[1:]]
    return argv


def _strip_wrapping_quotes(value: str) -> str:
    """Remove one matching pair of wrapping quotes from an argv item."""

    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def _command_name(value: str) -> str:
    """Return a normalized executable name for allowlist checks."""

    name = Path(value.strip('"')).name.lower()
    return name[:-4] if name.endswith(".exe") else name


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


def _command_rejected_record(
    test: dict[str, Any],
    command: str,
    error: str,
) -> TestExecutionRecord:
    """Return a failed command validation record."""

    return TestExecutionRecord(
        test_name=_test_name(test, default=command),
        command=command,
        validation_method="command",
        error=error,
        validation_result={"ok": False, "reason": "command_rejected"},
    )


def _command_timeout_record(
    test: dict[str, Any],
    command: str,
    exc: subprocess.TimeoutExpired,
    timing: tuple[float, float],
) -> TestExecutionRecord:
    """Return a failed command timeout record."""

    timeout_seconds, start = timing
    return TestExecutionRecord(
        test_name=_test_name(test, default=command),
        command=command,
        executed=False,
        stdout=exc.stdout or "",
        stderr=exc.stderr or "",
        duration_seconds=time.monotonic() - start,
        executed_at=_utc_now_iso(),
        error=f"命令超时 ({timeout_seconds:g}s)",
        validation_method="command",
        validation_result={"ok": False, "reason": "timeout"},
    )


def _command_error_record(
    test: dict[str, Any],
    command: str,
    exc: Exception,
    start: float,
) -> TestExecutionRecord:
    """Return a failed command execution-error record."""

    return TestExecutionRecord(
        test_name=_test_name(test, default=command),
        command=command,
        executed=False,
        duration_seconds=time.monotonic() - start,
        executed_at=_utc_now_iso(),
        error=str(exc),
        validation_method="command",
        validation_result={"ok": False, "reason": "execution_error"},
    )


def _command_completed_record(
    test: dict[str, Any],
    command: str,
    completed: subprocess.CompletedProcess,
    start: float,
) -> TestExecutionRecord:
    """Return a completed command validation record."""

    return TestExecutionRecord(
        test_name=_test_name(test, default=command),
        command=command,
        executed=True,
        exit_code=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
        duration_seconds=time.monotonic() - start,
        executed_at=_utc_now_iso(),
        validation_method="command",
        validation_result={"ok": completed.returncode == 0, "exit_code": completed.returncode},
    )


def _with_working_dir(record: TestExecutionRecord, working_dir: Path) -> TestExecutionRecord:
    record.metadata["working_dir"] = str(working_dir)
    record.validation_result.setdefault("working_dir", str(working_dir))
    return record


def _utc_now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
