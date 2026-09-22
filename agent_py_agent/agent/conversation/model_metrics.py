# LLM: 这是原 ModelCallLedger 的显示投影；用途分区不重复累计，不改变预算/输入/任务，主子按 exact thread 隔离。
# 模块用途: 提供原模型统计行及决策输入，未知保留未知，后台新用量让旧显示基数失效。
from __future__ import annotations

import logging
import math
import time
from collections.abc import Mapping

_LOGGER = logging.getLogger(__name__)
_SCHEMA = "model_runtime_metrics.v1"
_COUNTS = (
    "model_rounds", "retry_count", "input_tokens", "output_tokens",
    "estimated_tokens", "unreported_calls", "sampled_at_ns",
)


# LLM: 数值白名单不接受 bool、负数和无穷大，调用方不得借此传递提示词或工具参数。
# 函数用途: 检查可展示的计数字段，缺失值保持未知。
def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) and result >= 0 else None


# LLM: 通道共用数值白名单；决策输入缺报保持 None，部分已报另带完整性，不从空值猜供应商零消耗。
# 函数用途: 清洗统计条、决策输入及缓存诊断，拒绝未知 schema 和任意嵌套正文。
def public_model_metrics(value: object) -> dict[str, object]:
    from ..backends.cache_diagnostics import public_cache_diagnostic

    if not isinstance(value, Mapping) or value.get("schema") != _SCHEMA:
        return {}
    diagnostic = public_cache_diagnostic(value.get("cache_diagnostic"))
    decision_calls = int(_number(value.get("decision_call_count")) or 0)
    decision_input = _number(value.get("decision_input_tokens"))
    return {
        "schema": _SCHEMA,
        **{key: int(_number(value.get(key)) or 0) for key in _COUNTS},
        "tool_count": int(_number(value.get("tool_count"))) if _number(value.get("tool_count")) is not None else None,
        "cache_percent": min(100.0, _number(value.get("cache_percent"))) if _number(value.get("cache_percent")) is not None else None,
        "output_tps": _number(value.get("output_tps")),
        "pending": value.get("pending") is True,
        "totals_known": value.get("totals_known") is True,
        **({"cache_diagnostic": diagnostic} if diagnostic else {}),
        **({"decision_call_count": decision_calls,
            "decision_input_tokens": int(decision_input) if decision_input is not None else None,
            "decision_input_complete": value.get("decision_input_complete") is True}
           if decision_calls else {}),
    }


# LLM: 迟到的后台轮询和流重放只能保留较新的采样；时间戳只用于显示，不拥有调度或计费权限。
# 函数用途: 合并同一个代理页面的统计快照，避免轮询旧帧令数字倒退。
def newer_model_metrics(current: object, incoming: object) -> dict[str, object]:
    old, new = public_model_metrics(current), public_model_metrics(incoming)
    if not new or (old and new["sampled_at_ns"] < old["sampled_at_ns"]):
        return old
    return new


# LLM: 只读 exact thread 上的显示副本；账本仍是唯一费用权威，不读取文本历史或子代理目录。
# 函数用途: 在空闲、重连和切入子代理页面时取回最近统计条。
def model_metrics_from_thread(store: object, thread_id: str) -> dict[str, object]:
    loader = getattr(getattr(store, 'threads', None), 'load_report', None)
    if not thread_id or not callable(loader):
        return {}
    try:
        thread, error = loader(thread_id)
        return public_model_metrics(getattr(thread, "model_metrics", None)) if not error else {}
    except (OSError, RuntimeError, TypeError, ValueError):
        return {}


# LLM: 原用量文件变更才重读事件，仅排除相同统计代次 request/run；重启旧账仍加入基数。
# 事件读取只走 model_usage 领域；缺失能力保持既有嵌入式调用边界，不回退旧 Store 方法。
# 函数用途: 取得本会话以前已经落账的消耗基数，长输出的每个 token 不会触发历史扫描。
def _previous_totals(agent: object, params: object, thread_id: str, usage_scope_id: str) -> dict[str, object]:
    state = getattr(params, "live_archive_state", None)
    key = (thread_id, str(getattr(params, "request_id", "") or ""), str(getattr(params, "run_id", "") or ""), usage_scope_id)
    store = getattr(agent, "conversation_store", None)
    usage_store = getattr(store, "model_usage", None)
    revision_reader = getattr(usage_store, "revision", None)
    revision = revision_reader(thread_id) if thread_id and callable(revision_reader) else None
    cached = state.get("_model_metrics_baseline") if isinstance(state, dict) else None
    if isinstance(cached, tuple) and cached[0] == (key, revision):
        return dict(cached[1])
    totals = {"input_tokens": 0, "output_tokens": 0, "estimated_tokens": 0, "unreported_calls": 0, "totals_known": True,
              "decision_call_count": 0, "decision_input_tokens": None, "decision_input_complete": True}
    reader = getattr(usage_store, "events_report", None)
    if thread_id and callable(reader):
        try:
            events, errors = reader(thread_id)
            totals["totals_known"] = not errors
            for event in events:
                if (event.request_id, event.run_id, str(event.model_calls.get("usage_scope_id") or "")) == key[1:]:
                    continue
                _add_usage(totals, event.model_calls)
        except (OSError, RuntimeError, TypeError, ValueError):
            totals["totals_known"] = False
    if isinstance(state, dict):
        state["_model_metrics_baseline"] = ((key, revision), dict(totals))
    return totals


# LLM: provider input 已含缓存读写，不再加缓存；决策只是原总量子集，逐字段缺报不能由信封计数猜测。
# 函数用途: 汇总原 token 并附加决策输入显示，旧账没有用途时不推断历史决策为零。
def _add_usage(totals: dict[str, object], summary: Mapping[str, object]) -> None:
    breakdown = summary.get("usage_breakdown")
    breakdown = breakdown if isinstance(breakdown, Mapping) else {}
    provider, estimated = breakdown.get("provider", {}), breakdown.get("estimated", {})
    for key in ("input_tokens", "output_tokens"):
        totals[key] += int(provider.get(key) or 0)
    totals["estimated_tokens"] += sum(int(estimated.get(key) or 0) for key in ("input_tokens", "output_tokens"))
    statuses = summary.get("status_counts") or {}
    totals["unreported_calls"] += int(estimated.get("call_count") or 0) + sum(int(statuses.get(key) or 0) for key in ("failed", "timed_out"))
    purposes = summary.get("purpose_breakdown")
    if not isinstance(purposes, Mapping):
        if summary.get("physical_model_attempt_count"):
            totals["decision_input_complete"] = False
        return
    decision = purposes.get("decision", {})
    calls = int(decision.get("physical_model_attempt_count") or 0)
    usage = decision.get("usage_breakdown", {}).get("provider", {})
    reported = int(usage.get("input_tokens_reported_call_count") or 0)
    totals["decision_call_count"] += calls
    if reported:
        totals["decision_input_tokens"] = int(totals["decision_input_tokens"] or 0) + int(usage.get("input_tokens") or 0)
    totals["decision_input_complete"] = totals["decision_input_complete"] and calls == reported


# LLM: 遥测只在调用边界发布；usage_only 保留生成状态且只刷新活动显示，不等待持久会话写锁；调用方必须传原活动范围。
# 函数用途: 更新原会话消耗及生成统计，短决策只刷新当前用量行；持久显示由下一原模型边界更新，显示异常不改变执行结果。
def publish_model_metrics(agent: object, params: object, *, pending: bool, tool_count: int | None = None, response: object = None, call_id: str = "", usage_only: bool = False) -> dict[str, object]:
    try:
        return _publish_metrics(agent, params, pending=pending, tool_count=tool_count, response=response, call_id=call_id, usage_only=usage_only)
    except Exception:
        # 显示故障不能让一次本来成功的模型响应或工具执行变成任务失败。
        _LOGGER.warning("模型统计显示更新失败；不影响模型与工具执行", exc_info=False)
        return {}


# LLM: 使用原范围账本快照；决策分区不计普通轮，usage_only 不覆盖生成状态或等待写锁，异常交外层隔离。
# 显示写入走 model_usage 组件的原线程更新能力，不给统计页增加持久写入入口。
# 函数用途: 实现一次模型边界的统计采样和发布，不在 UI 渲染线程扫描数据。
def _publish_metrics(agent: object, params: object, *, pending: bool, tool_count: int | None, response: object, call_id: str, usage_only: bool) -> dict[str, object]:
    if str(getattr(params, "context_scope", "") or "").lower() == "isolated":
        return {}
    ledger = getattr(agent, "_model_call_ledger", None)
    summary_reader = getattr(ledger, "cumulative_summary", None)
    if not callable(summary_reader):
        return {}
    attrs = getattr(params, "task_attributes", None)
    attrs = attrs if isinstance(attrs, Mapping) else {}
    thread_id = str(attrs.get("agent_thread_id") or attrs.get("conversation_thread_id") or "")
    summary = summary_reader(request_id=str(getattr(params, "request_id", "") or ""), run_id=str(getattr(params, "run_id", "") or "")) or {}
    state = getattr(params, "live_archive_state", None)
    previous = state.get("_model_metrics_current", {}) if isinstance(state, dict) else {}
    if usage_only:
        previous = newer_model_metrics(previous, model_metrics_from_thread(getattr(agent, "conversation_store", None), thread_id))
    totals = _previous_totals(agent, params, thread_id, str(summary.get("usage_scope_id") or ""))
    _add_usage(totals, summary)
    payload = {**previous, **totals, "schema": _SCHEMA, "sampled_at_ns": time.time_ns()}
    if not usage_only:
        decision = (summary.get("purpose_breakdown") or {}).get("decision", {})
        payload.update(model_rounds=int(summary.get("logical_model_turn_count") or 0) - int(decision.get("logical_model_turn_count") or 0),
            retry_count=sum(int(summary.get(key) or 0) - int(decision.get(key) or 0) for key in ("model_retry_count", "provider_http_retry_count")),
            tool_count=tool_count, pending=pending)
    if response is not None and not usage_only:
        payload.update(_last_response_metrics(response, ledger, call_id))
    public = public_model_metrics(payload)
    if isinstance(state, dict):
        state["_model_metrics_current"] = public
    store = getattr(agent, "conversation_store", None)
    updater = getattr(getattr(store, "model_usage", None), "update_metrics", None)
    try:
        if not usage_only and thread_id and callable(updater):
            updater(thread_id, public)
        sink = getattr(params, "effective_on_chunk", None)
        writer = getattr(sink, "write_model_metrics", None)
        if callable(writer):
            writer(public)
    except (OSError, RuntimeError, TypeError, ValueError):
        _LOGGER.warning("模型统计显示更新失败；不影响模型与工具执行", exc_info=False)
    return public


# LLM: 速度及缓存来自 exact call_id 的生成调用；决策无权覆盖这些指标，请求比较只写显示副本。
# 函数用途: 得到最近结算的缓存、速度和前缀变化原因；随已有 thread 统计持久保存，不建立第二套账本。
def _last_response_metrics(response: object, ledger: object, call_id: str) -> dict[str, object]:
    from ..agent_core.model.usage import (
        input_token_usage,
        output_token_usage,
        reported_cache_read_token_usage,
        response_usage,
    )

    record = next((record for record in ledger.records() if record.call_id == call_id), None)
    if record and record.metadata.get("purpose") == "decision":
        return {}
    usage = response_usage(response)
    known = any(_number(usage.get(key)) is not None for key in ("cache_read_input_tokens", "cached_input_tokens"))
    for key in ("input_tokens_details", "prompt_tokens_details"):
        detail = usage.get(key)
        if isinstance(detail, Mapping):
            known = known or any(_number(detail.get(key)) is not None for key in ("cached_tokens", "cache_read_tokens"))
    denominator = input_token_usage(response)
    cache = 100.0 * reported_cache_read_token_usage(response) / denominator if known and denominator else None
    elapsed = (record.finished_at - record.first_token_at) if record and record.finished_at is not None and record.first_token_at is not None else 0
    output = output_token_usage(response)
    return {"cache_percent": cache, "output_tps": output / elapsed if output is not None and elapsed >= 0.1 else None,
            "cache_diagnostic": record.metadata.get("cache_diagnostic") if record else None}
