from __future__ import annotations

"""Expose the current local Gateway's canonical runtime facts to the main agent."""

# LLM: 当前模型只读调用者冻结的 config/backend，不能读 Gateway 启动默认值或后来切换的 owner 选择。This surface reuses Gateway state,
# heartbeat, validated PID records and lifecycle-bounded diagnostics; never fall back to ps/ss,
# guessed ports, raw logs, request bodies, or credentials.
# 模块用途: 让本机管理员主代理直接读取唯一 Gateway 的权威健康事实，避免靠 shell 猜进程和端口。

import json
from typing import Any

from ..gateway_parts.paths import gateway_paths
from ..gateway_parts.status_rendering import gateway_runtime_snapshot
from .models import (
    BaseTool,
    ConcurrencyPolicy,
    EffectResolverPolicy,
    OutputPolicy,
    ResourceScopePolicy,
    SandboxPolicy,
    ToolHandlerOutcome,
    ToolModelHints,
    ToolModelSpec,
    ToolRuntimePolicy,
)


# LLM: Register this handler only for a main_agent ToolRegistry. Its output deliberately includes
# admin diagnostics and host paths, so owner-scoped user/group agents must never receive it.
# 类用途: 为本机管理员返回当前唯一 Gateway 的进程、端点、队列、配置和本次日志摘要。
class GatewayStatusTool(BaseTool):
    model_spec = ToolModelSpec(
        name="gateway_status",
        description=(
            "读取当前 my-agent 唯一 Gateway 的权威运行状态。检查 Gateway 健康、真实监听端口、"
            "本次调用模型、配置来源、队列或日志噪声时必须优先使用本工具；不要用 ps/ss 猜端口，"
            "也不要把 /health 当作状态端点，HTTP 状态端点由结果中的 status_path 给出。"
            "本次模型只看 caller_model.model_name，不使用训练记忆或旧历史回答当前模型名称。"
            "模型名是请求配置，不是供应商内部模型身份的独立鉴定；不要改写版本号。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "include_log_diagnostics": {
                    "type": "boolean",
                    "description": (
                        "是否统计本次 Gateway 生命周期的异常签名；默认 true。"
                        "只返回计数，不返回原始日志。"
                    ),
                }
            },
            "additionalProperties": False,
        },
        hints=ToolModelHints(
            category="system",
            use_cases=(
                "检查当前唯一 Gateway 是否存活以及心跳是否新鲜",
                "确认真实 Gateway 地址、状态端点、模型和配置来源",
                "检查当前 Gateway 生命周期是否出现异常日志",
            ),
            avoid_when=(
                "检查模型自己启动的业务服务时使用 process_session",
                "不要用 shell 的 ps/ss/lsof 替代本工具提供的权威事实",
                "不要读取或展示原始 Gateway 日志、请求正文或密钥",
            ),
            keywords=(
                "gateway status",
                "gateway health",
                "gateway port",
                "gateway log",
                "网关状态",
                "网关日志",
            ),
            examples=(
                '{"tool":"gateway_status"}',
                '{"tool":"gateway_status","include_log_diagnostics":false}',
            ),
        ),
    )
    runtime_policy = ToolRuntimePolicy(
        effect_resolver=EffectResolverPolicy("read_only"),
        sandbox_policy=SandboxPolicy("none"),
        concurrency_policy=ConcurrencyPolicy("parallel_safe"),
        resource_scopes=ResourceScopePolicy(
            mode="declared",
            static_scopes=("gateway:current",),
        ),
        output_policy=OutputPolicy(trust="runtime"),
        promotes_task=False,
        mutates_workspace=False,
    )

    # LLM: The agent is the sole path/config authority. Tests may pass a minimal compatible agent,
    # but production callers must not inject an arbitrary Gateway path through model arguments.
    # 函数用途: 绑定当前主代理，让工具从它的规范配置定位唯一 Gateway，而不是接受模型路径。
    def __init__(self, agent: object) -> None:
        self.agent = agent

    # LLM: caller_model 与真实模型请求共用执行上下文绑定；仅读白名单，不泄漏 key/headers，也不热切正在工作的模型。
    # 函数用途: 返回网关健康信息和本次真正配置的模型，防止多个会话共享网关时答成启动默认模型。
    def execute(self, params: dict[str, Any]) -> ToolHandlerOutcome:
        include_diagnostics = params.get("include_log_diagnostics", True)
        snapshot = gateway_runtime_snapshot(
            self.agent,
            gateway_paths(self.agent),
            include_log_diagnostics=bool(include_diagnostics),
        )
        config = getattr(self.agent, "config", None)
        backend = getattr(self.agent, "backend", None)
        source = (getattr(config, "config_sources", {}) or {}).get("model_name", {})
        snapshot["caller_model"] = {
            "model_name": str(getattr(backend, "model_name", "") or getattr(config, "model_name", "") or ""),
            "backend": str(getattr(backend, "name", "") or getattr(config, "model_backend", "") or ""),
            "source": "calling_agent_execution_config",
            "profile_id": str(source.get("profile_id") or ""),
        }
        return ToolHandlerOutcome(
            self.model_spec.name,
            True,
            json.dumps(snapshot, ensure_ascii=False),
        )


__all__ = ["GatewayStatusTool"]
