# LLM: 生命周期唤醒片的宿主事件只从唤醒信封的结构化字段确定性投影：字段白名单、键排序、字符串与列表有界；
#   不复制后台上下文注入，不收原始结果 JSON（runner_result_json/output_json）。宿主决策仍只读 turn_trigger 与 wake_signal，
#   事实文本只给模型看。改字段或上限时同步 test_lifecycle_wake_host_event。
# 模块用途: 把一次子代理生命周期唤醒转换成回合触发类型（TurnTrigger），让后台工作片把本片记为宿主事件而不是新的用户任务。
from __future__ import annotations

import json
from collections.abc import Sequence

from ..agent_core.runtime.turn_trigger import TURN_TRIGGER_LIFECYCLE_WAKE, TurnTrigger

# 唤醒信封顶层与 metadata 里进入宿主事件的字段；原始结果 JSON、去重键等宿主内部字段不在名单里。
_SIGNAL_FIELDS = ("wake_signal_id", "source_agent_id", "parent_agent_id", "root_task_id")
_CHILD_FIELDS = (
    "status",
    "turn_end_reason",
    "failure_type",
    "completion_message",
    "completion_message_truncated",
    "final_report_ref",
    "declared_output_refs",
    "artifact_refs",
    "tool_failure_halt",
    "service_window_incomplete",
    "service_window_remaining_seconds",
)
_TEXT_LIMIT = 600
_LIST_LIMIT = 8
_DEPTH_LIMIT = 3


# LLM: 输入是宿主唤醒原因、唤醒信封、真实历史请求编号和调用方已判定的任务来源原文（都来自结构化事实）；输出冻结值
#   对象，JSON 按键排序。纯计算。origin_request_ids 只能放会话历史里真有用户消息的请求；原任务不在历史里（已核实的
#   Goal 任务来源，或唤醒没有请求编号只能取任务链接）时，调用方传 origin_task，事件附一段有界原文并如实标记
#   origin_task_attached，事件首句据此不再声称原任务在历史里。不另写用户任务。
# 函数用途: 生成本次生命周期唤醒的回合触发类型和给模型看的宿主事件事实。
def lifecycle_wake_turn_trigger(
    reason: str, wake_signal: object, origin_request_ids: Sequence[str], origin_task: str = "",
) -> TurnTrigger:
    signal = wake_signal if isinstance(wake_signal, dict) else {}
    metadata = signal.get("metadata") if isinstance(signal.get("metadata"), dict) else {}
    normalized_reason = str(reason or signal.get("reason") or "").strip().lower()
    origins = tuple(dict.fromkeys(str(item or "").strip() for item in origin_request_ids if str(item or "").strip()))
    facts: dict[str, object] = {
        "reason": normalized_reason,
        "origin_request_ids": list(origins),
        **_picked(signal, _SIGNAL_FIELDS),
        "child": _picked(metadata, _CHILD_FIELDS),
    }
    attached = bool(str(origin_task or "").strip())
    if attached:
        facts["origin_task"] = _bounded(str(origin_task).strip(), 0)
    return TurnTrigger(
        kind=TURN_TRIGGER_LIFECYCLE_WAKE,
        reason=normalized_reason,
        wake_signal_id=str(signal.get("wake_signal_id") or "").strip(),
        source_agent_id=str(signal.get("source_agent_id") or "").strip(),
        origin_request_ids=origins,
        event_facts=json.dumps(facts, ensure_ascii=False, sort_keys=True, indent=2),
        origin_task_attached=attached,
    )


# LLM: 只取名单内且有值的字段（布尔与数字 0 也算有值），逐个做有界投影；不读名单外的键。
# 函数用途: 从唤醒信封的一层字典里挑出进入宿主事件的字段。
def _picked(source: dict, fields: tuple[str, ...]) -> dict[str, object]:
    return {key: _bounded(source[key], 0) for key in fields if key in source and source[key] not in (None, "", [], {})}


# LLM: 字符串截到 _TEXT_LIMIT，列表与字典各取前若干项，嵌套超过 _DEPTH_LIMIT 层写占位说明；未知类型转字符串。纯计算。
# 函数用途: 把任意 JSON 值压成有界、可稳定序列化的投影。
def _bounded(value: object, depth: int) -> object:
    if isinstance(value, str):
        return value if len(value) <= _TEXT_LIMIT else value[:_TEXT_LIMIT] + "…（已截断）"
    if value is None or isinstance(value, bool | int | float):
        return value
    if depth >= _DEPTH_LIMIT:
        return "…（层级过深，已省略）"
    if isinstance(value, dict):
        items = sorted(value.items(), key=lambda item: str(item[0]))[: _LIST_LIMIT * 2]
        return {str(key): _bounded(item, depth + 1) for key, item in items}
    if isinstance(value, list | tuple):
        items = [_bounded(item, depth + 1) for item in list(value)[:_LIST_LIMIT]]
        return items + (["…（其余已省略）"] if len(value) > _LIST_LIMIT else [])
    return str(value)[:_TEXT_LIMIT]


__all__ = ["lifecycle_wake_turn_trigger"]
