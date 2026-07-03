"""watch_stream 的「学判据」层:sample(抓样本+字段分布)与 configure(灌判据 spec)。

learn → configure → monitor 里,learn 由模型干:sample 给它原始样本(截断)+ 全样本
纯计数的字段分布(每字段 distinct/top 取值/示例),模型据此判断结果端字段、目标/常态
取值、噪声字段,产出结构化 spec;configure 校验后灌进引擎并随 watch 持久化。
本层代码零语义判断:字段分布只是计数,spec 校验只是形状检查(铁律)。
"""

from __future__ import annotations

import json
import time
from typing import Any

from ..tooling.models import ToolExecutionResult
from .puller import DrainBudget, drain_source
from .source_spec import SourceSpec, canon_value, parse_source_spec
from .text_tokens import head_token
from .watch_state import WatchState, persist_state

_TOOL_NAME = "watch_stream"
_SAMPLE_DEFAULT = 200
_SAMPLE_MIN = 20
_SAMPLE_MAX = 1000
_SAMPLE_FETCH_SECONDS = 15
_RAW_EVENTS_SHOWN = 20
_RAW_EVENT_JSON_CAP = 1200
_TOP_VALUES_SHOWN = 8
_DISTINCT_TRACK_CAP = 48
_VALUE_DISPLAY_CAP = 80
_DIGEST_PATHS_CAP = 96

SAMPLE_GUIDANCE = (
    "这是源的原始样本(截断展示)与【全样本纯计数】的字段分布(代码不判语义,判断在你)。"
    "接下来由你学出这个源的判据,action=configure 提交结构化 spec:"
    "1) result_field=哪个字段是【结果/结论端】(常为低基数枚举/布尔,或含结论记号的文本;"
    "触发/输入端字段不算);2) 取值判据【默认学常态、盯常态之外】——样本几乎全是正常流水,"
    "真目标稀疏到样本里通常一条都没有;所以【别从样本可见取值里挑最像异常的那个当 "
    "target】,样本里出现的取值(哪怕只出现一两次)几乎全是常态。做法:认出结果端字段后看它"
    "的取值集合,把这些正常/完成/成功/已处理类记号【全部】列进常态——干净枚举/布尔用 "
    "normal_values 列全,变尾文本(每条尾巴不同)用 normal_value_contains 列全常态结论记号,"
    "引擎抬任何样本里没见过的新记号;只有当任务描述/源信封【明确点名】了目标记号、或样本里"
    "确实存在一眼可辨的失败/异常/丢失类取值时,才改用 target_values/target_value_contains 点名。"
    "3) ignore_fields=高基数噪声字段(distinct 接近样本数、"
    "几乎每条都不同,如随机串/随机 ID),列进去让签名统计不被噪声淹。"
    "布尔/数值在 spec 里写成字符串:true/false/null/整数字面。"
    "配好后引擎按判据抬候选(triage.reason=spec_target_value),通用稀有度兜底仍在。"
    "【判据只是引擎侧宽筛器】:它决定引擎多抬什么,不决定真假——每条候选仍要你"
    "逐条重判(看两端字段+源信封判据说明)才算数;samples 里高频出现的取值配成 "
    "target 会被拒(高频≈常态,系统按样本频次结构化校验)。"
)

CONFIGURE_GUIDANCE = (
    "判据 spec 已灌入引擎并随本 watch 持久化(重启/补岗自动生效);引擎已按新字段集重置"
    "画像并将重新预热。现在开始 pull 长轮询盯守。【判据只是宽筛,真假在重判】每条候选"
    "仍要你独立看两端字段定性,判真才入账上报;triage 里的取值窗口频次是重判证据。"
    "【自查】若 pull 后候选(reason=spec_target_value)几乎条条看着都正常/成功/已处理,"
    "或命中取值的 value_window_count 很大(窗口内高频≈常态),说明判据把常态当成了目标"
    "(target 配反),立即重新 sample+configure 改用 normal_values/normal_value_contains "
    "列全常态、盯常态之外;反之长期零候选也重学。格式漂移同理。"
)


def sample_source(fetch_json, state: WatchState, params: dict[str, Any]) -> ToolExecutionResult:
    """抓一批原始样本(从源滚动缓冲最旧处顺读,不动盯守游标),给模型学判据。
    每字段取值分布同时缓存进 watch 状态(configure 校验 target 频次的样本证据)。"""
    count = _sample_count(params)
    budget = DrainBudget(
        max_events=count,
        page_limit=min(state.tuning.page_limit, count),
        deadline=time.time() + _SAMPLE_FETCH_SECONDS,
    )
    drain = drain_source(fetch_json, state.source_url, 0, budget)
    if drain.error and not drain.events:
        return _err(f"取样失败: {drain.error}", drain.error_code or "NETWORK_REQUEST_FAILED")
    events = [event for _seq, event in drain.events]
    stats = _field_stats(events)
    with state.lock:
        state.last_sample_digest = _sample_cache(len(events), stats)
        persist_state(state)
    payload = {
        "ok": True,
        "action": "sample",
        "watch_id": state.watch_id,
        "sampled_events": len(events),
        "source_envelope": dict(state.source_envelope),
        "current_spec": dict(state.source_spec) if state.source_spec else None,
        "raw_events": [_capped_event(event) for event in events[:_RAW_EVENTS_SHOWN]],
        "field_digest": _field_digest(stats),
        "guidance": SAMPLE_GUIDANCE,
    }
    return _ok(payload)


def configure_spec(state: WatchState, params: dict[str, Any]) -> ToolExecutionResult:
    """校验并灌入模型学出的判据 spec:引擎立即按 spec 盯,spec 随 watch 持久化。"""
    raw = params.get("spec")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            return _err(f"spec 不是合法 JSON: {exc}", "TOOL_INVALID_ARGUMENTS")
    try:
        spec = parse_source_spec(raw)
    except ValueError as exc:
        return _err(f"spec 不合法: {exc}", "TOOL_INVALID_ARGUMENTS")
    frequency_error = _high_frequency_target_error(state, spec)
    if frequency_error:
        return _err(frequency_error, "TOOL_INVALID_ARGUMENTS")
    with state.lock:
        state.source_spec = spec.to_payload()
        state.engine.apply_spec(spec)
        persist_state(state)
    return _ok(
        {
            "ok": True,
            "action": "configure",
            "watch_id": state.watch_id,
            "spec": dict(state.source_spec),
            "guidance": CONFIGURE_GUIDANCE,
        }
    )


def _field_stats(events: list[dict]) -> dict[str, dict[str, Any]]:
    """全样本每字段的纯计数分布(展示与缓存共用同一份账)。只数不判。"""
    from .flatten import flatten_event

    stats: dict[str, dict[str, Any]] = {}
    for event in events:
        for path, value in flatten_event(event):
            _observe_field(stats, path, canon_value(value))
    return stats


def _field_digest(stats: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """分布的模型可读视图:出现数/去重数/top 取值/示例。"""
    digest = {path: _field_row(row) for path, row in sorted(stats.items())[:_DIGEST_PATHS_CAP]}
    if len(stats) > _DIGEST_PATHS_CAP:
        digest["…"] = f"+{len(stats) - _DIGEST_PATHS_CAP} fields"
    return digest


def _sample_cache(sampled_events: int, stats: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """分布的持久化缓存态(configure 校验 target 频次用):每字段出现数+完整取值计数。"""
    fields = {
        path: {"events": row["events"], "values": dict(row["values"])}
        for path, row in sorted(stats.items())[:_DIGEST_PATHS_CAP]
    }
    return {"sampled_events": sampled_events, "sampled_at": round(time.time(), 3), "fields": fields}


# target 频次拒错闸(§7.1):样本证据下,配为 target 的取值出现 >= 次数且 >= 占比即拒。
_TARGET_SAMPLE_MIN_COUNT = 3
_TARGET_SAMPLE_MIN_PCT = 2.0


def _high_frequency_target_error(state: WatchState, spec: SourceSpec) -> str:
    """结构化拒配"高频 target"(真机实锤:样本里没有真目标时,模型会把某个常态取值配成
    target → 引擎照抬、逐条报出的全是常态误报)。目标是稀疏的:配为 target 的取值若在
    最近样本里高频出现,它几乎必是常态。纯计数比对,零语义;没 sample 过 / 样本没看到
    结果端字段 → 无证据不拒(任务/源信封明确点名 target 的合法场景)。"""
    if not spec.result_field or not (spec.target_values or spec.target_value_contains):
        return ""
    field = dict((state.last_sample_digest.get("fields") or {}).get(spec.result_field) or {})
    values = {str(k): int(v) for k, v in dict(field.get("values") or {}).items()}
    field_events = int(field.get("events") or 0)
    if not values or field_events <= 0:
        return ""
    heads: dict[str, int] = {}
    for value, count in values.items():
        head = head_token(value)
        if head:
            heads[head] = heads.get(head, 0) + count
    for target in sorted(spec.target_values):
        count = max(values.get(target, 0), heads.get(target, 0))
        message = _frequency_verdict(target, count, field_events, spec.result_field)
        if message:
            return message
    for token in spec.target_value_contains:
        count = sum(c for v, c in values.items() if token in v)
        message = _frequency_verdict(token, count, field_events, spec.result_field)
        if message:
            return message
    return ""


def _frequency_verdict(target: str, count: int, field_events: int, field: str) -> str:
    pct = 100.0 * count / field_events
    if count < _TARGET_SAMPLE_MIN_COUNT or pct < _TARGET_SAMPLE_MIN_PCT:
        return ""
    return (
        f"spec 被拒:配为 target 的取值 '{target}' 在最近样本的 {field} 字段里出现 "
        f"{count}/{field_events} 次(≈{pct:.1f}%)。目标应是稀疏的——样本里高频出现的取值"
        "几乎必是常态,把它配成 target 会让逐条上报全是误报。改法:把它和其余常见取值一起"
        "列进 normal_values / normal_value_contains(盯常态之外);只有样本里稀有或根本"
        "没出现、且任务/源信封明确点名的取值才配 target。"
    )


def _observe_field(stats: dict[str, dict[str, Any]], path: str, canon: str) -> None:
    row = stats.setdefault(path, {"events": 0, "values": {}, "overflow": False, "example": canon})
    row["events"] += 1
    values = row["values"]
    shown = canon[:_VALUE_DISPLAY_CAP]
    if shown in values:
        values[shown] += 1
    elif len(values) < _DISTINCT_TRACK_CAP:
        values[shown] = 1
    else:
        row["overflow"] = True


def _field_row(row: dict[str, Any]) -> dict[str, Any]:
    values: dict[str, int] = row["values"]
    top = sorted(values.items(), key=lambda kv: (-kv[1], kv[0]))[:_TOP_VALUES_SHOWN]
    distinct = f"{_DISTINCT_TRACK_CAP}+" if row["overflow"] else len(values)
    return {
        "events": row["events"],
        "distinct_values": distinct,
        "top_values": [[value, count] for value, count in top],
        "example": row["example"][:_VALUE_DISPLAY_CAP],
    }


def _capped_event(event: dict) -> Any:
    text = json.dumps(event, ensure_ascii=False, sort_keys=True)
    if len(text) <= _RAW_EVENT_JSON_CAP:
        return event
    return {"__truncated__": text[:_RAW_EVENT_JSON_CAP]}


def _sample_count(params: dict[str, Any]) -> int:
    try:
        parsed = int(str(params.get("sample_count") or _SAMPLE_DEFAULT).strip())
    except (TypeError, ValueError):
        return _SAMPLE_DEFAULT
    return max(_SAMPLE_MIN, min(_SAMPLE_MAX, parsed))


def _ok(payload: dict[str, Any]) -> ToolExecutionResult:
    return ToolExecutionResult(_TOOL_NAME, True, json.dumps(payload, ensure_ascii=False), result_envelope=payload)


def _err(message: str, code: str) -> ToolExecutionResult:
    return ToolExecutionResult(
        _TOOL_NAME, False, json.dumps({"ok": False, "error": message}, ensure_ascii=False), error_code=code
    )


__all__ = ["CONFIGURE_GUIDANCE", "SAMPLE_GUIDANCE", "configure_spec", "sample_source"]
