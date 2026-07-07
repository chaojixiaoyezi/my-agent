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
    "触发/输入端字段不算);"
    "2) 取值判据【先看结果端字段基数(field_digest 的 distinct_values),按基数分两条路,别一律学常态】:"
    "●【低基数枚举/布尔】(distinct 就几种,如 flag/state):学常态、盯常态之外——样本几乎全是正常"
    "流水、真目标稀疏到样本里通常一条都没有,别从样本挑 target;把正常/完成/成功/已处理类记号【全部】"
    "列进 normal_values(枚举/布尔列全)、变尾文本用 normal_value_contains 列全常态结论记号,引擎抬"
    "任何样本里没见过的新记号。"
    "●【高基数文本消息】(distinct 接近样本数、每条正文都不同,如 HTTP 响应/日志正文):【别用 "
    "normal_value_contains 学常态盯「常态之外」——文本正文的常态无穷、列不全,盯「常态之外」会把大量"
    "正常业务响应也整批抬成候选(escalated 洪泛)、判读被淹、真目标反而沉底漏掉。改学【目标得逞/命中"
    "的结论记号】配 target_value_contains:结果端表示「目标真发生了」的短语记号(盯什么、什么得逞就是"
    "目标——命令回显、绕过成功、数据外泄、越权拿到的敏感字段名等),有限且明确,可从任务语义(盯的"
    "就是它得逞)+ 样本偶见的异常结论推断;引擎只精准抬命中这些记号的(不淹判读),且 target 命中优先"
    "于任何 normal 规则(带中性遥测/日志前缀的目标也不被 normal 误压)。此路【别把出现在目标结果里的"
    "中性词(遥测/日志前缀等目标与常态两端都有的记号)列进 normal_*——中性词会连目标一起压】。"
    "3) ignore_fields=高基数噪声字段(distinct 接近样本数、"
    "几乎每条都不同,如随机串/随机 ID),列进去让签名统计不被噪声淹。"
    "布尔/数值在 spec 里写成字符串:true/false/null/整数字面。"
    "配好后引擎按判据抬候选(triage.reason=spec_target_value),通用稀有度兜底仍在。"
    "【判据只是引擎侧宽筛器】:它决定引擎多抬什么,不决定真假——每条候选仍要你"
    "逐条重判(看两端字段+源信封判据说明)才算数;samples 里高频出现的取值配成 "
    "target 会被频次证据拦下并给出按内容改配的路(频率只触发再研判,不定真假)。"
)

CONFIGURE_GUIDANCE = (
    "判据 spec 已灌入引擎并随本 watch 持久化(重启/补岗自动生效);只改取值判据"
    "(增删 normal_*/target_* 内容规则)不重置统计,ignore_fields 变了才重置签名画像"
    "并重新预热——按告警补规则是常规动作,放心随时 configure。现在开始 pull 长轮询盯守。"
    "【判据只是宽筛,真假在重判】每条候选仍要你独立看两端字段定性,判真才入账上报;"
    "triage 里的取值窗口频次是重判证据。【自查】pull 后候选(reason=spec_target_value)几乎"
    "条条正常/成功、没有你要盯的目标迹象:①结果端低基数、你配了 target_* → 多半 target 配反"
    "(把常态当目标),改用 normal_values 列全常态、盯常态之外;②结果端高基数文本、你配了 "
    "normal_value_contains 盯「常态之外」、候选还量大(escalated 高) → 是常态列不全导致 "
    "outside_normal 洪泛(正常业务也被抬淹判读),改用 target_value_contains 学【目标得逞的结论"
    "记号】精准抬。核实候选确是目标(真事也可能高发)就照常逐条上报别停;长期零候选也重学。"
    "格式漂移同理。"
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
    target → 引擎照抬、逐条报出的全是常态误报)。目标是稀疏的:配为 target 的取值若高频
    出现,它几乎必是常态。纯计数比对,零语义。两路证据都查(有一路命中即拒):
    ① 最近样本(§7.1):sample 缓存的字段取值分布;
    ② 盯守窗口(§11.2):引擎历轮 pull 攒的取值窗口频次——治样本没看到/不具代表性、但
       历轮 pull 已明确显示该取值是常态高频的漏配(尤其"配反 → 洪泛误报 → 重 configure
       仍配同一个"的复发环)。两路都【无证据不拒】(没 sample / 字段没进过流 / 窗口没热身
       够引擎判据支持度),守"宁可漏拦不误杀"。"""
    if not spec.result_field or not (spec.target_values or spec.target_value_contains):
        return ""
    return _sample_high_frequency_error(state, spec) or _window_high_frequency_error(state, spec)


def _sample_high_frequency_error(state: WatchState, spec: SourceSpec) -> str:
    """① 最近样本证据(§7.1 原有):sample 缓存里 target 高频出现 → 拒。没 sample 过 / 样本
    没看到结果端字段 → 无证据不拒。"""
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
    location = f"最近样本的 {spec.result_field} 字段"
    for target in sorted(spec.target_values):
        count = max(values.get(target, 0), heads.get(target, 0))
        message = _frequency_verdict(target, count, field_events, location)
        if message:
            return message
    for token in spec.target_value_contains:
        count = sum(c for v, c in values.items() if token in v)
        message = _frequency_verdict(token, count, field_events, location)
        if message:
            return message
    return ""


def _window_high_frequency_error(state: WatchState, spec: SourceSpec) -> str:
    """② 盯守窗口证据(§11.2):引擎历轮 pull 攒的取值窗口频次里 exact target 高频 → 拒。
    只查 exact target_values(窗口按取值 token 计数,不跟踪 contains 子串);窗口字段样本量
    未达引擎自己的判据支持度(value_min_support)→ 不拒(窗口没热身够、pct 噪声大,宁可漏
    拦);零 pull / 字段没进过流 → value_window_counts 返 (0,0) → 不拒。纯回查零副作用。"""
    engine = getattr(state, "engine", None)
    if engine is None or not spec.target_values:
        return ""
    now = time.time()
    min_support = int(getattr(getattr(engine, "tuning", None), "value_min_support", 0) or 0)
    field_floor = max(min_support, _TARGET_SAMPLE_MIN_COUNT)
    location = f"盯守窗口的 {spec.result_field} 字段"
    for target in sorted(spec.target_values):
        value_count, field_count = engine.value_window_counts(spec.result_field, target, now)
        if field_count < field_floor:
            continue
        message = _frequency_verdict(target, value_count, field_count, location)
        if message:
            return message
    return ""


def _frequency_verdict(target: str, count: int, field_events: int, location: str) -> str:
    """频次裁决:命中取值在 location(最近样本/盯守窗口的某字段)出现 >= 次数且 >= 占比即拒。
    拒的是【这份判据配置】不是事件(事件层零按频丢弃);改法必须按内容再研判,绝不能
    教模型把疑似目标直接标成常态(真事也可能高频,标进 normal 就整类漏死)。"""
    if field_events <= 0:
        return ""
    pct = 100.0 * count / field_events
    if count < _TARGET_SAMPLE_MIN_COUNT or pct < _TARGET_SAMPLE_MIN_PCT:
        return ""
    return (
        f"spec 被拒:配为 target 的取值 '{target}' 在{location}里出现 "
        f"{count}/{field_events} 次(≈{pct:.1f}%)。频率不定真假,但按这份证据它更可能是"
        "常态流水——直接配 target 会让引擎照判据整批抬常态、逐条上报全是误报。"
        "先按内容再研判这一类(看几条带它的原始事件:触发端+结果端,对照任务/源信封):"
        "①确认是常态 → 把它和其余常见取值列进 normal_values / normal_value_contains"
        "(建内容过滤规则),盯常态之外;②确认它就是任务点名的目标(真事也可能高发)→ "
        "【别把它列进 normal_*】,改用 normal_values / normal_value_contains 列全【其余】"
        "常态取值、留它在常态之外——引擎会把它照常逐条抬升(车道内稀有优先、高频沉底,"
        "真事频繁也照报,量大另有调查告警提醒复核)。"
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
