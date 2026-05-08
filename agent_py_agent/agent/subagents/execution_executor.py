# LLM: Minimal real acceptance validation executor; returns records and leaves acceptance decisions to callers.
# 模块用途: 执行父级验收需要的 command/file/content 检查，产出执行记录，但不写任务状态。

from __future__ import annotations

"""Bounded executor for real parent-acceptance validation items."""

import os
import shlex
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, ClassVar

from .execution_records import TestExecutionRecord


# LLM: TestExecutor performs bounded local validation and returns records; callers decide whether records affect acceptance.
# 类用途: 执行父级验收用的最小真实检查；会在 workspace 内执行 allowlist 命令或读取指定文件，但不写任务状态。
class TestExecutor:
    """Execute one real validation item for parent acceptance."""

    __test__: ClassVar[bool] = False
    ALLOWED_PREFIXES: ClassVar[frozenset[str]] = frozenset({
        "python",
        "python3",
        "py",
        "pytest",
    })
    BLOCKED_CHARS: ClassVar[frozenset[str]] = frozenset({"|", "&", ";", ">", "<", "`", "$"})
    DEFAULT_TIMEOUT_SECONDS: ClassVar[float] = 120.0
    MAX_TIMEOUT_SECONDS: ClassVar[float] = 300.0

    # LLM: __init__ resolves the workspace once and clamps timeout so executor use stays bounded.
    # 函数用途: 创建验收执行器；后续命令和文件检查都限制在这个 workspace 内。
    def __init__(
        self,
        workspace_root: str | Path,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        allowed_prefixes: set[str] | frozenset[str] | None = None,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.timeout_seconds = min(max(float(timeout_seconds), 0.1), self.MAX_TIMEOUT_SECONDS)
        prefixes = allowed_prefixes if allowed_prefixes is not None else self.ALLOWED_PREFIXES
        self.allowed_prefixes = frozenset(str(item).lower() for item in prefixes)

    # LLM: execute dispatches by validation_method and never raises for normal validation failures.
    # 函数用途: 执行一条 tests 项；未知方法会返回未执行记录，方便验收层保留失败证据。
    def execute(self, test: dict[str, Any]) -> TestExecutionRecord:
        """Execute a validation item and return its evidence record."""

        method = str(test.get("validation_method") or "command").strip() or "command"
        if method == "command":
            return self._execute_command(test)
        if method == "file_check":
            return self._check_file(test)
        if method == "content_check":
            return self._check_content(test)
        return TestExecutionRecord(
            test_name=_test_name(test),
            validation_method=method,
            error=f"未知验证方式: {method}",
            validation_result={"ok": False, "reason": "unknown_validation_method"},
        )

    # LLM: _execute_command uses subprocess without shell expansion after allowlist and character checks.
    # 函数用途: 执行 allowlist 内命令并记录真实退出码和输出摘要；失败和超时都会转成记录而不是抛出。
    def _execute_command(self, test: dict[str, Any]) -> TestExecutionRecord:
        command = str(test.get("command") or "").strip()
        error = self._validate_command(command)
        if error:
            return _command_rejected_record(test, command, error)
        argv = _split_command(command)
        start = time.monotonic()
        try:
            completed = subprocess.run(
                argv,
                cwd=self.workspace_root,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            return _command_timeout_record(test, command, exc, (self.timeout_seconds, start))
        except Exception as exc:  # pragma: no cover - platform-specific subprocess failures.
            return _command_error_record(test, command, exc, start)
        return _command_completed_record(test, command, completed, start)

    # LLM: _validate_command blocks shell syntax and restricts executable names before subprocess is called.
    # 函数用途: 校验命令是否可执行；它只返回中文错误文本，不负责真正运行命令。
    def _validate_command(self, command: str) -> str:
        if not command:
            return "空测试命令"
        if any(char in command for char in self.BLOCKED_CHARS):
            return "测试命令包含高风险 shell 字符"
        try:
            argv = _split_command(command)
        except ValueError as exc:
            return f"测试命令解析失败: {exc}"
        if not argv:
            return "空测试命令"
        executable = _command_name(argv[0])
        if executable not in self.allowed_prefixes:
            return f"测试命令不在 allowlist 内: {executable}"
        return ""

    # LLM: _check_file validates existence inside workspace and records metadata only.
    # 函数用途: 检查 workspace 内文件是否存在；只读取元数据，不读取正文。
    def _check_file(self, test: dict[str, Any]) -> TestExecutionRecord:
        path, error = self._resolve_test_path(test.get("file_path"))
        if error:
            return _file_record(test, "file_check", error=error)
        exists = path.exists() and path.is_file()
        result = _file_result(path, exists)
        return TestExecutionRecord(
            test_name=_test_name(test),
            executed=True,
            exit_code=0 if exists else 1,
            executed_at=_utc_now_iso(),
            validation_method="file_check",
            validation_result=result,
            error="" if exists else "文件不存在",
        )

    # LLM: _check_content reads only workspace-local text and searches for a literal pattern.
    # 函数用途: 检查文件内容是否包含指定文本；当前是字面量包含，不做正则或模型判断。
    def _check_content(self, test: dict[str, Any]) -> TestExecutionRecord:
        path, error = self._resolve_test_path(test.get("file_path"))
        pattern = str(test.get("content_pattern") or "")
        if error:
            return _file_record(test, "content_check", error=error)
        if not pattern:
            return _file_record(test, "content_check", error="内容检查缺少 content_pattern")
        if not path.exists() or not path.is_file():
            return _file_record(test, "content_check", error="文件不存在", path=path)
        return _content_match_record(test, path, pattern)

    # LLM: _resolve_test_path enforces that file validations cannot escape the executor workspace.
    # 函数用途: 把测试项里的相对路径解析为 workspace 内绝对路径；越界路径会返回错误。
    def _resolve_test_path(self, value: object) -> tuple[Path, str]:
        raw = str(value or "").strip()
        if not raw:
            return self.workspace_root, "缺少 file_path"
        path = (self.workspace_root / raw).resolve()
        try:
            path.relative_to(self.workspace_root)
        except ValueError:
            return path, "file_path 超出 workspace 边界"
        return path, ""


# LLM: _split_command centralizes platform shlex mode so validation and execution see the same argv.
# 函数用途: 解析命令字符串；Windows 使用非 POSIX 模式，避免反斜杠路径被当成转义。
def _split_command(command: str) -> list[str]:
    """Split a command string using the current platform's shell quoting rules."""

    argv = shlex.split(command, posix=os.name != "nt")
    if os.name == "nt":
        return [_strip_wrapping_quotes(item) for item in argv]
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


# LLM: _content_match_record reads one workspace-local text file and stores only match metadata.
# 函数用途: 构造内容检查记录；不会把完整文件正文写入 validation_result。
def _content_match_record(
    test: dict[str, Any],
    path: Path,
    pattern: str,
) -> TestExecutionRecord:
    """Return a content_check validation record."""

    text = path.read_text(encoding="utf-8", errors="replace")
    matched = pattern in text
    return TestExecutionRecord(
        test_name=_test_name(test),
        executed=True,
        exit_code=0 if matched else 1,
        executed_at=_utc_now_iso(),
        validation_method="content_check",
        validation_result={"ok": matched, "matched": matched, "path": str(path), "pattern": pattern},
        error="" if matched else "内容未匹配",
    )


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


# LLM: _utc_now_iso gives records portable UTC timestamps without pulling in project-wide clock helpers.
# 函数用途: 生成 JSON 友好的 UTC 时间；用于执行证据，不作为排序唯一事实源。
def _utc_now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
