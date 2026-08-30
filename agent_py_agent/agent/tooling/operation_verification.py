from __future__ import annotations

"""Typed current-turn execution facts shared by every agent delivery surface."""

import json
from collections import OrderedDict
from typing import Any

_MAX_PROMPT_RECENT_CALLS = 6
_MAX_PROMPT_MUTATING_CALLS = 6
_MAX_PROMPT_ARCHIVE_REFS = 4
_MAX_MUTATING_CALLS = 64
_MAX_VISIBLE_OPERATION_GROUPS = 12
_MAX_REFS_PER_CALL = 4
_MUTATING_EFFECTS = frozenset({"mutating", "dangerous"})
_PRE_HANDLER_FAILURE_STAGES = frozenset(
    {"protocol", "authorization", "validation", "runtime_gate"}
)
_PUBLIC_VERIFICATION_STATUSES = frozenset(
    {
        "succeeded",
        "failed",
        "not_started",
        "unknown",
        "cancelled",
        "incomplete",
        "unverified",
    }
)
_PUBLIC_OVERALL_STATUSES = frozenset(
    {"none", "succeeded", "uncertain", "partial", "cancelled", "failed"}
)


def render_current_turn_execution_facts(
    agent: object,
    records: list[dict[str, object]] | None,
) -> str:
    """Render the current request's structured tool facts at the prompt tail.

    会话运行时 keeps function-call outputs as typed response items and 长期助手 warns
    that prose is only a self-report until a returned handle is verified.  Our
    provider prompt is a single user message on the first native-tool round, so
    the compact tool catalog can be far from the active user request.  Put one
    bounded projection of the authoritative runtime records *after* that
    request.  This does not infer intent or parse assistant prose.
    """

    normalized = [
        _execution_call(agent, record)
        for record in list(records or [])
        if isinstance(record, dict)
    ]
    mutating = [item for item in normalized if item["effect"] in _MUTATING_EFFECTS]
    successful_mutating = [
        item for item in mutating if item["verification_status"] == "succeeded"
    ]
    unsuccessful_mutating = [
        item for item in mutating if item["verification_status"] != "succeeded"
    ]
    verification_counts = {
        status: sum(item["verification_status"] == status for item in normalized)
        for status in _PUBLIC_VERIFICATION_STATUSES
    }
    effect_counts = {
        effect: sum(item["effect"] == effect for item in normalized)
        for effect in ("read_only", "mutating", "dangerous", "unknown")
    }
    mutating_groups = _visible_operation_groups(mutating)
    archive_refs = _recent_raw_archive_refs(records)
    payload = {
        "schema": "current_turn_execution.v2",
        "scope": "current_request_only",
        "call_count": len(normalized),
        "effect_counts": effect_counts,
        "verification_counts": verification_counts,
        "mutating_operation_groups": mutating_groups[-_MAX_VISIBLE_OPERATION_GROUPS:],
        "omitted_mutating_operation_group_count": max(
            0, len(mutating_groups) - _MAX_VISIBLE_OPERATION_GROUPS
        ),
        "successful_mutating_calls": successful_mutating[-_MAX_PROMPT_MUTATING_CALLS:],
        "omitted_successful_mutating_call_count": max(
            0, len(successful_mutating) - _MAX_PROMPT_MUTATING_CALLS
        ),
        "unsuccessful_mutating_calls": unsuccessful_mutating[
            -_MAX_PROMPT_MUTATING_CALLS:
        ],
        "omitted_unsuccessful_mutating_call_count": max(
            0, len(unsuccessful_mutating) - _MAX_PROMPT_MUTATING_CALLS
        ),
        "recent_calls": normalized[-_MAX_PROMPT_RECENT_CALLS:],
        "omitted_call_count": max(0, len(normalized) - _MAX_PROMPT_RECENT_CALLS),
        "raw_archive_refs": archive_refs,
    }
    return (
        "# Current Turn Execution Facts\n"
        "```json\n"
        + json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n```\n"
        "这是本次请求的结构化执行事实，不是历史对话或模型自述。"
        "只有 successful_mutating_calls 中同时具有 succeeded 权威操作终态的本轮调用，才允许支持"
        "“已经保存、修改、发送、创建或删除”等副作用结论；空列表表示本轮尚无这类成功事实。"
        "unsuccessful_mutating_calls 必须按失败或未完成说明。"
        "较早明细被省略时，以聚合计数和 raw_archive_refs 指向的 owner 私有原始记录为准，"
        "不得因明细不在热上下文中而重做已经执行过的调用。"
        "若成功调用给出 refs，扩大成功结论前优先按句柄回读核验。"
    )


# LLM: 最终操作核验只能复用当前轮 archive/operation 事实；不得扫描模型正文猜“已完成”语义。
# 函数用途: 生成当前请求副作用操作的可审计投影，供 CLI、Gateway、IM 和 transcript 共用。
def build_operation_verification(
    agent: object,
    records: list[dict[str, object]] | None,
) -> dict[str, object]:
    calls = [
        _execution_call(agent, record)
        for record in list(records or [])
        if isinstance(record, dict)
    ]
    mutating = [item for item in calls if item["effect"] in _MUTATING_EFFECTS]
    all_operations = _deduplicated_operations(mutating)
    counts = {
        status: sum(item["verification_status"] == status for item in all_operations)
        for status in (
            "succeeded",
            "failed",
            "not_started",
            "unknown",
            "cancelled",
            "incomplete",
            "unverified",
        )
    }
    return {
        "schema": "operation_verification.v1",
        "scope": "current_request_only",
        "status": _overall_verification_status(all_operations),
        "operation_count": len(all_operations),
        "omitted_operation_count": max(
            0, len(all_operations) - _MAX_MUTATING_CALLS
        ),
        "counts": counts,
        "operations": all_operations[-_MAX_MUTATING_CALLS:],
    }


def redact_executed_operation_labels(
    content: str,
    verification: dict[str, object],
) -> str:
    """Hide exact executed protocol labels from ordinary user prose.

    The candidates come only from this turn's typed operation records.  This
    does not inspect prose for business meaning and does not maintain a word
    list.  Structured verification remains available in result/transcript
    metadata; only an accidental rendering of internal tool protocol is
    replaced at the user boundary.
    """

    operations = verification.get("operations")
    if not isinstance(operations, list) or not operations:
        return str(content or "")
    labels: set[str] = set()
    for operation in operations:
        if not isinstance(operation, dict):
            continue
        tool = str(operation.get("tool") or "").strip()
        action = str(operation.get("action") or "").strip()
        if tool and action:
            labels.add(f"{tool}/{action}")
        if tool and any(marker in tool for marker in ("_", "-", ".")):
            labels.add(tool)
    projected = str(content or "")
    for label in sorted(labels, key=len, reverse=True):
        projected = projected.replace(label, "相关操作")
    return projected


# LLM: 对外 API 只公开聚合后的工具/动作/终态，不公开 call_id、operation_id、路径或副作用引用。
# 函数用途: 从内部核验投影生成可以安全写入 channel_delivery 的摘要。
def public_operation_verification(
    verification: object,
) -> dict[str, object]:
    value = verification if isinstance(verification, dict) else {}
    operations = value.get("operations")
    if isinstance(operations, list):
        groups = _visible_operation_groups(operations)[:_MAX_VISIBLE_OPERATION_GROUPS]
    else:
        groups = _normalized_public_groups(value.get("groups"))
    status = str(value.get("status") or "none")
    if status not in _PUBLIC_OVERALL_STATUSES:
        status = "none"
    operation_count = max(0, _int_value(value.get("operation_count")))
    if not operation_count and groups:
        operation_count = sum(int(group["count"]) for group in groups)
    return {
        "schema": "operation_verification.public.v1",
        "status": status,
        "operation_count": operation_count,
        "omitted_operation_count": max(
            0, _int_value(value.get("omitted_operation_count"))
        ),
        "counts": _public_verification_counts(value.get("counts")),
        "groups": groups,
    }


# LLM: 当前轮调用投影只读取 archive typed fields 与运行快照，不检查 output 或最终回复文本。
# 函数用途: 规范一条工具记录，供 prompt 事实和最终操作核验共用。
def _execution_call(agent: object, record: dict[str, object]) -> dict[str, object]:
    tool = str(record.get("tool") or "").strip() or "unknown"
    parameters = record.get("parameters")
    effect = _tool_effect(agent, tool, parameters if isinstance(parameters, dict) else {})
    item: dict[str, object] = {
        "call_id": str(record.get("call_id") or record.get("id") or "").strip(),
        "tool": tool,
        "effect": effect,
        "ok": record.get("ok") is True,
        "status": str(record.get("status") or "").strip(),
        "handler_executed": record.get("handler_executed") is True,
    }
    operation_id = str(record.get("operation_id") or "").strip()
    if operation_id:
        item["operation_id"] = operation_id
    action = _declared_business_action(agent, tool, record)
    if action:
        item["action"] = action
    for key in (
        "error_code",
        "failure_stage",
        "effect_outcome",
        "tool_operation_status",
        "tool_operation_action",
    ):
        value = str(record.get(key) or "").strip()
        if value:
            item[key] = value
    if record.get("tool_operation_replayed") is True:
        item["replayed"] = True
    item["verification_status"] = _verification_status(item)
    refs = _record_refs(record)
    if refs:
        item["refs"] = refs
    return item


def _tool_effect(
    agent: object,
    tool_name: str,
    parameters: dict[str, object],
) -> str:
    registry = getattr(agent, "tools", None)
    if registry is None or not hasattr(registry, "runtime_snapshot"):
        return "unknown"
    try:
        from .models import tool_effect_for_runtime_policy

        runtime = registry.runtime_snapshot().runtime(tool_name)
        if runtime is None:
            return "unknown"
        return tool_effect_for_runtime_policy(runtime.runtime_policy, parameters)
    except (AttributeError, TypeError, ValueError):
        return "unknown"


# LLM: 动作名只接受当前 ToolModelSpec Schema 明示的 action enum，不能把任意模型参数显示成可信动作。
# 函数用途: 从已校验工具参数中提取一个安全、精确的业务动作标签。
def _declared_business_action(
    agent: object,
    tool_name: str,
    record: dict[str, object],
) -> str:
    parameters = record.get("parameters")
    if not isinstance(parameters, dict):
        return ""
    value = parameters.get("action")
    if not isinstance(value, str):
        return ""
    registry = getattr(agent, "tools", None)
    if registry is None or not hasattr(registry, "runtime_snapshot"):
        return ""
    try:
        runtime = registry.runtime_snapshot().runtime(tool_name)
        if runtime is None:
            return ""
        schema = runtime.model_spec.input_schema
    except (AttributeError, TypeError, ValueError):
        return ""
    properties = schema.get("properties")
    action_schema = properties.get("action") if isinstance(properties, dict) else None
    allowed = action_schema.get("enum") if isinstance(action_schema, dict) else None
    return value if isinstance(allowed, list) and value in allowed else ""


# LLM: 副作用成功必须同时具有 ok=true 与 operation=succeeded；任一缺失都不能补猜成成功。
# 函数用途: 将一条当前轮工具记录归类为成功、失败、未执行、未知、取消或未核验。
def _verification_status(item: dict[str, object]) -> str:
    if item.get("effect") not in _MUTATING_EFFECTS:
        return "succeeded" if item.get("ok") is True else "failed"
    operation_status = str(item.get("tool_operation_status") or "").strip().lower()
    effect_outcome = str(item.get("effect_outcome") or "").strip().lower()
    if operation_status == "succeeded" and item.get("ok") is True:
        return "succeeded"
    if operation_status == "unknown" or effect_outcome == "unknown":
        return "unknown"
    if operation_status == "cancelled":
        return "cancelled"
    if operation_status == "running":
        return "incomplete"
    if operation_status == "failed":
        return "not_started" if effect_outcome == "not_started" else "failed"
    failure_stage = str(item.get("failure_stage") or "").strip().lower()
    if (
        item.get("ok") is not True
        and item.get("handler_executed") is not True
        and failure_stage in _PRE_HANDLER_FAILURE_STAGES
    ):
        return "not_started"
    return "unverified"


# LLM: 同一 operation 的幂等重放只能算一次业务操作，最新权威终态覆盖较早尝试态。
# 函数用途: 按 operation_id、call_id 的顺序去重操作并累计尝试次数。
def _deduplicated_operations(
    calls: list[dict[str, object]],
) -> list[dict[str, object]]:
    keyed: OrderedDict[str, dict[str, object]] = OrderedDict()
    for index, item in enumerate(calls):
        operation_id = str(item.get("operation_id") or "").strip()
        call_id = str(item.get("call_id") or "").strip()
        key = operation_id or call_id or f"record-{index}"
        previous = keyed.get(key)
        projected = _operation_projection(item)
        if previous is not None:
            projected["attempt_count"] = int(previous.get("attempt_count") or 1) + 1
            projected["replayed"] = (
                previous.get("replayed") is True or projected.get("replayed") is True
            )
        keyed[key] = projected
    return list(keyed.values())


# LLM: 内部核验记录只保留类型化状态与引用 ID，不复制工具正文、参数值或路径。
# 函数用途: 生成一条可审计但有界的操作投影。
def _operation_projection(item: dict[str, object]) -> dict[str, object]:
    projected: dict[str, object] = {
        "tool": str(item.get("tool") or "unknown"),
        "effect": str(item.get("effect") or "unknown"),
        "verification_status": str(item.get("verification_status") or "unverified"),
        "handler_executed": item.get("handler_executed") is True,
        "attempt_count": 1,
        "replayed": item.get("replayed") is True,
    }
    for key in (
        "call_id",
        "operation_id",
        "action",
        "error_code",
        "failure_stage",
        "effect_outcome",
        "tool_operation_status",
        "tool_operation_action",
    ):
        value = str(item.get(key) or "").strip()
        if value:
            projected[key] = value
    return projected


# LLM: 总体状态只能由逐项机器终态聚合，不能由最终回复措辞决定。
# 函数用途: 汇总本轮所有副作用操作的整体核验状态。
def _overall_verification_status(operations: list[dict[str, object]]) -> str:
    if not operations:
        return "none"
    statuses = {
        str(item.get("verification_status") or "unverified") for item in operations
    }
    if statuses == {"succeeded"}:
        return "succeeded"
    if "unknown" in statuses or "unverified" in statuses or "incomplete" in statuses:
        return "uncertain"
    if "succeeded" in statuses:
        return "partial"
    if statuses == {"cancelled"}:
        return "cancelled"
    return "failed"


# LLM: 用户摘要按工具、Schema 动作和核验状态聚合；不得携带内部 operation/call 标识。
# 函数用途: 把内部逐操作记录压成可公开的有界分组。
def _visible_operation_groups(
    operations: list[object],
) -> list[dict[str, object]]:
    groups: OrderedDict[tuple[str, str, str], dict[str, object]] = OrderedDict()
    for item in operations:
        if not isinstance(item, dict):
            continue
        tool = str(item.get("tool") or "unknown")
        action = str(item.get("action") or "")
        status = str(item.get("verification_status") or "unverified")
        key = (tool, action, status)
        group = groups.setdefault(
            key,
            {
                "tool": tool,
                "action": action,
                "label": f"{tool}/{action}" if action else tool,
                "status": status,
                "count": 0,
                "replayed": False,
            },
        )
        group["count"] = int(group["count"]) + 1
        group["replayed"] = group["replayed"] is True or item.get("replayed") is True
    return list(groups.values())


# LLM: 公开核验对象可能经过 Gateway、HTTP 和 transcript 多层投影；重复清洗必须保持同一业务分组。
# 函数用途: 校验并重建已经公开化的操作分组，使公开投影幂等且不接受内部标识或任意嵌套字段。
def _normalized_public_groups(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    groups: list[dict[str, object]] = []
    for item in value[:_MAX_VISIBLE_OPERATION_GROUPS]:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status") or "unverified")
        if status not in _PUBLIC_VERIFICATION_STATUSES:
            status = "unverified"
        tool = _public_label_part(item.get("tool"), fallback="unknown")
        action = _public_label_part(item.get("action"))
        groups.append(
            {
                "tool": tool,
                "action": action,
                "label": f"{tool}/{action}" if action else tool,
                "status": status,
                "count": max(1, _int_value(item.get("count"))),
                "replayed": item.get("replayed") is True,
            }
        )
    return groups


# LLM: 工具/动作标签只作为短公开枚举显示；换行、控制字符和超长输入不得穿透多次投影。
# 函数用途: 规范公开操作分组中的单个标签字段。
def _public_label_part(value: object, *, fallback: str = "") -> str:
    text = " ".join(str(value or "").split()).strip()
    return (text[:80] or fallback)


# LLM: 状态文案只映射固定枚举，不分析或改写模型自然语言。
# 函数用途: 将机器核验状态转成简短中文说明。
def _verification_status_text(status: str) -> str:
    return {
        "succeeded": "成功",
        "failed": "失败",
        "not_started": "未执行",
        "unknown": "结果未知，系统未自动重试",
        "cancelled": "已取消",
        "incomplete": "尚未结束，未按成功处理",
        "unverified": "缺少权威终态，未按成功处理",
    }.get(status, "未按成功处理")


# LLM: 对外计数使用固定状态白名单；未知键和非整数值一律忽略。
# 函数用途: 清洗 operation verification 的公开计数字段。
def _public_verification_counts(value: object) -> dict[str, int]:
    source = value if isinstance(value, dict) else {}
    return {
        key: max(0, _int_value(source.get(key)))
        for key in (
            "succeeded",
            "failed",
            "not_started",
            "unknown",
            "cancelled",
            "incomplete",
            "unverified",
        )
    }


# LLM: 计数转换失败时只能回退零，不能让外部畸形值进入公开结果。
# 函数用途: 安全转换公开核验计数。
def _int_value(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _record_refs(record: dict[str, object]) -> list[str]:
    refs: list[str] = []
    for key in (
        "effect_source_ref",
        "artifact_ref",
        "source_artifact_ref",
        "output_path",
    ):
        _append_ref(refs, record.get(key))
    nested = record.get("tool_result_refs")
    if isinstance(nested, list):
        for item in nested:
            if not isinstance(item, dict):
                continue
            for key in ("url", "path", "id", "source_ref", "artifact_ref"):
                _append_ref(refs, item.get(key))
    envelope = record.get("tool_result_envelope")
    if isinstance(envelope, dict):
        for key in ("url", "path", "target_path", "output_path", "source_ref", "artifact_ref"):
            _append_ref(refs, envelope.get(key))
    return refs[:_MAX_REFS_PER_CALL]


def _recent_raw_archive_refs(
    records: list[dict[str, object]] | None,
) -> list[str]:
    refs: list[str] = []
    for record in reversed(list(records or [])):
        if not isinstance(record, dict):
            continue
        text = str(record.get("raw_archive_path") or "").strip()
        if text and text not in refs:
            refs.append(text)
        if len(refs) >= _MAX_PROMPT_ARCHIVE_REFS:
            break
    return list(reversed(refs))


def _append_ref(refs: list[str], value: Any) -> None:
    text = str(value or "").strip()
    if text and text not in refs:
        refs.append(text)


__all__ = [
    "build_operation_verification",
    "public_operation_verification",
    "redact_executed_operation_labels",
    "render_current_turn_execution_facts",
]
