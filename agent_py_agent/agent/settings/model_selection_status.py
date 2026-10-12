# LLM: 这里只投影已有选择、依赖和模型调用账；不是配置源，不新建账本，不推断供应商内部身份或缓存结果。
# 模块用途: 让模型菜单区分已保存选择、待采用状态和最近一次实际调用记录。
from __future__ import annotations


# LLM: 精确 thread 过滤在原 owner Agent 的账本上完成；只读公开字段，不调用会创建账本的工厂。
# 函数用途: 返回本进程最近一条该会话业务请求的元数据；重启或没有记录时返回未知。
def latest_thread_model_call(agent: object, thread_id: str) -> dict | None:
    ledger = getattr(agent, "_model_call_ledger", None)
    if ledger is None:
        return None
    for row in reversed(ledger.records()):
        if row.is_probe or not row.request_id or row.metadata.get("thread_id") != thread_id:
            continue
        selection = row.metadata.get("model_selection") or {}
        reported = set(row.provider_usage_fields or ())
        return {"call_id": row.call_id, "request_id": row.request_id, "run_id": row.run_id, "task_id": row.metadata.get("task_id"),
                "model_name": row.model, "backend": row.backend, "status": row.status,
                "call_time_clock": "monotonic", "selection_time_clock": "unix",
                "started_at": row.started_at, "finished_at": row.finished_at,
                "profile_id": selection.get("profile_id"), "selection_revision": selection.get("revision"),
                "selection_source": selection.get("selection_source"),
                "selection_thread_id": selection.get("selection_thread_id"), "scheduler_run_id": selection.get("scheduler_run_id"),
                "selected_at": selection.get("selected_at"), "adopted_at": selection.get("adopted_at"),
                "provider_usage_reported": row.provider_usage_reported,
                "cached_input_tokens": row.cached_input_tokens if "cache_read_input_tokens" in reported else None,
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
