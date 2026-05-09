# LLM: Parent acceptance auto-execution facade models; first slice is audit-only and never runs commands.
# 模块用途: 定义父级验收自动执行器的请求/结果包，为后续 dry-run facade 和审计文件打基础。
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from .models import SubAgentTask
from .parent_acceptance_auto_policy import (
    ParentAcceptanceAutoPolicy,
    build_parent_acceptance_auto_policy,
)


# LLM: ParentAcceptanceAutoExecutionRequest is the explicit bundle future executors must consume.
# 类用途: 描述一次父级验收自动执行计划请求；只保存 policy/preflight 摘要和推荐命令，不执行命令。
@dataclass(frozen=True)
class ParentAcceptanceAutoExecutionRequest:
    """Request bundle for a parent acceptance auto-execution plan."""

    __test__: ClassVar[bool] = False

    run_id: str
    mode: str = "dry_run"
    policy_ref: str = ""
    recommended_command: str = ""
    preflight_status: str = "blocked"
    ready_for_manual_execution: bool = False
    ready_for_automatic_execution: bool = False
    preflight_blockers: list[str] = field(default_factory=list)
    requested_by: str = "parent_acceptance_auto_policy"
    reserved: dict[str, Any] = field(default_factory=dict)

    # LLM: to_dict keeps request JSON stable for audit files and CLI rendering.
    # 函数用途: 把自动执行请求包转换为 JSON 友好字典；不展开任何引用文件正文。
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# LLM: ParentAcceptanceAutoExecutionResult is an audit result, not proof of execution.
# 类用途: 记录自动执行计划结果；第一版 executed 恒为 false，不改 task 状态。
@dataclass(frozen=True)
class ParentAcceptanceAutoExecutionResult:
    """Audit-only result for a parent acceptance auto-execution plan."""

    __test__: ClassVar[bool] = False

    run_id: str
    mode: str
    status: str
    request: ParentAcceptanceAutoExecutionRequest
    executed: bool = False
    mutates_task_state: bool = False
    command: str = ""
    execution_ref: str = ""
    blocked_by: list[str] = field(default_factory=list)
    reserved: dict[str, Any] = field(default_factory=dict)

    # LLM: to_dict keeps nested request output consistent with dataclass serialization.
    # 函数用途: 把自动执行结果转换为 JSON 友好字典；保留 request 子结构和阻断原因。
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# LLM: build_parent_acceptance_auto_execution plans the executor path while keeping execution closed.
# 函数用途: 生成父级验收自动执行 dry-run 计划并写审计文件；不启动进程、不改 task 状态。
def build_parent_acceptance_auto_execution(
    task: SubAgentTask,
    *,
    workspace_root: str | Path,
    mode: str = "dry_run",
) -> ParentAcceptanceAutoExecutionResult:
    policy = build_parent_acceptance_auto_policy(task, workspace_root=workspace_root)
    policy_ref = Path(task.reports_dir) / "parent_acceptance_auto_policy.json"
    request = _request_from_policy(task, policy, policy_ref=policy_ref, mode=mode)
    result = _result_from_request(request)
    write_parent_acceptance_auto_execution_file(task, result)
    return result


# LLM: write_parent_acceptance_auto_execution_file persists only the executor plan audit.
# 函数用途: 写入 `parent_acceptance_auto_execution.json`；只保存请求/结果和硬边界，不执行命令。
def write_parent_acceptance_auto_execution_file(
    task: SubAgentTask,
    result: ParentAcceptanceAutoExecutionResult,
    *,
    generated_at: float | None = None,
) -> Path:
    path = Path(task.reports_dir) / "parent_acceptance_auto_execution.json"
    payload = {
        "schema": "parent_acceptance_auto_execution.v1",
        "generated_at": generated_at if generated_at is not None else time.time(),
        "dry_run": True,
        "run_id": task.id,
        "request": result.request.to_dict(),
        "result": result.to_dict(),
        "reserved": {
            "refs_only": True,
            "executes_command": False,
            "mutates_task_state": False,
            "future_execute_supported": True,
        },
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return path


# LLM: _request_from_policy narrows policy output into the executor-facing request bundle.
# 函数用途: 从 auto-policy 生成执行器请求包，只复制摘要字段和 refs，不读取引用文件正文。
def _request_from_policy(
    task: SubAgentTask,
    policy: ParentAcceptanceAutoPolicy,
    *,
    policy_ref: Path,
    mode: str,
) -> ParentAcceptanceAutoExecutionRequest:
    return ParentAcceptanceAutoExecutionRequest(
        run_id=task.id,
        mode=mode,
        policy_ref=str(policy_ref),
        recommended_command=policy.recommended_command,
        preflight_status=policy.preflight_status,
        ready_for_manual_execution=policy.ready_for_manual_execution,
        ready_for_automatic_execution=policy.ready_for_automatic_execution,
        preflight_blockers=list(policy.preflight_blockers),
    )


# LLM: _result_from_request records the current closed executor decision.
# 函数用途: 根据请求包生成 dry-run 结果；第一版即使有推荐命令，也固定不执行。
def _result_from_request(
    request: ParentAcceptanceAutoExecutionRequest,
) -> ParentAcceptanceAutoExecutionResult:
    return ParentAcceptanceAutoExecutionResult(
        run_id=request.run_id,
        mode=request.mode,
        status="blocked",
        request=request,
        command=request.recommended_command,
        blocked_by=list(request.preflight_blockers),
    )
