from __future__ import annotations

"""Model-facing control surface for managed background shell sessions."""

# LLM: 本模块只投影 process_registry 中当前可信用户会话可见的记录；session_id
# 只是定位键，不是授权凭证；停止的未确认回执不能投影成成功，权限由 __run_scope 注入。
# 进展指纹只用于软观察，不参与权限、进程终态或任务完成裁决。
# 模块用途: 查询、等待和停止自己的后台命令，向模型明确报告停止已确认还是执行结果未知。

import hashlib
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
from .process_network_status import managed_process_network_status
from .process_registry import process_access_scope, process_registry
from .process_session_store import process_session_store_root

_DEFAULT_WAIT_SECONDS = 30.0
_MAX_WAIT_SECONDS = 600.0


# LLM: process_session 是 run_command(run_in_background=true) 的唯一续接入口；
# list/status/wait/network_status 只读，stop 是对已登记且通过 owner/session scope 校验的
# 既有执行会话做状态变更，不是新起一条未隔离命令。新增动作时必须同步 effect mapping、
# schema、scope 测试和 run_command 返回提示。
# 类用途: 用一个统一工具列出、查看、核对网络、可取消地长等或停止当前 TUI 会话的后台命令。
class ProcessSessionTool(BaseTool):
    model_spec = ToolModelSpec(
        name="process_session",
        description=(
            "管理 run_command(run_in_background=true) 启动的后台命令，"
            "支持 list/status/wait/network_status/stop。session_id 是唯一稳定的管理句柄；"
            "network_status 是宿主机对受管进程树的权威只读观测，run_command 沙箱可能看不到其中的监听 PID；"
            "服务对外可达必须区分真实监听进程、主机防火墙与外部验证。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "status", "wait", "network_status", "stop"],
                    "description": "后台进程动作。",
                },
                "session_id": {
                    "type": "string",
                    "description": (
                        "status/wait/network_status/stop 所需的后台 session id；"
                        "不要猜测或传操作系统 PID。"
                    ),
                },
                "timeout_seconds": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": _MAX_WAIT_SECONDS,
                    "description": "wait 最多等待秒数，默认 30，最大 600；等待可由宿主取消，届满不代表进程失败。",
                },
                "port": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 65535,
                    "description": "network_status 可选端口；填写后只核对这个端口是否由该受管进程监听。",
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
                "需要确认服务监听范围、主机防火墙和局域网验证边界时",
            ),
            avoid_when=(
                "一次性命令直接使用 run_command",
                "交互式 stdin/TTY 使用 terminal_session",
                "不要用 run_command 执行 sleep 来轮询后台进程",
                "不要用沙箱内 ps/lsof 重复否定 network_status 已观测到的 listener_pids",
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
                '{"tool":"process_session","action":"network_status","session_id":"bg-1-...","port":3000}',
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
                    ("network_status", "read_only"),
                    ("stop", "mutating"),
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
    # 返回稳定错误，不泄露其他会话是否存在；stop 沿用 registry 终止回执，status/wait 附宿主进展观测。
    # 函数用途: 执行后台进程控制；未确认的停止结果明确 UNKNOWN，保留结果供核对。
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
        if action not in {"status", "wait", "network_status", "stop"}:
            return self._error(
                "TOOL_INVALID_ARGUMENTS",
                "action 必须是 list/status/wait/network_status/stop",
            )
        session_id = str(params.get("session_id") or "").strip()
        if not session_id:
            return self._error(
                "TOOL_INVALID_ARGUMENTS",
                f"{action} 需要 session_id",
            )
        if action == "status":
            result = process_registry.status(session_id, scope, store_root)
        elif action == "network_status":
            result = self._network_status(
                session_id,
                params.get("port"),
                scope,
                store_root,
            )
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
        termination = result.get("termination")
        if action == "stop" and isinstance(termination, dict) and termination.get("confirmed") is not True:
            return ToolHandlerOutcome(
                self.model_spec.name, False, json.dumps(result, ensure_ascii=False),
                result_envelope={"process": result},
                error_code="TOOL_OPERATION_OUTCOME_UNKNOWN", effect_outcome="unknown",
            )
        return self._ok(result, observe_progress=action in {"status", "wait"})

    # LLM: Session scope is checked by the registry before process/PID facts reach the network
    # observer. The observer is read-only and always leaves LAN reachability externally unverified.
    # 函数用途: 查询当前受管服务的监听与防火墙事实，不修改防火墙或伪造跨机器成功。
    @staticmethod
    def _network_status(
        session_id: str,
        port_value: object,
        scope: object,
        store_root: object,
    ) -> dict[str, object] | None:
        summary = process_registry.status(session_id, scope, store_root)
        if summary is None:
            return None
        record = process_registry.get(session_id, scope, store_root)
        if record is None:
            return None
        try:
            port = int(port_value or 0)
        except (TypeError, ValueError):
            port = 0
        return {
            "process": summary,
            "network": managed_process_network_status(record, requested_port=port),
        }

    # LLM: wait 上限必须小于工具运行时 timeout；长等待由取消令牌释放，不用短轮询消耗模型；
    # 非法值回到稳定默认值而不是传给 time/Popen。
    # 函数用途: 清洗并限制一次后台等待的秒数。
    @staticmethod
    def _wait_timeout(value: object) -> float:
        try:
            seconds = float(value) if value is not None else _DEFAULT_WAIT_SECONDS
        except (TypeError, ValueError):
            seconds = _DEFAULT_WAIT_SECONDS
        return max(0.0, min(_MAX_WAIT_SECONDS, seconds))

    # LLM: 软观察指纹由宿主结果生成，只排除时长和本次等待届满；原始正文、权限和终态均不改变。
    # 函数用途: 返回完整进程结果，并为状态/等待附上稳定进展摘要，避免时钟跳动被当成工作进展。
    def _ok(self, payload: dict[str, Any], *, observe_progress: bool = False) -> ToolHandlerOutcome:
        envelope = {}
        if observe_progress:
            stable = {key: value for key, value in payload.items() if key not in {"uptime_seconds", "wait_timed_out"}}
            digest = hashlib.sha256(json.dumps(stable, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            envelope["progress_observation"] = {"sha256": digest, "pending": payload.get("status") == "running"}
        return ToolHandlerOutcome(
            self.model_spec.name,
            True,
            json.dumps(payload, ensure_ascii=False),
            result_envelope=envelope,
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
