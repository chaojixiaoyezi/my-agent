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

from ..tooling.models import ToolHandlerOutcome
from .puller import DrainBudget, drain_source
from .source_http import public_source_envelope
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
    "raw_events 是只读样本，field_digest 是纯结构统计；程序不解释业务含义。"
    "如用户目标适合结构化候选缩减，可自行用 action=configure 保存字段与取值提示；"
    "如结构不足以区分记录，可使用 passthrough。spec 只影响展示和排序，不产生业务结论。"
)

CONFIGURE_GUIDANCE = (
    "结构化 spec 已随本 watch 持久化。它只影响候选展示和统计，不解释记录含义、"
    "不决定结论、复核、委派或汇报。可继续 pull，也可根据用户目标和后续证据修改或移除 spec。"
)

AUDIT_SAMPLE_GUIDANCE = (
    "这是对当前 Audit 来源的一次只读结构检查，不推进正式游标。原始任务、判断标准、"
    "评分方法和汇报要求仍以本次命名 Audit 的用户目标为准；不要为 Audit 创建或保存"
    "来源业务判据，后续直接领取 durable 批次并自主分析。"
)


def sample_source(fetch_json, state: WatchState, params: dict[str, Any]) -> ToolHandlerOutcome:
    """抓一批原始样本(从源滚动缓冲最旧处顺读,不动盯守游标),给模型学判据。
    每字段取值分布同时缓存进 watch 状态(configure 校验 target 频次的样本证据)。
    file 源=读文件头 N 行;poll 源=原样查一次接口包成单条样本;cursor 源=翻页顺读。"""
    count = _sample_count(params)
    events, error, error_code = _sample_events(fetch_json, state, count)
    if error and not events:
        return _err(f"取样失败: {error}", error_code or "NETWORK_REQUEST_FAILED")
    stats = _field_stats(events)
    if not state.audit_guarantee:
        with state.lock:
            state.last_sample_digest = _sample_cache(len(events), stats)
            persist_state(state)
    payload = {
        "ok": True,
        "action": "sample",
        "watch_id": state.watch_id,
        "sampled_events": len(events),
        "source_envelope": public_source_envelope(state.source_envelope),
        "current_spec": (
            None
            if state.audit_guarantee
            else (dict(state.source_spec) if state.source_spec else None)
        ),
        "raw_events": [_capped_event(event) for event in events[:_RAW_EVENTS_SHOWN]],
        "field_digest": _field_digest(stats),
        "guidance": AUDIT_SAMPLE_GUIDANCE if state.audit_guarantee else SAMPLE_GUIDANCE,
    }
    if not state.audit_guarantee and state.judgment_note:
        payload["judgment_note"] = state.judgment_note
    return _ok(payload)


def _sample_events(fetch_json, state: WatchState, count: int) -> tuple[list[dict], str, str]:
    """按源类型取样;返回 (events, error, error_code)。"""
    from .sources import (
        drain_poll_source,
        file_path_of,
        file_record_delimiter,
        sample_file_lines,
        source_kind,
    )

    kind = source_kind(state.source_url, state.source_mode)
    if kind == "file":
        events, error = sample_file_lines(
            file_path_of(state.source_url),
            count,
            delimiter=file_record_delimiter(state),
        )
        return events, error, "NETWORK_REQUEST_FAILED" if error else ""
    if kind == "poll":
        # 快照接口:样本=当下这一份响应(节拍闸不拦 sample,学判据要现货)。
        drain = drain_poll_source(
            fetch_json,
            state.source_url,
            0,
            due=True,
            source_envelope=dict(state.source_envelope),
        )
        return [event for _seq, event in drain.events], drain.error, drain.error_code
    budget = DrainBudget(
        max_events=count,
        page_limit=min(state.tuning.page_limit, count),
        deadline=time.time() + _SAMPLE_FETCH_SECONDS,
    )
    drain = drain_source(
        fetch_json,
        state.source_url,
        0,
        budget,
        source_envelope=(
            dict(state.source_envelope)
            if isinstance(state.source_envelope, dict)
            else None
        ),
    )
    return [event for _seq, event in drain.events], drain.error, drain.error_code


_JUDGMENT_NOTE_CAP = 2000


def configure_spec(state: WatchState, params: dict[str, Any]) -> ToolHandlerOutcome:
    """校验并灌入模型学出的判据 spec:引擎立即按 spec 盯,spec 随 watch 持久化。
    judgment_note(可选)= 轻量记忆:用户教的"这个来源/这类事怎么看"原文(样品说明/
    判据描述),随 watch 持久化,重启或消费者切换后都在载荷里原样带回——教一次别重教。
    只给 judgment_note 不给 spec 也行(有的源不需要结构化判据,只需要判读须知)。"""
    if state.audit_guarantee:
        return _err(
            "/audit 的判断标准属于命名任务目标，不能保存为来源筛选 spec 或 judgment_note",
            "TOOL_INVALID_ARGUMENTS",
        )
    note = params.get("judgment_note")
    raw = params.get("spec")
    if raw is None and note is None:
        return _err("configure 至少给 spec 或 judgment_note 之一", "TOOL_PARAMETER_REQUIRED")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            return _err(f"spec 不是合法 JSON: {exc}", "TOOL_INVALID_ARGUMENTS")
    spec = None
    if raw is not None:
        try:
            spec = parse_source_spec(raw)
        except ValueError as exc:
            return _err(f"spec 不合法: {exc}", "TOOL_INVALID_ARGUMENTS")
        frequency_error = _high_frequency_target_error(state, spec)
        if frequency_error:
            return _err(frequency_error, "TOOL_INVALID_ARGUMENTS")
    with state.lock:
        if spec is not None:
            state.source_spec = spec.to_payload()
            state.engine.apply_spec(spec)
        if note is not None:
            state.judgment_note = str(note).strip()[:_JUDGMENT_NOTE_CAP]
        persist_state(state)
    payload = {
        "ok": True,
        "action": "configure",
        "watch_id": state.watch_id,
        "spec": dict(state.source_spec) if state.source_spec else None,
        "guidance": CONFIGURE_GUIDANCE,
    }
    if state.judgment_note:
        payload["judgment_note"] = state.judgment_note
        payload["judgment_note_persisted"] = (
            "判读须知已随本 watch 持久化:重启或消费者切换时会在 open/sample/pull "
            "载荷里原样带回,不用让用户重教。"
        )
    return _ok(payload)


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


def _ok(payload: dict[str, Any]) -> ToolHandlerOutcome:
    return ToolHandlerOutcome(_TOOL_NAME, True, json.dumps(payload, ensure_ascii=False), result_envelope=payload)


def _err(message: str, code: str) -> ToolHandlerOutcome:
    return ToolHandlerOutcome(
        _TOOL_NAME, False, json.dumps({"ok": False, "error": message}, ensure_ascii=False), error_code=code
    )


__all__ = ["CONFIGURE_GUIDANCE", "SAMPLE_GUIDANCE", "configure_spec", "sample_source"]
