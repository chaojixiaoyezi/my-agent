# LLM: 生命周期唤醒片的宿主事件只从唤醒信封的结构化字段确定性投影：字段白名单、键排序、字符串与列表有界；
#   不复制后台上下文注入，不收原始结果 JSON（runner_result_json/output_json）。宿主决策仍只读 turn_trigger 与 wake_signal，
#   事实文本只给模型看。改字段或上限时同步 test_lifecycle_wake_host_event。
# 模块用途: 把子代理或原输入结算唤醒转换成回合触发类型；只呈现安全事实，不增加恢复或结果读取权限。
from __future__ import annotations

import json
import math
import re
import time
from collections.abc import Mapping, Sequence

from ..agent_core.runtime.turn_trigger import (
    TURN_TRIGGER_LIFECYCLE_WAKE,
    USER_INPUT_YIELD_TRIGGER_REASON,
    TurnTrigger,
)

# 唤醒信封顶层与 metadata 里进入宿主事件的字段；原始结果 JSON、去重键等宿主内部字段不在名单里。
# takeover_hint 是执行器退出后的结构化接替提示（C4，开关 subagent_takeover_hint_enabled 开时宿主才写入）。
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
    "takeover_hint",
    "service_window_incomplete",
    "service_window_remaining_seconds",
)
# 唤醒事实单条文本最多 600 字符。
_WAKE_FACT_TEXT_LIMIT_CHARS = 600
# 唤醒事实列表最多 8 条。
_WAKE_FACT_LIST_LIMIT_COUNT = 8
# 唤醒事实嵌套深度最多 3 层：防止深层结构撑爆。
_WAKE_FACT_DEPTH_LIMIT = 3


# LLM: 输入是宿主唤醒原因、唤醒信封、真实历史请求编号和调用方已判定的任务来源原文（都来自结构化事实）；输出冻结值
#   对象，JSON 按键排序。纯计算。origin_request_ids 只能放会话历史里真有用户消息的请求；原任务不在历史里（已核实的
#   Goal 任务来源，或唤醒没有请求编号只能取任务链接）时，调用方传 origin_task，事件附一段有界原文并如实标记
#   origin_task_attached，事件首句据此不再声称原任务在历史里。不另写用户任务。
# 函数用途: 生成子代理或输入结算的宿主事件事实，原输入终态只展示，不作为新的用户请求或恢复权限。
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
    }
    if normalized_reason != USER_INPUT_YIELD_TRIGGER_REASON:
        # 让出续跑没有子代理事实，不放空的 child 段误导模型。
        facts["child"] = _picked(metadata, _CHILD_FIELDS)
    input_facts = _input_resolution_facts(normalized_reason, signal, metadata)
    if input_facts:
        facts['input_dependency'] = input_facts
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
        credential_ready=bool(input_facts.get("credential")),
    )


# LLM: 仅原 task-input-event.v1 的终态标量进入输入事实；凭据 ready 仅投影已校验公开元数据，绝不复制秘密或私填路径。
# 函数用途: 让恢复模型知道原输入已经结算及可用凭据引用，避免把 ready 当成尚需填写；不改变原 Wake 调度和子代理事实。
def _input_resolution_facts(reason, signal, metadata):
    if reason != 'task_input_resolved' or metadata.get('schema_version') != 'task-input-event.v1':
        return {}
    status = metadata.get('input_status')
    if not isinstance(status, str) or status not in {'ready', 'cancelled', 'expired', 'revoked', 'failed'}:
        return {}
    if any(not isinstance(metadata.get(key), str) or not metadata[key].strip()
           for key in ('input_id', 'input_provider')):
        return {}
    refs = signal.get('evidence_refs')
    clean_refs = (
        {ref.strip() for ref in refs if isinstance(ref, str) and ref.strip()}
        if isinstance(refs, (list, tuple))
        else set()
    )
    available = status == 'ready' and bool(clean_refs)
    facts: dict[str, object] = {
        'input_id': metadata['input_id'][:_WAKE_FACT_TEXT_LIMIT_CHARS],
        'provider': metadata['input_provider'][:_WAKE_FACT_TEXT_LIMIT_CHARS],
        'status': status,
        'result_available': available,
        'user_input_required': False,
    }
    if available and metadata['input_provider'] == 'credential':
        from .input_dependencies import _validated_credential_result

        cred = _validated_credential_result(metadata.get('credential_result'))
        if cred is not None and cred['credential_ref'] in clean_refs:
            facts['credential'] = cred
    return facts


def protected_resource_check(params, *, credential_ref, generation, method, http_status):
    """从已认证的实际业务响应与宿主回合身份构造审计事实；调用方另须确认原目标资源及其访问保护证据才可附上。"""
    trigger = getattr(params, 'turn_trigger', None)
    if (not isinstance(trigger, TurnTrigger) or trigger.kind != TURN_TRIGGER_LIFECYCLE_WAKE
            or trigger.reason != 'task_input_resolved' or not trigger.credential_ready):
        return None
    return validate_protected_resource_check({
        'schema_version': 'protected_resource_check.v1',
        'credential_ref': credential_ref, 'generation': generation,
        'wake_id': trigger.wake_signal_id, 'model_request_id': getattr(params, 'request_id', ''),
        'method': method, 'http_status': http_status, 'checked_at': time.time(),
    })


def validate_protected_resource_check(value: object) -> dict[str, object] | None:
    """只保留固定的标识、认证代次、HTTP 结果和检查时间。"""
    if not isinstance(value, Mapping) or set(value) != {
        'schema_version', 'credential_ref', 'generation', 'wake_id', 'model_request_id',
        'method', 'http_status', 'checked_at',
    }:
        return None
    ref, generation = value['credential_ref'], value['generation']
    status, checked_at = value['http_status'], value['checked_at']
    if (value['schema_version'] != 'protected_resource_check.v1'
            or not isinstance(ref, str) or not re.fullmatch(r'cred_[0-9a-f]{32}', ref)
            or type(generation) is not int or generation < 1
            or value['method'] not in ('GET', 'HEAD')
            or type(status) is not int or not 200 <= status < 300
            or type(checked_at) is not float or not math.isfinite(checked_at) or checked_at <= 0):
        return None
    for key in ('wake_id', 'model_request_id'):
        if not isinstance(value[key], str) or not re.fullmatch(r'[A-Za-z0-9_.:/@-]{1,256}', value[key]):
            return None
    return dict(value)


# LLM: 只取名单内且有值的字段（布尔与数字 0 也算有值），逐个做有界投影；不读名单外的键。
# 函数用途: 从唤醒信封的一层字典里挑出进入宿主事件的字段。
def _picked(source: dict, fields: tuple[str, ...]) -> dict[str, object]:
    return {key: _bounded(source[key], 0) for key in fields if key in source and source[key] not in (None, "", [], {})}


# LLM: 字符串截到 _WAKE_FACT_TEXT_LIMIT_CHARS，列表与字典各取前若干项，嵌套超过 _WAKE_FACT_DEPTH_LIMIT 层写占位说明；未知类型转字符串。纯计算。
# 函数用途: 把任意 JSON 值压成有界、可稳定序列化的投影。
def _bounded(value: object, depth: int) -> object:
    if isinstance(value, str):
        return value if len(value) <= _WAKE_FACT_TEXT_LIMIT_CHARS else value[:_WAKE_FACT_TEXT_LIMIT_CHARS] + "…（已截断）"
    if value is None or isinstance(value, bool | int | float):
        return value
    if depth >= _WAKE_FACT_DEPTH_LIMIT:
        return "…（层级过深，已省略）"
    if isinstance(value, dict):
        items = sorted(value.items(), key=lambda item: str(item[0]))[: _WAKE_FACT_LIST_LIMIT_COUNT * 2]
        return {str(key): _bounded(item, depth + 1) for key, item in items}
    if isinstance(value, list | tuple):
        items = [_bounded(item, depth + 1) for item in list(value)[:_WAKE_FACT_LIST_LIMIT_COUNT]]
        return items + (["…（其余已省略）"] if len(value) > _WAKE_FACT_LIST_LIMIT_COUNT else [])
    return str(value)[:_WAKE_FACT_TEXT_LIMIT_CHARS]


__all__ = ["lifecycle_wake_turn_trigger"]
