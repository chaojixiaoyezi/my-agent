# LLM: Helper functions for TestExecutor records and command normalization.
# 模块用途: 承接父级验收执行器的命令解析、记录构造和时间戳 helper，保持主 executor 文件短小。

from __future__ import annotations

"""Small helpers used by the parent-acceptance test executor."""

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

from .execution_records import TestExecutionRecord


# LLM: ContentMatchRecordRequest keeps content-check options bundled for future match modes.
# 类用途: 保存内容验收记录构造所需的文件、模式和匹配选项，避免 helper 参数继续扩散。
@dataclass(frozen=True)
class ContentMatchRecordRequest:
    test: dict[str, Any]
    path: Path
    pattern: str
    exact: bool = False
    expect_absent: bool = False


# LLM: _split_command centralizes platform shlex mode so validation and execution see the same argv.
# 函数用途: 解析命令字符串；Windows 使用非 POSIX 模式，避免反斜杠路径被当成转义。
def _split_command(command: str) -> list[str]:
    """Split a command string using the current platform's shell quoting rules."""

    argv = shlex.split(command, posix=os.name != "nt")
    if os.name == "nt":
        return [_strip_wrapping_quotes(item) for item in argv]
    return argv


# LLM: _execution_argv makes portable `python ...` tests use the current interpreter when needed.
# 函数用途: 在系统没有 python 命令时，把首个 `python` 参数替换为当前解释器，保持验收测试跨平台可跑。
def _execution_argv(argv: list[str]) -> list[str]:
    if argv and _command_name(argv[0]) == "python" and shutil.which(argv[0]) is None:
        return [sys.executable, *argv[1:]]
    return argv


# LLM: _strip_wrapping_quotes repairs Windows shlex output while preserving inner quotes.
# 函数用途: 去掉参数最外层的一对引号，让 `python -c "print(...)"` 在 Windows 上按预期执行。
def _strip_wrapping_quotes(value: str) -> str:
    """Remove one matching pair of wrapping quotes from an argv item."""

    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


# LLM: _command_name normalizes executable names for allowlist checks across Windows and Unix.
# 函数用途: 提取命令名并去掉 Windows 的 .exe 后缀，让 allowlist 可以写稳定名字。
def _command_name(value: str) -> str:
    """Return a normalized executable name for allowlist checks."""

    name = Path(value.strip('"')).name.lower()
    return name[:-4] if name.endswith(".exe") else name


# LLM: _test_name keeps record names stable when runner output omits explicit names.
# 函数用途: 从测试项里提取人类可读名称；没有 name 时用命令或 unknown 兜底。
def _test_name(test: dict[str, Any], fallback: str = "") -> str:
    """Return a readable test name for records."""

    return str(test.get("name") or fallback or "unknown").strip()[:120]


# LLM: _file_result records file metadata without reading file contents.
# 函数用途: 构造 file_check 的验证结果；文件存在时只记录 size 和 mtime。
def _file_result(path: Path, exists: bool) -> dict[str, Any]:
    """Return metadata for a file_check validation."""

    result: dict[str, Any] = {"ok": exists, "exists": exists, "path": str(path)}
    if exists:
        stat = path.stat()
        result.update({"size": stat.st_size, "modified_at": stat.st_mtime})
    return result


# LLM: _content_match_record reads one workspace-local text file and supports exact matches for cat normalization.
# 函数用途: 构造内容检查记录；不会把完整文件正文写入 validation_result，可用于精确或包含匹配。
def _content_match_record(request: ContentMatchRecordRequest) -> TestExecutionRecord:
    """Return a content_check validation record."""

    # LLM: expect_absent is an explicit schema flag for negative content checks.
    # 函数用途: 支持正向包含、全文相等和负向不存在三种内容验收；负向检查必须由机器字段声明。
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


# LLM: _content_match_error keeps localized failure text beside match option handling.
# 函数用途: 根据内容检查模式返回用户可读的失败原因。
def _content_match_error(request: ContentMatchRecordRequest) -> str:
    if request.expect_absent:
        return "内容不应出现"
    return "内容不相等" if request.exact else "内容未匹配"


# LLM: _file_record builds rejected file/content validation records without duplicating failure shape.
# 函数用途: 构造文件类验证失败记录；不会读取文件，也不会写状态。
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


# LLM: _command_rejected_record keeps command validation failures in the same record shape as executed tests.
# 函数用途: 构造命令被安全校验拦截时的记录；不会执行命令。
def _command_rejected_record(
    test: dict[str, Any],
    command: str,
    error: str,
) -> TestExecutionRecord:
    """Return a failed command validation record."""

    return TestExecutionRecord(
        test_name=_test_name(test, fallback=command),
        command=command,
        validation_method="command",
        error=error,
        validation_result={"ok": False, "reason": "command_rejected"},
    )


# LLM: _command_timeout_record preserves partial subprocess output when a validation command times out.
# 函数用途: 构造命令超时记录，保留可用的 stdout/stderr 摘要，方便父级判断失败原因。
def _command_timeout_record(
    test: dict[str, Any],
    command: str,
    exc: subprocess.TimeoutExpired,
    timing: tuple[float, float],
) -> TestExecutionRecord:
    """Return a failed command timeout record."""

    timeout_seconds, start = timing
    return TestExecutionRecord(
        test_name=_test_name(test, fallback=command),
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


# LLM: _command_error_record converts platform subprocess failures into evidence instead of exceptions.
# 函数用途: 构造命令启动失败记录；调用方可以把它当验收失败证据继续展示。
def _command_error_record(
    test: dict[str, Any],
    command: str,
    exc: Exception,
    start: float,
) -> TestExecutionRecord:
    """Return a failed command execution-error record."""

    return TestExecutionRecord(
        test_name=_test_name(test, fallback=command),
        command=command,
        executed=False,
        duration_seconds=time.monotonic() - start,
        executed_at=_utc_now_iso(),
        error=str(exc),
        validation_method="command",
        validation_result={"ok": False, "reason": "execution_error"},
    )


# LLM: _command_completed_record creates the canonical command execution success/failure payload.
# 函数用途: 把 subprocess 完成结果转成验收记录；退出码非零也算 executed=True 但 passed=False。
def _command_completed_record(
    test: dict[str, Any],
    command: str,
    completed: subprocess.CompletedProcess,
    start: float,
) -> TestExecutionRecord:
    """Return a completed command validation record."""

    return TestExecutionRecord(
        test_name=_test_name(test, fallback=command),
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


# LLM: _with_working_dir records command cwd evidence without changing the pass/fail calculation.
# 函数用途: 给命令执行记录补充真实工作目录，方便排查父验收和子代理本地测试目录差异。
def _with_working_dir(record: TestExecutionRecord, working_dir: Path) -> TestExecutionRecord:
    record.metadata["working_dir"] = str(working_dir)
    record.validation_result.setdefault("working_dir", str(working_dir))
    return record


# LLM: _utc_now_iso gives records portable UTC timestamps without pulling in project-wide clock helpers.
# 函数用途: 生成 JSON 友好的 UTC 时间；用于执行证据，不作为排序唯一事实源。
def _utc_now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
