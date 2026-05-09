# LLM: Parent acceptance auto-execution facade models; first slice is audit-only and never runs commands.
# 模块用途: 定义父级验收自动执行器的请求/结果包，为后续 dry-run facade 和审计文件打基础。
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, ClassVar


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
