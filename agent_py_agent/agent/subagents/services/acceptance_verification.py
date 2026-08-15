"""Machine verification for DONE acceptance tests (验收证据绑定机器执行).

子代理收口时可以口头声明 DONE/VERIFIED、failing_tests=[],但机器状态与证据
没有绑定:真实 build/test 日志可能仍然失败(问题3 实锤)。本模块在 runner 收口
路径上对 DONE 任务的 tests[] 逐条执行进程内安全验证:

- 全部可机验条目真实通过(文件存在 / 内容命中 / 产物完整性)→ VERIFIED 成立;
- 任何一条失败 → 在 test dict 上打真实失败事实(exit code + 错误信息),任务
  保持 UNVERIFIED,由现有 ISSUE_UNVERIFIED_DONE → BLOCKED 返工门要求重做;
- 模型自述的 ok/status 一律以机器验证结果为准(机器裁决覆盖模型自述);
- 无任何可机验条目时也保持 UNVERIFIED(不可绑定即不可信),返工门要求补齐。

审计 R0:模型验收 command/cwd/working_dir 执行路径已删除。command 只作为
inert evidence 记录(任何读取不触发 subprocess,永不参与 VERIFIED 判定);
可机验方式仅剩 file_check / content_check / static_site_check /
artifact_integrity——全部为进程内文件检查,不执行外部程序。

来源 worker(ledger)路径跳过:持久账本才是权威,模型收口不适用机器验收。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..execution.executor import TestExecutor
from ..models import TaskStatus, task_has_status

# 机器可验证的验收方式集合(全部为进程内文件检查,无外部程序执行)。其余
# (模型自述类,如"人工核对过"或 command)不可绑定机器,不参与裁决。
_CHECKABLE_METHODS = frozenset({
    "file_check",
    "content_check",
    "static_site_check",
    "artifact_integrity",
})

_STDERR_TAIL_CHARS = 400
_MESSAGE_MAX_CHARS = 600


@dataclass(frozen=True)
class AcceptanceVerificationResult:
    checked: bool
    passed: bool
    reason: str = ""
    failures: list[dict[str, str]] = field(default_factory=list)
    checked_count: int = 0


def verify_done_acceptance(
    task,
    tests: list[dict[str, object]],
    *,
    owner_home: str = "",
    executor: TestExecutor | None = None,
) -> AcceptanceVerificationResult:
    """机器验证一个 DONE 任务的验收声明,返回裁决结果(不修改 task 状态)。"""
    if not task_has_status(task, TaskStatus.DONE):
        return AcceptanceVerificationResult(checked=False, passed=False, reason="NOT_DONE")
    if _ledger_authoritative(task):
        # 来源 worker 的 DONE 由持久账本证明(source_worker_lifecycle_state==complete),
        # 没有模型声明的 tests 可验;保持账本权威,不做机器裁决。
        return AcceptanceVerificationResult(checked=False, passed=False, reason="LEDGER_AUTHORITATIVE")

    checkable = [test for test in (tests or []) if _is_checkable(test)]
    # C3/G4 兜底收尾(据 artifact_registry 已登记产物合成 DONE)没有 tests[],
    # 只有 registered_artifact 证据;合成 file_check 让"产物真的在盘上"成为
    # 机器可验证事实——注册表撒谎即验失败,不再无条件放行。产物路径由注册表
    # 背书(绝对路径,可能落在任务工作区之外);owner-scoped 沙箱的可写宇宙是
    # 整个 owner home,边界随之覆盖,同一 runner 即可验证,无需 per-artifact
    # 工作区。
    synthesized = _synthesized_file_checks(task)
    if not checkable and not synthesized:
        return AcceptanceVerificationResult(
            checked=False,
            passed=False,
            reason="NO_CHECKABLE_TESTS",
        )

    workspace = _workspace_for(task)
    if workspace is None:
        return AcceptanceVerificationResult(
            checked=False,
            passed=False,
            reason="NO_WORKSPACE",
        )

    failures: list[dict[str, str]] = []
    for test in [*checkable, *synthesized]:
        runner = _runner_for(workspace, owner_home, executor)
        _run_one(runner, test, failures)
    return AcceptanceVerificationResult(
        checked=True,
        passed=not failures,
        failures=failures,
        checked_count=len(checkable) + len(synthesized),
    )


def _is_checkable(test: dict[str, object]) -> bool:
    if not isinstance(test, dict):
        return False
    method = str(test.get("validation_method") or "command").strip().lower() or "command"
    # command 不作为可机验方式:模型提交的 command 只保留为 inert evidence,
    # 即使带了 command 字段也不触发任何机器执行。
    return method in _CHECKABLE_METHODS


def _synthesized_file_checks(task) -> list[dict[str, object]]:
    """从 task.evidence(kind=registered_artifact + path)合成 file_check 测试。"""
    checks: list[dict[str, object]] = []
    for item in getattr(task, "evidence", []) or []:
        if isinstance(item, dict):
            kind = str(item.get("kind") or "")
            path = str(item.get("path") or "")
        else:
            kind = str(getattr(item, "kind", "") or "")
            path = str(getattr(item, "path", "") or "")
        if kind != "registered_artifact" or not path:
            continue
        checks.append(
            {
                "name": f"registered artifact exists: {path}",
                "validation_method": "file_check",
                "file_path": path,
            }
        )
    return checks


def _workspace_for(task) -> Path | None:
    # 优先 runner 的真实命令 cwd(agent_run_workspace_dir == task_dir):
    # 模型任务期的 write_file 相对路径与 `go test ./...` 都在这里解析,验收必须
    # 复现同一体验,否则模型写的 test -f proof.txt 因 cwd 漂到 output/ 假失败。
    for attr in ("agent_run_workspace_dir", "task_dir", "output_dir", "tests_dir"):
        candidate = getattr(task, attr, "") or ""
        if not candidate:
            continue
        path = Path(candidate)
        if path.is_dir():
            return path
    return None


def _runner_for(
    workspace: Path,
    owner_home: str,
    executor: TestExecutor | None = None,
) -> TestExecutor:
    """构建验收执行器(仅进程内文件检查,无外部程序执行)。"""
    if executor is not None:
        return executor
    if owner_home:
        # 注册表背书的绝对产物路径与父任务 output/ 可能在任务 workspace 外、
        # owner home 内(file_check 需要可验),路径边界放开到 owner home。
        boundary_root = Path(owner_home)
        unrestricted = False
    else:
        # 无 owner scope = 与 run_command 普通执行同可信级(单租户/开发机,本身
        # 无路径限制):绝对路径照实解析,不做边界裁剪。
        boundary_root = None
        unrestricted = True
    return TestExecutor(
        workspace,
        boundary_root=boundary_root,
        unrestricted_paths=unrestricted,
    )


def _run_one(runner: TestExecutor, test: dict[str, object], failures: list[dict[str, str]]) -> None:
    record = runner.execute(test)
    ok = bool(record.passed)
    _stamp_machine_fact(test, record, ok)
    if not ok:
        failures.append(_failure_fact(test, record))


def _ledger_authoritative(task) -> bool:
    attrs = getattr(task, "attributes", {}) or {}
    if not isinstance(attrs, dict):
        return False
    from ...common.audit_activation import structured_audit_source_worker_attributes

    return structured_audit_source_worker_attributes(attrs)


def _stamp_machine_fact(test: dict[str, object], record, ok: bool) -> None:
    """把机器事实打到 test dict 上(模型自述一律以机器结果为准)。

    该 dict 就是 output_payload["tests"] 里的对象(直接引用),失败事实随
    build_failing_tests_payload 进 decision ledger,返工重开后模型可见。
    """
    test["ok"] = ok
    test["status"] = "passed" if ok else "failed"
    test["verified_by"] = "machine_execution"
    if ok:
        return
    message = str(record.error or "").strip()
    exit_code = getattr(record, "exit_code", None)
    if exit_code is not None:
        message = f"exit code {exit_code}: {message}".strip(": ") if message else f"exit code {exit_code}"
    stderr = str(getattr(record, "stderr", "") or "").strip()
    if stderr:
        tail = stderr[-_STDERR_TAIL_CHARS:]
        suffix = "…" if len(stderr) > _STDERR_TAIL_CHARS else ""
        message = f"{message}\nstderr{suffix}: {tail}" if message else f"stderr{suffix}: {tail}"
    test["message"] = message[:_MESSAGE_MAX_CHARS]


def _failure_fact(test: dict[str, object], record) -> dict[str, str]:
    return {
        "name": str(test.get("name") or "unnamed"),
        "command": str(test.get("command") or test.get("validation_method") or ""),
        "message": str(test.get("message") or record.error or "验收测试未通过"),
    }


__all__ = [
    "AcceptanceVerificationResult",
    "verify_done_acceptance",
]
