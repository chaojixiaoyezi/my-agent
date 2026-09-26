# LLM: 只注册给本机管理员主代理（owner_type=main_agent）且 enable_gateway_restart_tool 开启；子代理运行里不可用。
# 工具只写一份重启请求就返回，真正的排空与换进程由 Gateway 服务主循环执行（gateway_parts/restart_service）。
# 发起方身份只取结构化事实：owner 三元组、会话 thread_id、会话存储根；不接受模型传入的进程号或路径。
# effect=dangerous：完全放行模式直接执行，其它审批模式走统一危险动作审批。改动须同步 test_gateway_restart_tool.py。
# 模块用途: 让管理员的 my-agent 在排查中发现需要重启时，自己安排一次不会切断回合的 Gateway 安全重启。
from __future__ import annotations

import json
from typing import Any

from ..conversation.authority import current_conversation_task_attributes
from ..gateway_parts.paths import gateway_paths
from ..gateway_parts.restart_service import hosting_gateway_pid, submit_restart_request
from ..runtime_context import current_subagent_run_id
from .models import (
    BaseTool,
    EffectResolverPolicy,
    IdempotencyPolicy,
    ResourceScopePolicy,
    ToolAvailability,
    ToolHandlerOutcome,
    ToolModelHints,
    ToolModelSpec,
    ToolRuntimePolicy,
)

TOOL_NAME = "restart_gateway"
_REFUSAL_CODES = {"cooldown": "GATEWAY_RESTART_COOLDOWN", "loop_guard": "GATEWAY_RESTART_LOOP_GUARD"}
_HINT = (
    "已安排安全重启。本轮结束后 Gateway 会先排空再换新进程，TUI 和 IM 会自动重连，停在半路的回合会自动续跑。"
    "请现在用一句话告诉用户正在重启并结束本轮；重启完成后你会收到观测通知，届时不要再次调用 restart_gateway。"
)


# LLM: 工具说明面向模型：强调“只写请求、立刻返回”“不要用 shell 自停”“完成后不要再次重启”，避免重启循环。
# 类用途: 管理员主代理安排 Gateway 安全重启的唯一入口。
class RestartGatewayTool(BaseTool):
    model_spec = ToolModelSpec(
        name=TOOL_NAME,
        description=(
            "安排当前 Gateway 安全重启：Gateway 先停领新请求、等在跑的回合结束，再让新的副作用工具停在开跑前、"
            "等执行中的工具跑完，然后换一个新进程接班；停在半路的回合会自动续跑，TUI 和 IM 自动重连。"
            "只在确实需要时调用，例如用户要求重启、改了只在启动时读取的配置、部署了新版本。"
            "用 /model 或 manage_models 改的模型配置下一次请求直接生效，不需要重启。"
            "调用后立刻返回 scheduled；不要再用 shell 执行 my-agent gateway restart/stop 或 kill 进程。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "为什么需要重启，一句话；会写进 Gateway 日志并展示给用户。",
                },
            },
            "required": ["reason"],
            "additionalProperties": False,
        },
        hints=ToolModelHints(
            category="system",
            use_cases=(
                "用户明确要求重启 Gateway",
                "排查中确认必须重启才能生效（启动时读取的配置、新部署的版本）",
            ),
            avoid_when=(
                "只改了模型配置：下一次请求直接生效，不需要重启",
                "刚收到“重启已完成”的观测：不要再次重启或为了验证而重启",
                "不要用 shell 的 my-agent gateway restart/stop 或 kill 代替本工具",
            ),
            keywords=("restart gateway", "gateway restart", "重启 gateway", "重启网关", "安全重启"),
            examples=('{"tool":"restart_gateway","reason":"用户要求重启以加载新的通道配置"}',),
        ),
    )
    runtime_policy = ToolRuntimePolicy(
        effect_resolver=EffectResolverPolicy("dangerous"),
        idempotency_policy=IdempotencyPolicy("operation"),
        resource_scopes=ResourceScopePolicy(mode="declared", static_scopes=("gateway:current",)),
        promotes_task=False,
        mutates_workspace=False,
    )

    # LLM: agent 是路径与身份的唯一来源；测试可传最小兼容对象，生产不接受模型给出的路径。
    # 函数用途: 绑定当前主代理，用它定位 Gateway 目录和发起会话。
    def __init__(self, agent: object) -> None:
        self.agent = agent

    # LLM: 子代理不能替主会话安排重启；判定只读结构化运行上下文。
    # 函数用途: 子代理运行里隐藏本工具。
    def availability(self) -> ToolAvailability:
        if current_subagent_run_id(self.agent):
            return ToolAvailability.unavailable("gateway restart is scheduled only by the main conversation agent")
        return ToolAvailability.ready()

    # LLM: 副作用：写 Gateway 根目录下的 gateway_restart.request 与冷却记录；不启动/停止任何进程。
    #   拒绝（不在 Gateway 内、冷却中、防循环）都在写入前返回并标记 not_started。
    # 函数用途: 校验托管身份与发起会话，提交一份安全重启请求并返回 scheduled/coalesced 回执。
    def execute(self, params: dict[str, Any]) -> ToolHandlerOutcome:
        target_pid = hosting_gateway_pid()
        if target_pid <= 0:
            return _refuse("当前会话不在 Gateway 进程内运行，无法安排安全重启。", "GATEWAY_RESTART_NOT_HOSTED")
        reason = str(params.get("reason") or "").strip()
        if not reason:
            return _refuse("reason 必填：用一句话说明为什么需要重启。", "TOOL_INVALID_ARGUMENTS")
        config = getattr(self.agent, "config", None)
        result = submit_restart_request(
            gateway_paths(self.agent),
            target_pid=target_pid,
            requester=_requester(self.agent),
            reason=reason,
            cooldown_seconds=float(getattr(config, "gateway_restart_cooldown_seconds", 30) or 0),
        )
        status = str(result.get("status") or "")
        if status in _REFUSAL_CODES:
            body = {key: value for key, value in result.items() if key != "request"}
            return _refuse(json.dumps(body, ensure_ascii=False), _REFUSAL_CODES[status], raw=True)
        request = result.get("request") or {}
        body = {
            "status": status,
            "request_id": request.get("request_id"),
            "target_pid": target_pid,
            "turn_wait_seconds": getattr(config, "gateway_restart_turn_wait_seconds", 300),
            "hint": _HINT,
        }
        return ToolHandlerOutcome(TOOL_NAME, True, json.dumps(body, ensure_ascii=False))


def _requester(agent: object) -> dict[str, object]:
    home = getattr(agent, "home_paths", None)
    store = getattr(agent, "conversation_store", None)
    storage_root = getattr(getattr(store, "storage", None), "root", "")
    return {
        "kind": "agent_tool",
        "owner_provider": str(getattr(home, "owner_provider", "") or ""),
        "owner_kind": str(getattr(home, "owner_kind", "") or ""),
        "owner_id": str(getattr(home, "owner_id", "") or ""),
        "thread_id": str(current_conversation_task_attributes(agent).get("conversation_thread_id") or ""),
        "conversation_store_root": str(storage_root or ""),
    }


def _refuse(message: str, code: str, *, raw: bool = False) -> ToolHandlerOutcome:
    return ToolHandlerOutcome(
        TOOL_NAME,
        False,
        message if raw else json.dumps({"error": message}, ensure_ascii=False),
        error_code=code,
        effect_outcome="not_started",
        failure_stage="validation",
    )


__all__ = ["RestartGatewayTool", "TOOL_NAME"]
