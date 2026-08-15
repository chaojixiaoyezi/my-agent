
from __future__ import annotations

"""Safe closeout validation executor: file/content/artifact checks only.

模型验收命令(command/cwd/working_dir)执行路径已删除(审计 R0):模型提交的
command 只保留为 inert evidence——记录原文供审计,任何读取都不触发 subprocess,
也永不参与 VERIFIED 判定。可机验方式仅剩 file_check / content_check /
static_site_check / artifact_integrity(全部为进程内文件检查,不执行外部程序)。
"""

import importlib
from pathlib import Path
from typing import Any, ClassVar

from ..static_site import run_static_site_check
from .executor_helpers import (
    ContentMatchRecordRequest,
    _content_match_record,
    _file_record,
    _file_result,
    _test_name,
    _utc_now_iso,
)
from .records import TestExecutionRecord

_COMMAND_DISABLED_REASON = "command_execution_disabled"


class TestExecutor:
    """Execute one real validation item for closeout (no command execution)."""

    __test__: ClassVar[bool] = False

    def __init__(
        self,
        workspace_root: str | Path,
        *,
        boundary_root: str | Path | None = None,
        unrestricted_paths: bool = False,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        # 路径边界:默认收窄到 workspace_root;注册表背书的绝对产物路径可能在
        # 任务 workspace 外(owner home 内),owner-scoped 时边界放开到 owner home。
        # unrestricted_paths 只给单租户/无 owner scope 环境用(本身无路径限制)。
        self.boundary_root = Path(boundary_root).resolve() if boundary_root else self.workspace_root
        self.unrestricted_paths = bool(unrestricted_paths)

    def execute(self, test: dict[str, Any]) -> TestExecutionRecord:
        """Execute a validation item and return its evidence record."""

        method = _validation_method(test)
        if method == "command":
            return self._inert_command(test)
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

    def _inert_command(self, test: dict[str, Any]) -> TestExecutionRecord:
        """Record a model-supplied command as inert evidence only; never execute.

        审计 R0:模型验收 command/cwd/working_dir 执行路径已删除。此处保留
        command 原文供审计,executed=False 且 ok=False——command 永不参与
        VERIFIED 判定,也绝不会触碰 subprocess。
        """
        command = str(test.get("command") or "").strip()
        working_dir = str(test.get("working_dir") or test.get("cwd") or "").strip()
        return TestExecutionRecord(
            test_name=_test_name(test, default=command),
            command=command,
            executed=False,
            executed_at=_utc_now_iso(),
            error="模型验收命令已禁用:command 只保留为 inert evidence,不会被机器执行",
            validation_method="command",
            validation_result={
                "ok": False,
                "reason": _COMMAND_DISABLED_REASON,
                "command": command,
                "working_dir": working_dir,
            },
        )

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
