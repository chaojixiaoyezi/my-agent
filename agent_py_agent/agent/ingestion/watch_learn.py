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
from .source_spec import canon_value, parse_source_spec
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
    "配好后引擎按判据精准抬候选(triage.reason=spec_target_value),通用稀有度兜底仍在。"
)

CONFIGURE_GUIDANCE = (
    "判据 spec 已灌入引擎并随本 watch 持久化(重启/补岗自动生效);引擎已按新字段集重置"
    "画像并将重新预热。现在开始 pull 长轮询盯守。【自查】若 pull 后候选(reason="
    "spec_target_value)几乎条条看着都正常/成功/已处理,说明判据把常态当成了目标(target 配反),"
    "立即重新 sample+configure 改用 normal_values/normal_value_contains 列全常态、盯常态之外;"
    "反之长期零候选也重学。格式漂移同理。"
)


def sample_source(fetch_json, state: WatchState, params: dict[str, Any]) -> ToolExecutionResult:
    """抓一批原始样本(从源滚动缓冲最旧处顺读,不动盯守游标),给模型学判据。"""
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
    payload = {
        "ok": True,
        "action": "sample",
        "watch_id": state.watch_id,
        "sampled_events": len(events),
        "source_envelope": dict(state.source_envelope),
        "current_spec": dict(state.source_spec) if state.source_spec else None,
        "raw_events": [_capped_event(event) for event in events[:_RAW_EVENTS_SHOWN]],
        "field_digest": _field_digest(events),
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


def _field_digest(events: list[dict]) -> dict[str, Any]:
    """全样本每字段的纯计数分布:出现数/去重数/top 取值/示例。只数不判。"""
    from .flatten import flatten_event

    stats: dict[str, dict[str, Any]] = {}
    for event in events:
        for path, value in flatten_event(event):
            _observe_field(stats, path, canon_value(value))
    digest = {path: _field_row(row) for path, row in sorted(stats.items())[:_DIGEST_PATHS_CAP]}
    if len(stats) > _DIGEST_PATHS_CAP:
        digest["…"] = f"+{len(stats) - _DIGEST_PATHS_CAP} fields"
    return digest


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
