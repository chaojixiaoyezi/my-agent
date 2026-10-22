# LLM: 这里只投影已有选择、依赖和模型调用账；不是配置源，不新建账本，不推断供应商内部身份或缓存结果。
# 模块用途: 让模型菜单区分已保存选择、待采用状态和最近一次实际调用记录。
from __future__ import annotations


# LLM: 精确thread过滤在原owner账本上完成；估算/供应商用量与组装/轮间耗时分开投影，不建账本、不读正文。
# 函数用途: 返回本进程最近一次该会话调用的安全指标，缺记录或首轮间隔仍为未知。
def latest_thread_model_call(agent: object, thread_id: str) -> dict | None:
    ledger = getattr(agent, "_model_call_ledger", None)
    if ledger is None:
        return None
    for row in reversed(ledger.records()):
        if row.is_probe or not row.request_id or row.metadata.get("thread_id") != thread_id:
            continue
        selection = row.metadata.get("model_selection") or {}
        reported = set(row.provider_usage_fields or ())
        input_tokens = int(row.accounted_input_tokens if row.status == "finished" else row.input_tokens)
        cached_input = row.cached_input_tokens if "cache_read_input_tokens" in reported else None
        return {"call_id": row.call_id, "request_id": row.request_id, "run_id": row.run_id, "task_id": row.metadata.get("task_id"),
                "model_name": row.model, "backend": row.backend, "status": row.status,
                "call_time_clock": "monotonic", "selection_time_clock": "unix",
                "started_at": row.started_at, "finished_at": row.finished_at,
                "context_assembly_ms": row.metadata.get("context_assembly_ms"),
                "inter_round_gap_ms": row.metadata.get("inter_round_gap_ms"),
                "estimated_input_tokens": row.metadata.get("estimated_input_tokens", row.input_tokens),
                "duration_ms": round(max(0.0, float(row.total_latency_seconds)) * 1000.0, 3) if row.total_latency_seconds is not None else None,
                "first_token_wait_ms": round(max(0.0, float(row.first_token_latency_seconds)) * 1000.0, 3) if row.first_token_latency_seconds is not None else None,
                "profile_id": selection.get("profile_id"), "selection_revision": selection.get("revision"),
                "selection_source": selection.get("selection_source"),
                "selection_thread_id": selection.get("selection_thread_id"), "scheduler_run_id": selection.get("scheduler_run_id"),
                "selected_at": selection.get("selected_at"), "adopted_at": selection.get("adopted_at"),
                "provider_usage_reported": row.provider_usage_reported,
                "input_tokens": input_tokens,
                "cached_input_tokens": cached_input,
                "uncached_input_tokens": max(0, input_tokens - int(cached_input or 0)) if cached_input is not None else None,
                "output_tokens": row.output_tokens,
                "cache_creation_input_tokens": row.cache_creation_input_tokens if "cache_write_input_tokens" in reported else None}
    return None


# LLM: 仅用于显式 /model 控制回执；started 是宿主调用已开始，completed 才是调用完成，不把绑定当成发送证明。
# 函数用途: 从结构化状态生成菜单说明，不读取任务内容或模型自述。
def render_model_execution_status(status: object) -> str:
    if not isinstance(status, dict):
        return ""
    lines = []
    if status.get("pending") is True:
        bound = status.get("bound") or {}
        phase = {"compacting": "正在压缩", "thinking": "正在采样", "responding": "正在生成回复",
                 "tool": "工具阶段", "waiting_permission": "等待工具审批", "retrying": "重试阶段"}.get(
                     (status.get("activity") or {}).get("phase"), "执行阶段未确认")
        lines.append(f"待采用：当前使用 {bound.get('model_name') or '未知模型'}（{phase}），下一安全请求边界采用新选择。")
    latest = status.get("last_request")
    if isinstance(latest, dict):
        label = {"finished": "已完成", "failed": "已失败", "timed_out": "已超时"}.get(latest.get("status"), "已开始")
        lines.append(f"最近请求：{latest.get('model_name') or '未知模型'}（{label}；选择版本 {latest.get('selection_revision') or '未知'}）。")
    else:
        lines.append("最近请求：本进程暂无记录；保存选择不等于请求已发出。")
    return "\n".join(lines)
