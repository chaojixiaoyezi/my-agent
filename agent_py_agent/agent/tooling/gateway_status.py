from __future__ import annotations

"""Expose the current local Gateway's canonical runtime facts to the main agent."""

# LLM: 当前模型只读调用者冻结的 config/backend，不能读 Gateway 启动默认值或后来切换的 owner 选择。This surface reuses Gateway state,
# heartbeat, validated PID records and lifecycle-bounded diagnostics; never fall back to ps/ss,
# guessed ports, raw logs, request bodies, or credentials.
# 模块用途: 让本机管理员主代理直接读唯一Gateway健康事实与本代有限安全诊断，日志受保护时仍可自查而不开放原文。

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
            "诊断日志读取被保护、native物化或执行异常时仍用本工具：返回本代最多8条安全阶段、错误码和有限资源分类，"
            "不读出原始正文；具体用户请求归属用audit_records及原跨用户审计授权核验，不从全局日志猜归属。"
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
                        "返回计数及最多8条安全native机器诊断；native记录不含原始日志、ID、路径或正文。"
                    ),
                },
                "metadata_only": {"type": "boolean", "description": "仅返回进程、队列、模型选择和状态元数据；不读日志诊断，不返回路径、地址或原始错误。"},
            },
            "additionalProperties": False,
        },
        hints=ToolModelHints(
            category="system",
            use_cases=(
                "检查当前唯一 Gateway 是否存活以及心跳是否新鲜",
                "确认真实 Gateway 地址、状态端点、模型和配置来源",
                "检查当前 Gateway 生命周期是否出现异常日志",
                "查看本 owner 反复失败、已结案不再自动领取的后台唤醒条数（quarantined_wakes；详情由管理员用 /wakes 查看）",
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
            default_deferred=True,
            deferred_summary="查Gateway健康、日志/native阶段错误码与资源缺失诊断（不读raw；不用ps/ss猜）",
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

    # LLM: caller_model 共用请求依赖；metadata_only 关闭日志读取并从入口白名单投影，绑定与实际 HTTP 事实不混同。
    # 函数用途: 返回网关健康及模型选择状态，诊断可排除路径、地址和原始错误。
    def execute(self, params: dict[str, Any]) -> ToolHandlerOutcome:
        metadata_only = params.get("metadata_only", False)
        if not isinstance(metadata_only, bool):
            return ToolHandlerOutcome("gateway_status", False, '{"error_code":"TOOL_INVALID_ARGUMENTS"}', error_code="TOOL_INVALID_ARGUMENTS")
        try:
            snapshot = self._snapshot(params, metadata_only=metadata_only)
        except Exception:  # noqa: BLE001 - 元数据诊断不能从错误出口回显私有路径
            if not metadata_only:
                raise
            return ToolHandlerOutcome("gateway_status", False, '{"error_code":"TOOL_EXECUTION_FAILED"}', error_code="TOOL_EXECUTION_FAILED")
        return ToolHandlerOutcome(self.model_spec.name, True, json.dumps(snapshot, ensure_ascii=False))

    # LLM: 只读现有 Gateway 快照和精确当前 thread 选择；错误保持未知，不猜测其他会话或启动默认模型。
    # 函数用途: 组装完整状态或纯元数据状态，不修改模型、Goal 或后台运行。
    def _snapshot(self, params: dict, *, metadata_only: bool) -> dict:
        include_diagnostics = params.get("include_log_diagnostics", True)
        snapshot = gateway_runtime_snapshot(
            self.agent,
            gateway_paths(self.agent),
            include_log_diagnostics=bool(include_diagnostics) and not metadata_only,
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
        snapshot["quarantined_wakes"] = _quarantined_wake_count(self.agent)
        current = getattr(self.agent, "_current_run_params", None)
        thread_id = str((getattr(current, "task_attributes", None) or {}).get("conversation_thread_id") or "")
        if thread_id:
            from ..settings.model_scope import thread_model_execution_status

            snapshot["model_selection"] = thread_model_execution_status(self.agent, thread_id)
        return _metadata_projection(snapshot) if metadata_only else snapshot


# LLM: 嵌套对象逐字段白名单，未来完整快照新增正文或地址不会自动泄漏；不复制 load_errors 或日志内容。
# 函数用途: 将原 Gateway 快照转换成可供无正文诊断使用的状态。
def _metadata_projection(snapshot: dict) -> dict:
    result = {key: snapshot.get(key) for key in (
        "schema", "status", "alive", "uptime_seconds", "quarantined_wakes",
    )}
    result["metadata_only"] = True
    for group, keys in (
        ("identity", ("pid", "process_start_time")),
        ("heartbeat", ("updated_at", "age_seconds", "stale_after_seconds")),
        ("queue", ("pending", "processing", "oldest_pending_age_seconds", "oldest_processing_age_seconds")),
        ("caller_model", ("model_name", "backend", "source", "profile_id")),
    ):
        row = snapshot.get(group) or {}
        result[group] = {key: row.get(key) for key in keys}
    result["queue"]["inflight_total"] = ((snapshot.get("queue") or {}).get("inflight") or {}).get("total")
    selection = snapshot.get("model_selection")
    if isinstance(selection, dict):
        result["model_selection"] = {key: selection.get(key) for key in (
            "thread_id", "state", "reason_code", "pending", "pending_reason",
        )}
        for group, keys in (
            ("selected", ("profile_id", "revision", "source", "selected_at")),
            ("bound", ("model_name", "backend", "profile_id", "selection_revision", "explicit_revision", "adopted_at",
                       "selection_source", "selection_thread_id", "scheduler_run_id")),
            ("activity", ("phase", "updated_at")),
            ("last_request", ("call_id", "request_id", "run_id", "task_id", "model_name", "backend", "status", "started_at", "finished_at",
                              "profile_id", "selection_revision", "selected_at", "adopted_at", "provider_usage_reported",
                              "selection_source", "selection_thread_id", "scheduler_run_id", "call_time_clock", "selection_time_clock",
                              "cached_input_tokens", "cache_creation_input_tokens")),
        ):
            row = selection.get(group)
            result["model_selection"][group] = {key: row.get(key) for key in keys} if isinstance(row, dict) else None
    return result


# LLM: 只数调用方 owner 的结案目录文件（store.wakes.attempts.quarantined_count，不读内容、不含归档）；读不到会话存储时
#   返回 None，状态工具不能因为这一项失败。
# 函数用途: 取本 owner 已结案、不再自动领取的后台唤醒条数，读不到时为 None。
def _quarantined_wake_count(agent: object) -> int | None:
    try:
        return int(agent.conversation_store.wakes.attempts.quarantined_count())
    except Exception:  # noqa: BLE001 - 状态面尽力统计
        return None


__all__ = ["GatewayStatusTool"]
