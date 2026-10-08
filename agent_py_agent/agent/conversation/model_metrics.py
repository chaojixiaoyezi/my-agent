# LLM: 这是原 ModelCallLedger 的显示投影；缓存累计与去重身份同快照保存/恢复，不建第二份账本，不改变预算/输入/任务，主子按 exact thread 隔离。
# 模块用途: 提供原模型统计行及决策输入与成败次数，未知保留未知，后台新用量让旧显示基数失效。
from __future__ import annotations

import hashlib
import logging
import math
import time
from collections.abc import Mapping

_LOGGER = logging.getLogger(__name__)
_SCHEMA = "model_runtime_metrics.v1"
# 全部用途的计数；failure_count/unfinished_calls/unfinished_tokens/unknown_failures 是跨事件累加的原始次数
# （口径见 unfinished_usage_facts），展示时减去决策分区得到非决策调用，再按 split_unsent_failures 推导
_COUNTS = (
    "model_rounds", "retry_count", "input_tokens", "output_tokens", "estimated_tokens", "sampled_at_ns",
    "failure_count", "unfinished_calls", "unfinished_tokens", "unknown_failures",
    # 10-08：会话累计的缓存命中输入（供应商回报的 cache_read）和回报过缓存字段的调用次数，一起算会话累计命中率；
    # 旧快照没有这两个键按 0 读，没回报过缓存字段的供应商不显示累计值（不能把"没报"显示成 0%）。
    "cache_read_input_tokens", "cache_read_reported_calls",
)
# 决策分区的显示计数（都是跨事件累加的原始次数，展示时再推导）：已报输入的调用次数（供约数外推）、成功（finished）、
# 失败（failed + timed_out 原始次数）、其中已发出后未完成的次数与本地估算输入、分不清是否发出的旧账失败次数
_DECISION_COUNTS = ("decision_input_reported_calls", "decision_success_count", "decision_failure_count",
                    "decision_unfinished_calls", "decision_estimated_tokens", "decision_unknown_failures")


# LLM: 缓存前缀变化按原因码累计，只增不减：一次比较只说明“这一次调用换过什么”，累计次数才能回答
#   “修复之后有没有回退”。累计对着原因码逐个加，未出现过的不写 0（缺失即从未发生）。
# 函数用途: 把新一次比较的原因码累加进已有的累计计数。
def _add_cache_change_counts(previous: object, diagnostic: object) -> dict[str, int]:
    from ..backends.cache_diagnostics import change_codes

    allowed = change_codes()
    # previous 可能是 None（首次发布、或上一份显示副本被清掉），此时从零开始累计，不能对 None 取 items。
    counts = {key: int(value) for key, value in (previous.items() if isinstance(previous, Mapping) else ())
              if key in allowed and type(value) is int and value > 0}
    changes = diagnostic.get("changes") if isinstance(diagnostic, Mapping) else None
    for code in changes if isinstance(changes, list) else []:
        if isinstance(code, str) and code in allowed:
            counts[code] = counts.get(code, 0) + 1
    return counts


# LLM: 累计计数走与单次诊断同一套原因码白名单（cache_diagnostics.change_codes），避免两处名单漂移；
#   旧记录没有这个键时返回空字典，不补全 0，也不让缺字段变成读取失败。
# 函数用途: 清洗持久化的 cache_change_counts，只保留已知原因码的正整数计数。
def public_cache_change_counts(value: object) -> dict[str, object]:
    from ..backends.cache_diagnostics import change_codes

    if not isinstance(value, Mapping):
        return {}
    allowed = change_codes()
    return {key: int(count) for key, count in value.items()
            if key in allowed and type(count) is int and count > 0}


# LLM: 身份只允许 SHA256 小写十六进制串，不允许 call_id 原文或任意嵌套正文进入持久/通道显示副本。
#   去重集合与累计值同属 model_metrics；合法身份排序去重，旧记录缺字段时不猜测旧调用是否已累计。
# 函数用途: 清洗已累计调用的不可逆身份，供原线程快照保存和恢复。
def public_cache_counted_calls(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return sorted({item for item in value if isinstance(item, str) and len(item) == 64
                   and not set(item) - set("0123456789abcdef")})


# LLM: 基数与已计数身份只能取同一份较新 model_metrics 快照；不再从旧回合状态单独读身份。
#   只有 exact call 的新诊断才加一次；身份散列随同累计值返回，走原线程原子保存。新结算时身份集合
#   收敛到账本仍保留的调用：被裁掉的调用已无诊断可重新累计；无新诊断时完整保留恢复事实。
# 函数用途: 计算本次缓存累计与去重身份，支持旧内存重发、状态清空和交错重放，不写旁路文件。
def _cache_change_counts_for_publish(base: dict, ledger: object, identity: str, diagnostic: object) -> dict:
    counts = public_cache_change_counts(base.get("cache_change_counts"))
    counted = set(public_cache_counted_calls(base.get("cache_counted_calls")))
    if identity and isinstance(diagnostic, Mapping):
        digest = hashlib.sha256(identity.encode()).hexdigest()
        if digest not in counted:
            counts = _add_cache_change_counts(counts, diagnostic)
        retained = {hashlib.sha256(row.call_id.encode()).hexdigest() for row in ledger.records()}
        counted = (counted & retained) | {digest}
    return {"cache_change_counts": counts, "cache_counted_calls": sorted(counted)}


# LLM: 结算时把本次调用的响应指标并进 payload；没有新诊断（例如找不到调用记录）时不覆盖上一份，
#   保留已持久化的最近一次诊断，避免诊断被空值清掉。
# 函数用途: 合并响应指标并返回本次的新缓存诊断（没有则为 None）。
def _merge_response_metrics(payload: dict, response: object, ledger: object, call_id: str) -> object:
    response_metrics = _last_response_metrics(response, ledger, call_id)
    diagnostic = response_metrics.get("cache_diagnostic")
    if diagnostic is None:
        response_metrics.pop("cache_diagnostic", None)
    payload.update(response_metrics)
    return diagnostic


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


# LLM: 通道共用数值白名单；决策输入缺报保持 None，另带已报调用数与成败次数，不从空值猜供应商零消耗。
#   失败构成出现前写下的旧快照没有 decision_unknown_failures 键：与 unfinished_usage_facts 对旧用量行同一规则，
#   失败整体记为分不清是否发出，不能补 0（补 0 会让 split_unsent_failures 把旧失败说成“根本没发出去”）。
#   缓存累计与不可逆去重身份一起清洗，原线程保存及通道投影不得只保留其中一半。
# 函数用途: 清洗统计条、决策输入与成败次数及缓存诊断，拒绝未知 schema 和任意嵌套正文。
def public_model_metrics(value: object) -> dict[str, object]:
    from ..backends.cache_diagnostics import public_cache_diagnostic

    if not isinstance(value, Mapping) or value.get("schema") != _SCHEMA:
        return {}
    diagnostic = public_cache_diagnostic(value.get("cache_diagnostic"))
    change_counts = public_cache_change_counts(value.get("cache_change_counts"))
    counted_calls = public_cache_counted_calls(value.get("cache_counted_calls"))
    decision_calls = int(_number(value.get("decision_call_count")) or 0)
    decision_input = _number(value.get("decision_input_tokens"))
    decision = {key: int(_number(value.get(key)) or 0) for key in _DECISION_COUNTS}
    if "decision_unknown_failures" not in value:
        decision["decision_unknown_failures"] = decision["decision_failure_count"]
    return {
        "schema": _SCHEMA,
        **{key: int(_number(value.get(key)) or 0) for key in _COUNTS},
        "tool_count": int(_number(value.get("tool_count"))) if _number(value.get("tool_count")) is not None else None,
        "cache_percent": min(100.0, _number(value.get("cache_percent"))) if _number(value.get("cache_percent")) is not None else None,
        # 会话累计命中率 = 累计缓存命中输入 ÷ 累计供应商输入（DeepSeek Harness 页脚同一口径）；没有输入时为 None。
        "cache_percent_session": _session_cache_percent(value),
        "output_tps": _number(value.get("output_tps")),
        "pending": value.get("pending") is True,
        "totals_known": value.get("totals_known") is True,
        **({"cache_diagnostic": diagnostic} if diagnostic else {}),
        **({"cache_change_counts": change_counts} if change_counts else {}),
        **({"cache_counted_calls": counted_calls} if counted_calls else {}),
        **({"decision_call_count": decision_calls,
            "decision_input_tokens": int(decision_input) if decision_input is not None else None,
            **decision}
           if decision_calls else {}),
    }


# LLM: 最近一次调用的命中率会被压缩前后的两次调用拉得很低（压缩请求、压缩后第一次都是新前缀），TUI 只看它会误判；
#   累计值按账本的供应商回报数算，缓存读入超过输入时夹到 100。
# 函数用途: 从统计快照算会话累计缓存命中百分比；没有供应商输入时返回 None。
def _session_cache_percent(value: Mapping[str, object]) -> float | None:
    total_input = _number(value.get("input_tokens"))
    cached = _number(value.get("cache_read_input_tokens"))
    reported = _number(value.get("cache_read_reported_calls"))
    if not total_input or total_input <= 0 or cached is None or not reported:
        return None
    return min(100.0, 100.0 * cached / total_input)


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
    totals = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_read_reported_calls": 0,
              "estimated_tokens": 0, "totals_known": True,
              "failure_count": 0, "unfinished_calls": 0, "unfinished_tokens": 0, "unknown_failures": 0,
              "decision_call_count": 0, "decision_input_tokens": None, **dict.fromkeys(_DECISION_COUNTS, 0)}
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


# LLM: 读累计摘要或 model_usage 增量行里一个用途分区的失败构成，不改账。新账 estimated 带 unfinished_call_count 键：
#   失败里有 HTTP 尝试的有本地估算；旧账没有该键，分不清失败的调用是否发出，整体记为 unknown。TUI 统计行与 audit_records 共用。
# 函数用途: 从一个用途分区摘要取出失败次数、已发出未完成的次数与估算输入、以及分不清是否发出的失败次数。
def unfinished_usage_facts(purpose_summary: Mapping[str, object]) -> dict[str, int]:
    statuses = purpose_summary.get("status_counts") or {}
    failures = sum(max(0, int(statuses.get(key) or 0)) for key in ("failed", "timed_out"))
    estimated = (purpose_summary.get("usage_breakdown") or {}).get("estimated") or {}
    if "unfinished_call_count" not in estimated:
        return {"failures": failures, "unfinished_calls": 0, "unfinished_tokens": 0, "unknown_failures": failures}
    return {"failures": failures, "unfinished_calls": max(0, int(estimated.get("unfinished_call_count") or 0)),
            "unfinished_tokens": max(0, int(estimated.get("unfinished_input_tokens") or 0)), "unknown_failures": 0}


# LLM: 入参是跨事件累加后的原始次数（迟到的尝试可能落在后一条事件里，逐条算会错）。没发出去 = 失败 − 分不清的 − 已发出未完成的，
#   即准入忙、发送前期限用完等一次 HTTP 尝试都没有的调用，不计入 Jev 失败率。只用于展示与汇总，TUI 与 audit_records 共用。
# 函数用途: 把累计失败次数拆成“Jev 失败（发出去后失败/超时）”和“没发出去”两类。
def split_unsent_failures(failures: int, unfinished_calls: int, unknown_failures: int) -> tuple[int, int]:
    not_sent = max(0, int(failures) - int(unknown_failures) - int(unfinished_calls))
    return max(0, int(failures) - not_sent), not_sent


# LLM: provider input 已含缓存读写，不再加缓存；决策只是原总量子集，逐字段缺报不能由信封计数猜测。
#   成功/失败取决策分区 status_counts（finished / failed + timed_out），进行中的调用两边都不计；失败只累加原始次数，
#   “发出去后失败 / 没发出去”由 split_unsent_failures 在展示时推导。全部用途的失败构成用同一个 unfinished_usage_facts
#   累加（没有用途分区的旧账也有根摘要），展示时减去决策分区就是非决策调用，不另写口径。
# 函数用途: 汇总原 token 并附加全部用途与决策的失败构成、成败次数与未完成估算；旧账没有用途分区时不计入决策，也不推断为零。
def _add_usage(totals: dict[str, object], summary: Mapping[str, object]) -> None:
    breakdown = summary.get("usage_breakdown")
    breakdown = breakdown if isinstance(breakdown, Mapping) else {}
    provider, estimated = breakdown.get("provider", {}), breakdown.get("estimated", {})
    for key in ("input_tokens", "output_tokens", "cache_read_input_tokens"):
        totals[key] += int(provider.get(key) or 0)
    totals["cache_read_reported_calls"] += int(provider.get("cache_read_input_tokens_reported_call_count") or 0)
    totals["estimated_tokens"] += sum(int(estimated.get(key) or 0) for key in ("input_tokens", "output_tokens"))
    overall = unfinished_usage_facts(summary)
    totals["failure_count"] += overall["failures"]
    totals["unfinished_calls"] += overall["unfinished_calls"]
    totals["unfinished_tokens"] += overall["unfinished_tokens"]
    totals["unknown_failures"] += overall["unknown_failures"]
    purposes = summary.get("purpose_breakdown")
    if not isinstance(purposes, Mapping):
        return
    decision = purposes.get("decision", {})
    usage = decision.get("usage_breakdown", {}).get("provider", {})
    reported = int(usage.get("input_tokens_reported_call_count") or 0)
    outcomes, facts = decision.get("status_counts") or {}, unfinished_usage_facts(decision)
    totals["decision_call_count"] += int(decision.get("physical_model_attempt_count") or 0)
    totals["decision_input_reported_calls"] += reported
    totals["decision_success_count"] += int(outcomes.get("finished") or 0)
    totals["decision_failure_count"] += facts["failures"]
    totals["decision_unfinished_calls"] += facts["unfinished_calls"]
    totals["decision_estimated_tokens"] += facts["unfinished_tokens"]
    totals["decision_unknown_failures"] += facts["unknown_failures"]
    if reported:
        totals["decision_input_tokens"] = int(totals["decision_input_tokens"] or 0) + int(usage.get("input_tokens") or 0)


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
# 显示写入走 model_usage 的原线程更新能力；缓存累计与去重身份选同一较新快照，不能拼接不同来源。
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
    store = getattr(agent, "conversation_store", None)
    # 整份选较新快照，基数与身份同源；持久较新时不能取旧内存身份再对新基数重复加一。
    persisted = model_metrics_from_thread(store, thread_id)
    state_previous = state.get("_model_metrics_current", {}) if isinstance(state, dict) else {}
    previous = newer_model_metrics(state_previous, persisted)
    totals = _previous_totals(agent, params, thread_id, str(summary.get("usage_scope_id") or ""))
    _add_usage(totals, summary)
    payload = {**previous, **totals, "schema": _SCHEMA, "sampled_at_ns": time.time_ns()}
    if not usage_only:
        decision = (summary.get("purpose_breakdown") or {}).get("decision", {})
        payload.update(model_rounds=int(summary.get("logical_model_turn_count") or 0) - int(decision.get("logical_model_turn_count") or 0),
            retry_count=sum(int(summary.get(key) or 0) - int(decision.get(key) or 0) for key in ("model_retry_count", "provider_http_retry_count")),
            tool_count=tool_count, pending=pending)
    fresh = response is not None and not usage_only
    fresh_diagnostic = _merge_response_metrics(payload, response, ledger, call_id) if fresh else None
    # 同一快照里的累计基数和身份一起更新，随后一起清洗并通过原线程保存入口持久化。
    payload.update(_cache_change_counts_for_publish(previous, ledger, call_id if fresh else "", fresh_diagnostic))
    public = public_model_metrics(payload)
    if isinstance(state, dict):
        state["_model_metrics_current"] = public
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
