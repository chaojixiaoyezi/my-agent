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
_MAX_FOLLOWUP_STALE_ROOTS = 4
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


# LLM: A final prose draft cannot override the last effect-bearing mutation.
# A trailing ``not_started`` attempt has no effect and therefore cannot erase
# an earlier succeeded terminal effect; explicit required actions remain owned
# by their separate completion gate. This reads typed records only, never prose.
# 函数用途: 找出最后一项真正产生过执行效果但未成功的副作用；纯执行前拒绝保留审计记录，不推翻更早成功终态。
def incomplete_final_mutation_facts(
    agent: object,
    records: list[dict[str, object]] | None,
) -> dict[str, object]:
    calls = [
        _execution_call(agent, record)
        for record in list(records or [])
        if isinstance(record, dict)
    ]
    mutating = [item for item in calls if item["effect"] in _MUTATING_EFFECTS]
    if not mutating:
        return {}
    latest = mutating[-1]
    latest_status = str(latest.get("verification_status") or "unverified")
    if latest_status == "succeeded" or _not_started_tail_follows_succeeded_effect(
        mutating
    ):
        return {}
    verification = build_operation_verification(agent, records)
    latest_public: dict[str, object] = {
        "tool": str(latest.get("tool") or "unknown"),
        "action": str(latest.get("action") or ""),
        "status": latest_status,
        "handler_executed": latest.get("handler_executed") is True,
    }
    for key in ("error_code", "failure_stage", "effect_outcome"):
        value = str(latest.get(key) or "").strip()
        if value:
            latest_public[key] = value
    return {
        "schema": "incomplete_final_mutation.v1",
        "latest_mutating_operation": latest_public,
        "operation_verification": public_operation_verification(verification),
    }


# LLM: ``not_started`` is a typed proof that no handler effect occurred. Keep
# every denied attempt in operation_verification, but let the latest prior
# effect-bearing terminal state remain authoritative. Unknown/cancelled/
# incomplete/failed attempts never use this exemption.
# 函数用途: 判断末尾一串执行前拒绝是否发生在已成功副作用之后，避免无副作用的清理尝试把整项任务误判未完成。
def _not_started_tail_follows_succeeded_effect(
    mutating: list[dict[str, object]],
) -> bool:
    if not mutating or str(mutating[-1].get("verification_status") or "") != "not_started":
        return False
    for item in reversed(mutating[:-1]):
        status = str(item.get("verification_status") or "unverified")
        if status == "not_started":
            continue
        return status == "succeeded"
    return False


# LLM: 这是一项软收口事实，优先读取被动验证账本的 stale root；普通 read/search 不能冒充测试证据。
# 旧归档缺少账本事实时才保留“失败后最后一条是成功 workspace mutation”的窄兼容判定，不解析最终草稿。
# 函数用途: 找出真实验证后又改了文件且尚未重新验证的收口，供模型获得一次自主复核机会。
def post_failure_workspace_mutation_followup_facts(
    agent: object,
    records: list[dict[str, object]] | None,
) -> dict[str, object]:
    raw_records = [item for item in list(records or []) if isinstance(item, dict)]
    calls = [_execution_call(agent, record) for record in raw_records]
    stale_states = _latest_stale_workspace_verification_states(raw_records)
    if stale_states:
        latest_index = max(int(item.get("record_index") or 0) for item in stale_states)
        prior_failures = [item for item in calls[: latest_index + 1] if item.get("ok") is not True]
        latest_record = raw_records[latest_index]
        latest_public = {
            "tool": str(latest_record.get("tool") or "unknown"),
            "call_id": str(latest_record.get("call_id") or latest_record.get("id") or ""),
            "status": "succeeded",
        }
        facts: dict[str, object] = {
            "schema": "post_failure_workspace_mutation_followup.v2",
            "latest_workspace_mutation": latest_public,
            "stale_workspace_verification": [
                _public_stale_verification_state(item)
                for item in stale_states[-_MAX_FOLLOWUP_STALE_ROOTS:]
            ],
            "prior_failed_call_count": len(prior_failures),
            "tool_record_count_after_latest_mutation": max(
                0, len(raw_records) - latest_index - 1
            ),
            "later_successful_verification": False,
        }
        if prior_failures:
            facts["latest_prior_failure"] = _public_prior_failure(prior_failures[-1])
        return facts
    if len(calls) < 2:
        return {}
    latest = calls[-1]
    if (
        not _tool_mutates_workspace(agent, str(latest.get("tool") or ""))
        or latest.get("verification_status") != "succeeded"
    ):
        return {}
    prior_failures = [item for item in calls[:-1] if item.get("ok") is not True]
    if not prior_failures:
        return {}
    prior = prior_failures[-1]
    return {
        "schema": "post_failure_workspace_mutation_followup.v1",
        "latest_workspace_mutation": {
            "tool": str(latest.get("tool") or "unknown"),
            "status": "succeeded",
        },
        "prior_failed_call_count": len(prior_failures),
        "latest_prior_failure": _public_prior_failure(prior),
        "tool_record_after_latest_mutation": False,
    }


# LLM: dedupe keys are derived from durable verification event ids; do not use mutation call ids or changed-path counts,
# because additional edits in the same stale cycle must not create an unbounded soft-followup loop.
# 函数用途: 生成一次收口提醒的稳定签名，同一次真实验证后的多次修改只提醒一次。
def post_failure_workspace_mutation_followup_signature(
    facts: dict[str, object],
) -> str:
    states = facts.get("stale_workspace_verification")
    if not isinstance(states, list):
        return "legacy-post-failure-mutation"
    signature = [
        (
            str(item.get("root") or ""),
            _int_value(item.get("last_verification_id")),
            str(item.get("last_verification_status") or ""),
        )
        for item in states
        if isinstance(item, dict)
    ]
    return "verification-cycle:" + json.dumps(
        signature,
        ensure_ascii=False,
        separators=(",", ":"),
    )


# LLM: state replay consumes only compact tool_result_envelope facts in archive order; later verification evidence
# supersedes an earlier stale state for the same canonical root, while read/search records leave it unchanged.
# 函数用途: 计算当前轮最后仍处于 stale 的项目根，忽略不能证明构建或测试结果的普通读取。
def _latest_stale_workspace_verification_states(
    records: list[dict[str, object]],
) -> list[dict[str, object]]:
    by_root: dict[str, dict[str, object]] = {}
    for index, record in enumerate(records):
        envelope = _record_verification_envelope(record)
        evidence = envelope.get("verification_evidence")
        if isinstance(evidence, dict):
            root = str(evidence.get("root") or "").strip()
            if root:
                by_root[root] = {
                    "root": root,
                    "status": str(evidence.get("status") or "unverified"),
                    "last_verification_id": _int_value(evidence.get("id")),
                    "last_verification_status": str(
                        evidence.get("status") or "unverified"
                    ),
                    "record_index": index,
                }
        states = envelope.get("verification_state")
        for state in states if isinstance(states, list) else ():
            if not isinstance(state, dict):
                continue
            root = str(state.get("root") or "").strip()
            if not root:
                continue
            by_root[root] = {
                "root": root,
                "status": str(state.get("status") or "unverified"),
                "last_verification_id": _int_value(
                    state.get("last_verification_id")
                ),
                "last_verification_status": str(
                    state.get("last_verification_status") or ""
                ),
                "changed_path_count": len(
                    state.get("changed_paths")
                    if isinstance(state.get("changed_paths"), list)
                    else ()
                ),
                "record_index": index,
            }
    return sorted(
        (item for item in by_root.values() if item.get("status") == "stale"),
        key=lambda item: int(item.get("record_index") or 0),
    )


# LLM: verification facts have one canonical archive location; the nested output fallback only reads older compact
# projections and never promotes arbitrary tool output text into evidence.
# 函数用途: 从工具归档信封读取被动验证事实。
def _record_verification_envelope(record: dict[str, object]) -> dict[str, object]:
    envelope = record.get("tool_result_envelope")
    if not isinstance(envelope, dict):
        return {}
    if "verification_evidence" in envelope or "verification_state" in envelope:
        return envelope
    output = envelope.get("output")
    return output if isinstance(output, dict) else {}


# LLM: only bounded, non-secret fields from stale state enter model guidance; changed paths stay in the durable ledger.
# 函数用途: 清洗一条过期验证状态，避免把大路径列表重复塞回上下文。
def _public_stale_verification_state(
    state: dict[str, object],
) -> dict[str, object]:
    return {
        "root": str(state.get("root") or ""),
        "status": "stale",
        "last_verification_id": _int_value(state.get("last_verification_id")),
        "last_verification_status": str(
            state.get("last_verification_status") or ""
        ),
        "changed_path_count": _int_value(state.get("changed_path_count")),
    }


# LLM: prior failure summaries use normalized execution facts only and never expose raw command arguments or output.
# 函数用途: 为软提醒生成最近一次失败的安全摘要。
def _public_prior_failure(prior: dict[str, object]) -> dict[str, object]:
    result: dict[str, object] = {
        "tool": str(prior.get("tool") or "unknown"),
        "status": str(prior.get("verification_status") or "failed"),
    }
    error_code = str(prior.get("error_code") or "").strip()
    if error_code:
        result["error_code"] = error_code
    return result


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


# LLM: workspace mutation 身份只读注册表 ToolRuntimePolicy.mutates_workspace；工具名、参数文本和最终回复都不能冒充该能力事实。
# 函数用途: 判断一个已注册工具是否声明可能修改当前工作区。
def _tool_mutates_workspace(agent: object, tool_name: str) -> bool:
    registry = getattr(agent, "tools", None)
    if registry is None or not hasattr(registry, "runtime_snapshot"):
        return False
    try:
        runtime = registry.runtime_snapshot().runtime(tool_name)
        return bool(runtime is not None and runtime.runtime_policy.mutates_workspace)
    except (AttributeError, TypeError, ValueError):
        return False


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
    "incomplete_final_mutation_facts",
    "post_failure_workspace_mutation_followup_facts",
    "post_failure_workspace_mutation_followup_signature",
    "public_operation_verification",
    "redact_executed_operation_labels",
    "render_current_turn_execution_facts",
]
