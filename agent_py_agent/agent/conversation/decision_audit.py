# LLM: 决策审计只读三类权威结构化事实：①决策设置读取（有效开关、各点模式、等待时间）；②各会话 model_usage 账本的
#   decision 用途分区（调用次数、finished/failed/timed_out、已报输入 token）；③Gateway 请求记录里的决策观察（经宿主写入器）。
#   不 grep 日志、不读会话/记忆正文、不按文本推断；线程集合与 owner 身份由调用方按已认证范围解析后传入。只读，不写文件。
#   改字段须同步 audit_records 工具、docs/design/DECISION_AUDIT_AND_ADMIN_CONTROLS.md 与 test_decision_audit_controls。
# 模块用途: 汇总一个 owner 在时间窗内的决策使用情况，供审计工具输出。
from __future__ import annotations

from collections.abc import Mapping

# 每个 owner 最多展示这么多条按调用次数排序的会话明细
_THREAD_ROWS = 10
_STATUS_KEYS = ("finished", "failed", "timed_out")


# LLM: 只投影有效值里的开关、等待时间与各点有效模式，不输出模型编号以外的连接细节；读失败交给调用方记录。
# 函数用途: 生成某个 owner（或当前会话）的决策设置摘要。
def decision_settings_summary(host: object, thread_id: str = "") -> dict:
    from ..settings.decision_settings import execute_decision_settings_operation

    report = execute_decision_settings_operation(host, "read", {}, thread_id=thread_id, blocking=False)
    effective = report["effective"]
    return {
        "scope": report["scope"],
        **{key: effective[key] for key in ("enabled", "experiment_enabled", "timeout_seconds",
                                            "stage_timeout_seconds", "background_timeout_seconds")},
        "profile_bound": bool(effective.get("profile_id")),
        "points": {point: row["effective_mode"] for point, row in effective["points"].items()},
    }


# LLM: 只读 purpose_breakdown.decision 的计数字段；没有用途分区的旧事件单独计数，不当作零决策也不猜用途。
# 函数用途: 把一条用量事件里的决策分区累加到计数行上；返回该事件是否是没有用途分区的旧账。
def _add_event(row: dict, model_calls: Mapping) -> bool:
    purposes = model_calls.get("purpose_breakdown")
    if not isinstance(purposes, Mapping):
        return bool(model_calls.get("physical_model_attempt_count"))
    decision = purposes.get("decision") or {}
    statuses = decision.get("status_counts") or {}
    provider = (decision.get("usage_breakdown") or {}).get("provider") or {}
    row["calls"] += int(decision.get("physical_model_attempt_count") or 0)
    for key in _STATUS_KEYS:
        row[key] += int(statuses.get(key) or 0)
    row["input_reported_calls"] += int(provider.get("input_tokens_reported_call_count") or 0)
    row["input_tokens_reported"] += int(provider.get("input_tokens") or 0) if provider.get("input_tokens_reported_call_count") else 0
    return False


# LLM: 计数键与 _add_event、decision_usage_summary 的合计口径一致；新增计数须三处同步。
# 函数用途: 生成一行全零的决策计数。
def _zero_row() -> dict:
    return {"calls": 0, **dict.fromkeys(_STATUS_KEYS, 0), "input_tokens_reported": 0, "input_reported_calls": 0}


# LLM: 用量文件修改时间早于窗口的整份跳过（不读内容），否则按事件 created_at 过滤；坏账本行只计数。
# 函数用途: 统计一个会话在时间窗内的决策计数，返回（计数行或 None、无用途旧账条数、是否有坏行）。
def _thread_usage(usage: object, thread_id: str, since: float) -> tuple[dict | None, int, int]:
    revision = usage.revision(thread_id)
    if not revision or revision[3] / 1e9 < since:
        return None, 0, 0
    events, errors = usage.events_report(thread_id)
    row, legacy = _zero_row(), 0
    for event in [event for event in events if event.created_at >= since]:
        legacy += int(_add_event(row, event.model_calls))
    return row, legacy, int(bool(errors))


# LLM: 只读 model_usage 领域的 revision/events_report；in_flight = 调用次数 − 已结束（finished/failed/timed_out），
#   表示记账时尚未结束的调用。会话明细按调用次数取前 _THREAD_ROWS 条。
# 函数用途: 汇总这些会话在时间窗内的决策调用次数、成败与已报输入 token，并给出调用最多的会话明细。
def decision_usage_summary(store: object, thread_ids: list[str], *, since: float) -> dict:
    totals = {"legacy_events_without_purpose": 0, "unreadable_threads": 0}
    rows = []
    for thread_id in thread_ids:
        row, legacy, unreadable = _thread_usage(store.model_usage, thread_id, since)
        totals["legacy_events_without_purpose"] += legacy
        totals["unreadable_threads"] += unreadable
        if row and row["calls"]:
            rows.append({"thread_id": thread_id, **row})
    totals.update({key: sum(item[key] for item in rows) for key in _zero_row()})
    totals["threads_with_calls"] = len(rows)
    totals["in_flight"] = max(0, totals["calls"] - sum(totals[key] for key in _STATUS_KEYS))
    rows.sort(key=lambda item: item["calls"], reverse=True)
    return {**totals, "threads": rows[:_THREAD_ROWS]}


__all__ = ["decision_settings_summary", "decision_usage_summary"]
