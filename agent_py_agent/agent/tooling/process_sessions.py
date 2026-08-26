from __future__ import annotations

"""Model-facing control surface for managed background shell sessions."""

# LLM: 本模块只投影 process_registry 中当前可信用户会话可见的记录；session_id
# 只是定位键，不是授权凭证，授权范围必须由 executor 通过 __run_scope 注入。
# 模块用途: 让模型不用 shell sleep/ps/kill，也能查询、等待和停止自己启动的后台命令。

import json
from typing import Any

from .models import (
    BaseTool,
    EffectResolverPolicy,
    IdempotencyPolicy,
    OutputPolicy,
    ResourceScopePolicy,
    SandboxPolicy,
    TimeoutPolicy,
    ToolHandlerOutcome,
    ToolInputPolicy,
    ToolModelHints,
    ToolModelSpec,
    ToolRuntimePolicy,
)
from .process_registry import process_access_scope, process_registry
from .process_session_store import process_session_store_root

_DEFAULT_WAIT_SECONDS = 5.0
_MAX_WAIT_SECONDS = 30.0


# LLM: process_session 是 run_command(run_in_background=true) 的唯一续接入口；
# list/status/wait 只读，stop 通过结构化 action 进入危险操作门。新增动作时必须同步
# effect mapping、schema、scope 测试和 run_command 返回提示。
# 类用途: 用一个统一工具列出、查看、短暂等待或停止当前 TUI 会话的后台命令。
class ProcessSessionTool(BaseTool):
    model_spec = ToolModelSpec(
        name="process_session",
        description=(
            "管理 run_command(run_in_background=true) 启动的后台命令，"
            "支持 list/status/wait/stop；等待时不要另跑 sleep。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "status", "wait", "stop"],
                    "description": "后台进程动作。",
                },
                "session_id": {
                    "type": "string",
                    "description": "status/wait/stop 所需的后台 session id。",
                },
                "timeout_seconds": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": _MAX_WAIT_SECONDS,
                    "description": "wait 最多等待秒数，默认 5，最大 30。",
                },
            },
            "required": ["action"],
            "additionalProperties": False,
        },
        hints=ToolModelHints(
            category="shell",
            use_cases=(
                "后台构建、下载或服务启动后查看状态和日志尾部",
                "需要有界等待后台命令完成时",
                "需要停止当前会话启动的后台命令时",
            ),
            avoid_when=(
                "一次性命令直接使用 run_command",
                "交互式 stdin/TTY 使用 terminal_session",
                "不要用 run_command 执行 sleep 来轮询后台进程",
            ),
            keywords=(
                "background process",
                "process status",
                "wait process",
                "stop process",
                "后台进程",
                "等待构建",
            ),
            examples=(
                '{"tool":"process_session","action":"status","session_id":"bg-1-..."}',
                '{"tool":"process_session","action":"wait","session_id":"bg-1-...","timeout_seconds":10}',
                '{"tool":"process_session","action":"stop","session_id":"bg-1-..."}',
            ),
        ),
    )
    runtime_policy = ToolRuntimePolicy(
        effect_resolver=EffectResolverPolicy(
            "dangerous",
            by_parameter=((
                "action",
                (
                    ("list", "read_only"),
                    ("status", "read_only"),
                    ("wait", "read_only"),
                    ("stop", "dangerous"),
                ),
            ),),
        ),
        sandbox_policy=SandboxPolicy("none"),
        idempotency_policy=IdempotencyPolicy("operation"),
        timeout_policy=TimeoutPolicy(int(_MAX_WAIT_SECONDS + 5)),
        resource_scopes=ResourceScopePolicy(
            parameter_names=("session_id",),
            parameter_kinds={"session_id": "logical"},
        ),
        output_policy=OutputPolicy(trust="external_data"),
        input_policy=ToolInputPolicy(internal_parameters=("__run_scope",)),
        promotes_task=False,
        mutates_workspace=False,
    )

    # LLM: owner_scope_root and workspace_root are fixed by ToolRegistry, never by
    # model arguments. Together they locate the protected cross-process store.
    # 函数用途: 创建绑定当前用户根和工作区的后台进程会话工具。
    def __init__(
        self,
        owner_scope_root: object = "",
        workspace_root: object = ".",
    ) -> None:
        self.owner_scope_root = str(owner_scope_root or "")
        self.workspace_root = str(workspace_root or ".")

    # LLM: action 只分派到 registry 的 scope-aware 方法；未知、越界与不存在记录均
    # 返回稳定结构化错误，不泄露其他会话是否存在同名 session。
    # 函数用途: 执行后台进程的列出、查询、等待或停止动作。
    def execute(self, params: dict[str, Any]) -> ToolHandlerOutcome:
        scope = process_access_scope(params.get("__run_scope"), self.owner_scope_root)
        if not scope.is_bound():
            return self._error(
                "OWNER_SCOPE_UNAVAILABLE",
                "当前运行缺少可信用户会话范围，不能访问后台进程。",
            )
        store_root = process_session_store_root(
            self.workspace_root,
            self.owner_scope_root,
        )
        action = str(params.get("action") or "").strip().lower()
        if action == "list":
            processes, load_errors = process_registry.list_report(scope, store_root)
            payload: dict[str, Any] = {"processes": processes}
            if load_errors:
                payload["load_errors"] = load_errors
            return self._ok(payload)
        if action not in {"status", "wait", "stop"}:
            return self._error(
                "TOOL_INVALID_ARGUMENTS",
                "action 必须是 list/status/wait/stop",
            )
        session_id = str(params.get("session_id") or "").strip()
        if not session_id:
            return self._error(
                "TOOL_INVALID_ARGUMENTS",
                f"{action} 需要 session_id",
            )
        if action == "status":
            result = process_registry.status(session_id, scope, store_root)
        elif action == "stop":
            result = process_registry.kill(session_id, scope, store_root)
        else:
            timeout = self._wait_timeout(params.get("timeout_seconds"))
            result = process_registry.wait(session_id, timeout, scope, store_root)
        if result is None:
            return self._error(
                "PROCESS_NOT_FOUND",
                f"当前用户会话中不存在后台进程: {session_id}",
            )
        return self._ok(result)

    # LLM: wait 上限必须小于工具运行时 timeout，避免模型一次调用长期占住主链；
    # 非法值回到稳定默认值而不是传给 time/Popen。
    # 函数用途: 清洗并限制一次后台等待的秒数。
    @staticmethod
    def _wait_timeout(value: object) -> float:
        try:
            seconds = float(value) if value is not None else _DEFAULT_WAIT_SECONDS
        except (TypeError, ValueError):
            seconds = _DEFAULT_WAIT_SECONDS
        return max(0.0, min(_MAX_WAIT_SECONDS, seconds))

    # LLM: 成功结果始终是 JSON 对象，供 provider、TUI 和归档复用同一结构。
    # 函数用途: 把进程结果编码成统一成功工具返回。
    def _ok(self, payload: dict[str, Any]) -> ToolHandlerOutcome:
        return ToolHandlerOutcome(
            self.model_spec.name,
            True,
            json.dumps(payload, ensure_ascii=False),
        )

    # LLM: 错误码是机器判断依据，中文 message 只供模型和用户理解。
    # 函数用途: 构造统一的后台进程工具失败返回。
    def _error(self, code: str, message: str) -> ToolHandlerOutcome:
        return ToolHandlerOutcome(
            self.model_spec.name,
            False,
            message,
            error_code=code,
        )


__all__ = ["ProcessSessionTool"]
