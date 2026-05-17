# LLM: Minimal real acceptance validation executor; returns records and leaves acceptance decisions to callers.
# 模块用途: 执行父级验收需要的 command/file/content 检查，产出执行记录，但不写任务状态。

from __future__ import annotations

"""Bounded executor for real parent-acceptance validation items."""

import subprocess
import time
from pathlib import Path
from typing import Any, ClassVar

from .execution_executor_helpers import (
    ContentMatchRecordRequest,
    _command_completed_record,
    _command_error_record,
    _command_name,
    _command_rejected_record,
    _command_timeout_record,
    _content_match_record,
    _execution_argv,
    _file_record,
    _file_result,
    _split_command,
    _test_name,
    _utc_now_iso,
    _with_working_dir,
)
from .execution_records import TestExecutionRecord
from .static_site_validator import run_static_site_check


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

        method = _validation_method(test)
        if method == "command":
            return self._execute_command(test)
        if method == "file_check":
            return self._check_file(test)
        if method == "content_check":
            return self._check_content(test)
        if method == "static_site_check":
            # LLM: Static-site validation is read-only and workspace-bound like file/content checks.
            return run_static_site_check(test, self.workspace_root)
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
        cwd, cwd_error = self._resolve_command_working_dir(test)
        if cwd_error:
            return _command_rejected_record(test, command, cwd_error)
        argv = _split_command(command)
        run_argv = _execution_argv(argv)
        start = time.monotonic()
        try:
            completed = subprocess.run(
                run_argv,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            return _with_working_dir(
                _command_timeout_record(test, command, exc, (self.timeout_seconds, start)),
                cwd,
            )
        except Exception as exc:  # pragma: no cover - platform-specific subprocess failures.
            return _with_working_dir(_command_error_record(test, command, exc, start), cwd)
        return _with_working_dir(_command_completed_record(test, command, completed, start), cwd)

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

    # LLM: _check_content reads only workspace-local text and compares literal patterns.
    # 函数用途: 检查文件内容是否包含或精确等于指定文本；不做正则或模型判断。
    def _check_content(self, test: dict[str, Any]) -> TestExecutionRecord:
        path, error = self._resolve_test_path(test.get("file_path"))
        pattern = _content_pattern(test)
        exact = _content_match_is_exact(test)
        expect_absent = _content_match_expects_absent(test)
        if error:
            return _file_record(test, "content_check", error=error)
        if not pattern:
            return _file_record(test, "content_check", error="内容检查缺少 content_pattern")
        if not path.exists() or not path.is_file():
            return _file_record(test, "content_check", error="文件不存在", path=path)
        return _content_match_record(
            ContentMatchRecordRequest(
                test=test,
                path=path,
                pattern=pattern,
                exact=exact,
                expect_absent=expect_absent,
            )
        )

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

    # LLM: _resolve_command_working_dir lets parent tests run inside artifact dirs while preserving workspace bounds.
    # 函数用途: 解析 command 测试的 working_dir/cwd；未指定时使用 workspace 根目录，越界或非目录会阻断执行。
    def _resolve_command_working_dir(self, test: dict[str, Any]) -> tuple[Path, str]:
        raw = str(test.get("working_dir") or test.get("cwd") or "").strip()
        if not raw:
            return self.workspace_root, ""
        candidate = Path(raw).expanduser()
        path = candidate.resolve() if candidate.is_absolute() else (self.workspace_root / candidate).resolve()
        try:
            path.relative_to(self.workspace_root)
        except ValueError:
            return path, "working_dir 超出 workspace 边界"
        if not path.exists() or not path.is_dir():
            return path, "working_dir 不存在或不是目录"
        return path, ""


# LLM: _validation_method treats common test-runner aliases as bounded commands.
# 函数用途: runner 把 pytest/unittest 写进 validation_method 时，只要提供 command，就仍走 command allowlist。
def _validation_method(test: dict[str, Any]) -> str:
    method = str(test.get("validation_method") or "command").strip().lower() or "command"
    if method in {"pytest", "unittest"} and str(test.get("command") or "").strip():
        return "command"
    return method


# LLM: _content_pattern accepts newer exact-content field names while preserving legacy content_pattern.
# 函数用途: 从 content_check 测试项里提取要匹配的字面文本；支持后续 schema 扩展字段。
def _content_pattern(test: dict[str, Any]) -> str:
    for key in ("content_equals", "expected_content", "content_pattern"):
        value = str(test.get(key) or "")
        if value:
            return value
    return ""


# LLM: _content_match_is_exact keeps exact file assertions explicit and backward compatible.
# 函数用途: 判断 content_check 是否要做全文相等；旧的 content_pattern 默认仍是包含匹配。
def _content_match_is_exact(test: dict[str, Any]) -> bool:
    mode = str(test.get("match_mode") or "").strip().lower()
    return mode in {"exact", "equals", "equal"} or any(key in test for key in ("content_equals", "expected_content"))


# LLM: _content_match_expects_absent reads explicit negative-content schema only.
# 函数用途: 只有 match_mode=not_contains 或 expect_absent=true 这类机器字段才表示反向内容检查。
def _content_match_expects_absent(test: dict[str, Any]) -> bool:
    mode = str(test.get("match_mode") or "").strip().lower()
    if mode in {
        "not_contains",
        "not_exists",
        "not_exist",
        "absent",
        "missing",
        "not_present",
        "does_not_contain",
    }:
        return True
    if bool(test.get("expect_absent") or test.get("negate") or test.get("should_not_contain")):
        return True
    return False
