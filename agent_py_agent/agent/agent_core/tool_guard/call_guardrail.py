# LLM: 工具结果后的观测只写现有有界记录并返回软提示；安全 effect、审批、执行和 UNKNOWN 仍由原合同控制。
# 模块用途: 将真实工具调用结果交给重复门与成功重复观察，并为下一模型轮提供短提醒。
from __future__ import annotations

from ...contracts.gates.tool_guardrail import (
    ToolGuardrailConfig,
    ToolGuardrailFacts,
    evaluate_tool_guardrail_gate,
    failure_class_of_result,
    record_tool_guardrail_result,
    repeated_success_observation,
    result_hash_for_guardrail,
)
from ...tooling.models import tool_effect_for_runtime_policy
from ...tooling.runtime_contracts import ToolCall, ToolResult
from .call_guardrail_config import (
    readonly_no_progress_threshold,
    repeat_fail_threshold,
    repeated_success_hint_threshold,
)
from .call_guardrail_config import (
    terminal_block_enabled as configured_terminal_block_enabled,
)

_RECORDS_ATTR = "_tool_call_guardrail_records"
_MAX_RECORDS = 256


def tool_guardrail_records(agent: object) -> tuple[dict[str, object], ...]:
    records = getattr(agent, _RECORDS_ATTR, None)
    if not isinstance(records, tuple):
        records = ()
    return tuple(dict(item) for item in records if isinstance(item, dict))


# LLM: 该快照沿原 write_boundary 传递；新成功阈值不由 ActionPolicy 用作阻断依据。
# 函数用途: 导出当前工作片实际生效的重复门和软提醒参数。
def tool_guardrail_policy(params: object) -> dict[str, object]:
    return {
        "repeat_fail_threshold": repeat_fail_threshold(params),
        "readonly_no_progress_threshold": readonly_no_progress_threshold(params),
        "terminal_block_enabled": configured_terminal_block_enabled(params),
        "repeated_success_hint_threshold": repeated_success_hint_threshold(params),
    }


# LLM: 先落规范调用事实，再计算观察；只读 gate 的进展边界保留为记录而非删除，不改变任何安全 effect。
# 函数用途: 更新同一有界工具账并生成下一轮软提示，避免成功 Shell 复读完全没有提醒。
def record_tool_guard_observation(
    agent: object,
    runtime_params: object,
    call: ToolCall,
    result: ToolResult,
) -> str:
    facts = _facts_from_result(runtime_params, call, result)
    records = record_tool_guardrail_result(
        tool_guardrail_records(agent),
        facts,
        max_records=_MAX_RECORDS,
    )
    _set_tool_guardrail_records(agent, records)
    config = ToolGuardrailConfig(**tool_guardrail_policy(runtime_params))
    decision = evaluate_tool_guardrail_gate(
        _facts_for_next_hint(facts),
        config=config,
        records=records,
    )
    if decision.findings:
        return decision.model_message if decision.allowed else ""
    finding = repeated_success_observation(
        facts, records, threshold=config.repeated_success_hint_threshold,
    )
    _set_tool_guardrail_records(agent, records)
    if finding is None:
        return ""
    records[-1]["repeated_success_hint_count"] = finding.evidence["count"]
    _set_tool_guardrail_records(agent, records)
    return finding.message[:500]


def clear_consecutive_failure_segment(
    agent: object,
    tool_name: str,
    failure_class: str,
) -> None:
    """清掉某工具某失败类的尾部连续段,使计数从 0 重新累计。

    收口后调用:模型换策略重新尝试同一工具时,不应因为历史段未清而
    一碰就再次收口。边界规则与 consecutive_same_failure_count 一致
    (同工具成功/不同失败类/guardrail 自身记录都不在段内)。
    """
    records = list(tool_guardrail_records(agent))
    if not records:
        return
    drop_indices: set[int] = set()
    for i in range(len(records) - 1, -1, -1):
        record = records[i]
        if str(record.get("tool_name") or "") != tool_name:
            continue
        current_class = str(record.get("failure_class") or "")
        if current_class.startswith("code:TOOL_GUARDRAIL"):
            continue
        if record.get("failed") is not True or current_class != failure_class:
            break
        drop_indices.add(i)
    if not drop_indices:
        return
    remaining = [r for i, r in enumerate(records) if i not in drop_indices]
    _set_tool_guardrail_records(agent, tuple(remaining))


# LLM: 原文摘要与宿主进展摘要分开；后者只供软提醒，不参与动作门；UNKNOWN/回放不计成功。
# 函数用途: 从真实工具结果提取安全分类和执行事实，避免未执行拦截被当成进展。
def _facts_from_result(
    runtime_params: object,
    call: ToolCall,
    result: ToolResult,
) -> ToolGuardrailFacts:
    effect = _resolved_effect(runtime_params, call, result)
    is_readonly = effect == "read_only"
    output_hash = _observation_result_hash(result) if result.ok else ""
    details = result.metadata.get("handler_details")
    progress = details.get("progress_observation") if isinstance(details, dict) else None
    progress = progress if isinstance(progress, dict) else {}
    progress_hash = progress.get("sha256")
    if not isinstance(progress_hash, str) or len(progress_hash) != 64 or not all(c in "0123456789abcdef" for c in progress_hash):
        progress_hash = ""
    operation = result.operation
    observed_success = (
        result.ok and result.handler_executed and result.effect_outcome in {"", "confirmed"}
        and (operation is None or (
            operation.status == "succeeded" and not operation.replayed
            and operation.handler_executed
            and operation.effect_outcome in {"", "confirmed"}
        ))
    )
    return ToolGuardrailFacts(
        tool_name=call.tool_name,
        args_hash=call.args_hash,
        failed=not result.ok,
        result_hash=output_hash,
        is_readonly=is_readonly,
        failure_class=_failure_class(result) if not result.ok else "",
        observed_success=observed_success,
        run_id=call.run_id,
        handler_executed=result.handler_executed,
        progress_hash=progress_hash,
        progress_pending=bool(progress_hash) and progress.get("pending") is True,
    )


# LLM: raw_output_sha256 由执行器/归档器生成而非模型字段；直接合同调用无摘要时才从正文计算。
# 函数用途: 比较完整工具结果而不是带随机归档地址的预览，避免相同输出漏报或相同截断预览误报。
def _observation_result_hash(result: ToolResult) -> str:
    digest = result.metadata.get("raw_output_sha256")
    if isinstance(digest, str) and len(digest) == 64 and all(c in "0123456789abcdef" for c in digest):
        return "sha256:" + digest
    return result_hash_for_guardrail(result.output)


def _resolved_effect(
    runtime_params: object,
    call: ToolCall,
    result: ToolResult,
) -> str:
    decision = result.metadata.get("action_decision")
    if isinstance(decision, dict):
        effect = str(decision.get("resolved_effect") or "").strip().lower()
        if effect in {"read_only", "mutating", "dangerous"}:
            return effect
    snapshot = getattr(runtime_params, "tool_runtime_snapshot", None)
    runtime = snapshot.runtime(call.tool_name) if snapshot is not None else None
    if runtime is None:
        return "dangerous"
    return tool_effect_for_runtime_policy(runtime.runtime_policy, call.arguments)


def _facts_for_next_hint(facts: ToolGuardrailFacts) -> ToolGuardrailFacts:
    return ToolGuardrailFacts(
        tool_name=facts.tool_name,
        args_hash=facts.args_hash,
        result_hash=facts.result_hash,
        is_readonly=facts.is_readonly,
        failure_class=facts.failure_class,
    )


def _set_tool_guardrail_records(
    agent: object,
    records: tuple[dict[str, object], ...],
) -> None:
    setattr(agent, _RECORDS_ATTR, records[-_MAX_RECORDS:])


def _failure_class(result: ToolResult) -> str:
    return failure_class_of_result(result)


__all__ = [
    "record_tool_guard_observation",
    "tool_guardrail_policy",
    "tool_guardrail_records",
]
