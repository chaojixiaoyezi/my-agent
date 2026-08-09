
from __future__ import annotations

"""Bounded executor for real closeout validation items."""

import importlib
import subprocess
import time
from pathlib import Path
from typing import Any, ClassVar

from ..static_site import run_static_site_check
from .executor_helpers import (
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
from .records import TestExecutionRecord


class TestExecutor:
    """Execute one real validation item for closeout."""

    __test__: ClassVar[bool] = False
    ALLOWED_PREFIXES: ClassVar[frozenset[str]] = frozenset({
        "python",
        "python3",
        "py",
        "pytest",
        # 机器验收(问题3):子代理交付的验收命令常以 shell 内建 test 和 Go 工具链
        # 开头(test -f / go build / go test)。既有的 python 前缀本就允许任意代码
        # 执行,test/go 不改变风险面;缺失则 `test -f` 这类最朴素的验收命令整批
        # 被拒,机器绑定形同虚设。
        "test",
        "go",
    })
    BLOCKED_CHARS: ClassVar[frozenset[str]] = frozenset({"|", "&", ";", ">", "<", "`", "$"})
    DEFAULT_TIMEOUT_SECONDS: ClassVar[float] = 120.0
    MAX_TIMEOUT_SECONDS: ClassVar[float] = 300.0

    def __init__(
        self,
        workspace_root: str | Path,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        allowed_prefixes: set[str] | frozenset[str] | None = None,
        argv_prefix: tuple[str, ...] = (),
        env: dict[str, str] | None = None,
        boundary_root: str | Path | None = None,
        unrestricted_paths: bool = False,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        # 路径边界:默认收窄到 workspace_root(既有语义);机器验收(问题3)在
        # owner-scoped 沙箱里跑时,bwrap 的可写宇宙是整个 owner home(写根 None =
        # `--bind owner_home owner_home` rw),模型声明的绝对 working_dir 和注册表
        # 背书的产物路径都在 home 内、任务 workspace 外——边界必须放开到 owner home,
        # 否则这些合法路径被误拒。unrestricted_paths 只给无沙箱可信环境用
        # (owner_home 空 = 单租户,与 run_command 普通执行同可信级,本身无路径限制)。
        self.boundary_root = Path(boundary_root).resolve() if boundary_root else self.workspace_root
        self.unrestricted_paths = bool(unrestricted_paths)
        self.timeout_seconds = min(max(float(timeout_seconds), 0.1), self.MAX_TIMEOUT_SECONDS)
        prefixes = allowed_prefixes if allowed_prefixes is not None else self.ALLOWED_PREFIXES
        self.allowed_prefixes = frozenset(str(item).lower() for item in prefixes)
        # 机器验收沙箱(问题3):owner-scoped 任务的验收命令必须包 bwrap,argv_prefix
        # 就是 bwrap argv;env 只覆盖显式字段(HOME 等),不引入宿主环境其余变量。
        self.argv_prefix = tuple(argv_prefix)
        self.env = dict(env) if env else None

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
            return run_static_site_check(test, self.workspace_root)
        if method == "artifact_integrity":
            return self._check_artifact_integrity(test)
        return TestExecutionRecord(
            test_name=_test_name(test),
            validation_method=method,
            error=f"未知验证方式: {method}",
            validation_result={"ok": False, "reason": "unknown_validation_method"},
        )

    def _execute_command(self, test: dict[str, Any]) -> TestExecutionRecord:
        command = str(test.get("command") or "").strip()
        error = self._validate_command(command)
        if error:
            return _command_rejected_record(test, command, error)
        cwd, cwd_error = self._resolve_command_working_dir(test)
        if cwd_error:
            return _command_rejected_record(test, command, cwd_error)
        argv = _split_command(command)
        run_argv = [*self.argv_prefix, *_execution_argv(argv)]
        start = time.monotonic()
        try:
            completed = subprocess.run(
                run_argv,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
                env=self.env,
            )
        except subprocess.TimeoutExpired as exc:
            return _with_working_dir(
                _command_timeout_record(test, command, exc, (self.timeout_seconds, start)),
                cwd,
            )
        except Exception as exc:  # pragma: no cover - platform-specific subprocess failures.
            return _with_working_dir(_command_error_record(test, command, exc, start), cwd)
        return _with_working_dir(_command_completed_record(test, command, completed, start), cwd)

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

    def _check_file(self, test: dict[str, Any], *, method: str = "file_check") -> TestExecutionRecord:
        path, error = self._resolve_test_path(test.get("file_path"))
        if error:
            return _file_record(test, method, error=error)
        exists = path.exists() and path.is_file()
        result = _file_result(path, exists)
        return TestExecutionRecord(
            test_name=_test_name(test),
            executed=True,
            exit_code=0 if exists else 1,
            executed_at=_utc_now_iso(),
            validation_method=method,
            validation_result=result,
            error="" if exists else "文件不存在",
        )

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

    def _check_artifact_integrity(self, test: dict[str, Any]) -> TestExecutionRecord:
        path, error = self._resolve_test_path(test.get("file_path"))
        if error:
            return _file_record(test, "artifact_integrity", error=error, path=path)
        artifact_integrity = importlib.import_module("agent_py_agent.agent.tooling.artifact_integrity")
        decision = artifact_integrity.check_artifact_integrity(
            artifact_integrity.ArtifactIntegrityCheckRequest(path=path, require_complete=True)
        )
        result = _artifact_integrity_result(path, decision)
        return TestExecutionRecord(
            test_name=_test_name(test),
            executed=True,
            exit_code=0 if decision.ok else 1,
            executed_at=_utc_now_iso(),
            error="" if decision.ok else "产物完整性检查失败",
            validation_method="artifact_integrity",
            validation_result=result,
        )

    def _resolve_test_path(self, value: object) -> tuple[Path, str]:
        raw = str(value or "").strip()
        if not raw:
            return self.workspace_root, "缺少 file_path"
        path = (self.workspace_root / raw).resolve()
        if not self.unrestricted_paths:
            try:
                path.relative_to(self.boundary_root)
            except ValueError:
                return path, "file_path 超出可执行边界"
        return path, ""

    def _resolve_command_working_dir(self, test: dict[str, Any]) -> tuple[Path, str]:
        raw = str(test.get("working_dir") or test.get("cwd") or "").strip()
        if not raw:
            return self.workspace_root, ""
        candidate = Path(raw).expanduser()
        path = candidate.resolve() if candidate.is_absolute() else (self.workspace_root / candidate).resolve()
        if not self.unrestricted_paths:
            try:
                path.relative_to(self.boundary_root)
            except ValueError:
                return path, "working_dir 超出可执行边界"
        if not path.exists() or not path.is_dir():
            return path, "working_dir 不存在或不是目录"
        return path, ""


def _validation_method(test: dict[str, Any]) -> str:
    method = str(test.get("validation_method") or "command").strip().lower() or "command"
    if method in {"pytest", "unittest"} and str(test.get("command") or "").strip():
        return "command"
    return method


def _artifact_integrity_result(path: Path, decision: Any) -> dict[str, Any]:
    return {
        "ok": decision.ok,
        "path": str(path),
        "kind": decision.kind,
        "blocker_codes": decision.blocker_codes,
        "warning_codes": decision.warning_codes,
        "issues": [
            {
                "code": issue.code,
                "message": issue.message,
                "severity": issue.severity,
                "count": issue.count,
                "examples": list(issue.examples),
            }
            for issue in decision.issues
        ],
    }


def _content_pattern(test: dict[str, Any]) -> str:
    for key in ("content_equals", "expected_content", "content_pattern"):
        value = str(test.get(key) or "")
        if value:
            return value
    return ""


def _content_match_is_exact(test: dict[str, Any]) -> bool:
    mode = str(test.get("match_mode") or "").strip().lower()
    return mode in {"exact", "equals", "equal"} or any(key in test for key in ("content_equals", "expected_content"))


def _content_match_expects_absent(test: dict[str, Any]) -> bool:
    mode = str(test.get("match_mode") or "").strip().lower()
    if mode == "not_contains":
        return True
    if bool(test.get("expect_absent")):
        return True
    return False
