from __future__ import annotations

"""Expose the current local Gateway's canonical runtime facts to the main agent."""

# LLM: 当前模型只读调用者冻结的 config/backend，不能读 Gateway 启动默认值或后来切换的 owner 选择。This surface reuses Gateway state,
# heartbeat, validated PID records and lifecycle-bounded diagnostics; never fall back to ps/ss
# or guessed ports. Only explicit log-tail requests add bounded, scrubbed observational text.
# 模块用途: 让本机管理员读唯一Gateway权威健康；默认有限安全摘要，显式诊断可取脱敏有界日志尾。

import json
import math
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
# 类用途: 返回管理员的Gateway权威事实及默认无原文摘要，显式tail参数只增加有界脱敏观察。
class GatewayStatusTool(BaseTool):
    model_spec = ToolModelSpec(
        name="gateway_status",
        description=(
            "读取当前 my-agent 唯一 Gateway 的权威运行状态。检查 Gateway 健康、真实监听端口、"
            "本次调用模型、配置来源、队列或日志噪声时必须优先使用本工具；不要用 ps/ss 猜端口，"
            "也不要把 /health 当作状态端点，HTTP 状态端点由结果中的 status_path 给出。"
            "诊断日志读取被保护、native物化或执行异常时仍用本工具：返回本代最多8条安全阶段、错误码和有限资源分类，"
            "不读出原始正文；具体用户请求归属用audit_records及原跨用户审计授权核验，不从全局日志猜归属。"
            "明确需要排查日志时可include_log_tail=true读取脱敏有界片段；log_offset_bytes/log_since/log_cursor可向前分页历史，每页最多32768字节。full_file仅解除本代边界，不代表一次遍历全文件。"
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
                "include_log_tail": {"type": "boolean", "description": "显式读取脱敏日志尾，默认false；metadata_only=true时始终不读。"},
                "log_tail_lines": {"type": "integer", "description": "每个日志最多返回多少行，默认80，夹取到1..200；所有来源共用32768字节采样及返文上限。"},
                "log_source": {"type": "string", "enum": ["gateway", "supervisor", "both"], "description": "日志来源，默认gateway；不能传文件路径。"},
                "log_scope": {"type": "string", "enum": ["current_lifecycle", "full_file"], "description": "默认current_lifecycle；full_file可回看跨重启记录，默认尾窗，显式offset/time/cursor按有界页向前读，不是一次全文件搜索。"},
                "log_query": {"type": "string", "description": "可选不区分大小写的关键词，只在脱敏后的有界采样行中过滤，默认空。"},
                "include_queue_entries": {"type": "boolean", "description": "显式读取有界队列元数据页，默认false；需原审计授权。"},
                "queue_limit": {"type": "integer", "minimum": 1, "maximum": 100, "description": "队列页条数，默认20，上限沿原审计100。"},
                "queue_cursor": {"type": "string", "description": "原 queue_entries.next_cursor；保留同范围续页。"},
                "queue_scope": {"type": "string", "enum": ["owner", "all_owners"], "description": "默认owner；all_owners须原跨用户审计授权。"},
                "include_quarantined_wakes": {"type": "boolean", "description": "显式读取本owner结案唤醒元数据页，不读取正文。"},
                "quarantined_limit": {"type": "integer", "minimum": 1, "maximum": 100, "description": "唤醒页条数，默认20，上限100。"},
                "quarantined_cursor": {"type": "string", "description": "原 quarantined_wake_entries.next_cursor。"},
                "log_offset_bytes": {"type": "integer", "minimum": 0, "description": "显式前向日志字节起点；每页共用原32768字节预算。"},
                "log_since": {"type": "number", "description": "可选Unix秒起点，只过滤本次有界窗口；无匹配不代表全文件无匹配。"},
                "log_until": {"type": "number", "description": "可选Unix秒终点，缺时间戳行计入unknown覆盖。"},
                "log_cursor": {"type": "string", "description": "原 log_pages.next_cursor；保持来源、范围及时间/关键词过滤器不变。轮转后清空重新开始。"},
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
                "不要把有界日志当全文件搜索或用户请求归属证明，不展示凭据；原文观察须显式include_log_tail",
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
            deferred_summary="查Gateway权威健康与native摘要；显式可读脱敏有界双日志尾，不用ps/ss猜",
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

    # LLM: 新tail参数直接调用也须类型/枚举校验；metadata_only关闭全部日志读取，错误不回显参数或私有路径。
    # 函数用途: 只读返回健康及模型选择，默认无原文，显式日志请求仍有界脱敏。
    def execute(self, params: dict[str, Any]) -> ToolHandlerOutcome:
        metadata_only = params.get("metadata_only", False)
        if not isinstance(metadata_only, bool) or not _valid_log_tail_parameters(params):
            invalid = [key for key, value in params.items() if not _valid_log_tail_parameters({key: value})]
            if not isinstance(metadata_only, bool):
                invalid.append("metadata_only")
            if not invalid:
                invalid = ["log_since", "log_until"]
            details = {"error_code": "TOOL_INVALID_ARGUMENTS", "effect_outcome": "not_started",
                       "invalid_fields": [{"field": key, "actual_type": type(params.get(key)).__name__} for key in invalid],
                       "reason": "gateway_status 参数类型、枚举、页大小或时间范围不符合 input_schema。",
                       "next_action": "按 schema 修正参数；例如 {\"include_queue_entries\":true,\"queue_limit\":20,\"queue_scope\":\"owner\"}。日志续页保持原过滤条件。"}
            return ToolHandlerOutcome("gateway_status", False, json.dumps(details, ensure_ascii=False), error_code="TOOL_INVALID_ARGUMENTS")
        try:
            snapshot = self._snapshot(params, metadata_only=metadata_only)
        except Exception:  # noqa: BLE001 - 元数据诊断不能从错误出口回显私有路径
            if not metadata_only:
                raise
            return ToolHandlerOutcome("gateway_status", False, '{"error_code":"TOOL_EXECUTION_FAILED"}', error_code="TOOL_EXECUTION_FAILED")
        return ToolHandlerOutcome(self.model_spec.name, True, json.dumps(snapshot, ensure_ascii=False))

    # LLM: 只读规范Gateway及当前thread；仅显式且非metadata请求透传tail选项，旧默认kwargs与模型选择保持。
    # 函数用途: 组装状态与可选脱敏日志，不修改模型、Goal、后台运行或权限。
    def _snapshot(self, params: dict, *, metadata_only: bool) -> dict:
        include_diagnostics = params.get("include_log_diagnostics", True)
        log_options = {}
        if params.get("include_log_tail", False) and not metadata_only:
            log_options = {"include_log_tail": True, "log_tail_lines": params.get("log_tail_lines", 80),
                           "log_source": params.get("log_source", "gateway"),
                           "log_scope": params.get("log_scope", "current_lifecycle"), "log_query": params.get("log_query", "")}
        for key in ("include_queue_entries", "queue_limit", "queue_cursor", "queue_scope",
                    "include_quarantined_wakes", "quarantined_limit", "quarantined_cursor"):
            if key in params:
                log_options[key] = params[key]
        if not metadata_only:
            for key in ("log_offset_bytes", "log_since", "log_until", "log_cursor"):
                if key in params:
                    log_options[key] = params[key]
            if any(key in params for key in ("log_offset_bytes", "log_since", "log_until", "log_cursor")):
                for key, default in (("log_source", "gateway"), ("log_scope", "current_lifecycle"), ("log_query", ""), ("log_tail_lines", 80)):
                    log_options[key] = params.get(key, default)
        snapshot = gateway_runtime_snapshot(
            self.agent,
            gateway_paths(self.agent),
            include_log_diagnostics=bool(include_diagnostics) and not metadata_only,
            **log_options,
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


# LLM: 纯参数检查，不读日志；整数不接受bool，行数范围由原渲染入口夹取，新增参数不改变旧诊断默认值。
# 函数用途: 防止直接handler调用绕过新日志参数的schema边界。
def _valid_log_tail_parameters(params: dict) -> bool:
    return (isinstance(params.get("include_log_tail", False), bool)
            and type(params.get("log_tail_lines", 80)) is int
            and params.get("log_source", "gateway") in ("gateway", "supervisor", "both")
            and params.get("log_scope", "current_lifecycle") in ("current_lifecycle", "full_file")
            and isinstance(params.get("log_query", ""), str)
            and all(type(params.get(key, False)) is bool for key in ("include_queue_entries", "include_quarantined_wakes"))
            and all(type(params.get(key, 20)) is int and 1 <= params.get(key, 20) <= 100 for key in ("queue_limit", "quarantined_limit"))
            and all(isinstance(params.get(key, ""), str) for key in ("queue_cursor", "quarantined_cursor", "log_cursor"))
            and params.get("queue_scope", "owner") in ("owner", "all_owners")
            and (params.get("log_offset_bytes") is None or type(params["log_offset_bytes"]) is int and params["log_offset_bytes"] >= 0)
            and all(params.get(key) is None or type(params[key]) in (int, float) and math.isfinite(params[key]) for key in ("log_since", "log_until"))
            and (params.get("log_since") is None or params.get("log_until") is None or params["log_since"] <= params["log_until"]))


# LLM: 嵌套对象逐字段白名单；只透传已有数值用量及组装/轮间耗时，新增正文、地址、错误或日志不会自动泄漏。
# 函数用途: 将原Gateway快照投影为无正文诊断，保留估算与实报token的区别和未知值。
def _metadata_projection(snapshot: dict) -> dict:
    result = {key: snapshot.get(key) for key in (
        "schema", "status", "alive", "uptime_seconds", "quarantined_wakes",
    )}
    for key in ("queue_entries", "quarantined_wake_entries"):
        if key in snapshot:
            result[key] = snapshot[key]
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
                              "context_assembly_ms", "inter_round_gap_ms", "estimated_input_tokens", "duration_ms", "first_token_wait_ms",
                              "profile_id", "selection_revision", "selected_at", "adopted_at", "provider_usage_reported",
                              "selection_source", "selection_thread_id", "scheduler_run_id", "call_time_clock", "selection_time_clock",
                              "input_tokens", "cached_input_tokens", "uncached_input_tokens", "output_tokens", "cache_creation_input_tokens")),
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
