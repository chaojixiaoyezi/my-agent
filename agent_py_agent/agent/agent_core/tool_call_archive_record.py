# LLM: Tool call archive records keep replayable tool evidence compact and structured.
# 模块用途: 生成 archive_tool_calls 记录，并保留 runtime_gate、工具产物 ref 和小型结果 envelope 供 closeout/replay 使用。

from __future__ import annotations

from ..memory_archive import ExternalizeToolOutputRequest, externalize_tool_output_record
from .tool_loop_recovery import runtime_run_id
from .tool_output_failsafe import write_tool_output_fail_safe_checkpoint
from .tool_round_execution import ToolCallRecordParams


# LLM: archive_tool_call_record externalizes output and attaches replayable gate/provenance facts.
# 函数用途: 从一次工具结果生成可归档记录，确保产物来源和 runtime_gate 不丢失。
def archive_tool_call_record(agent: object, record: ToolCallRecordParams) -> dict[str, object]:
    call_id = f"{record.tool_rounds}-{record.idx}"
    request = ExternalizeToolOutputRequest(
        root=agent.root,
        tool=record.result.tool,
        call_id=call_id,
        output=record.result.output,
        ok=record.result.ok,
        request_id=record.params.request_id,
        run_id=runtime_run_id(agent, record.params),
        task_id=record.params.task_id,
        min_chars=_config_int(agent, "tool_output_externalize_min_chars"),
        preview_chars=_config_int(agent, "tool_output_preview_chars"),
    )
    output_record = externalize_tool_output_record(request)
    output_record.update(write_tool_output_fail_safe_checkpoint(request))
    output_record["parameters"] = record.payload
    _attach_gate_and_refs(output_record, record.result)
    return output_record


# LLM: _config_int reads archive-related tool budgets from AgentConfig.
# 函数用途: 读取工具输出外置和 preview 预算，非法值只回退到配置 schema 默认。
def _config_int(agent: object, key: str) -> int:
    try:
        return int(getattr(agent.config, key))
    except (AttributeError, TypeError, ValueError):
        from ..settings.config import AgentConfig

        return int(getattr(AgentConfig(), key))


# LLM: _attach_gate_and_refs copies small structured result facts into the archive row.
# 函数用途: 将 runtime_gate、tool_result_refs 和 compact envelope 附到归档记录，不复制大正文。
def _attach_gate_and_refs(output_record: dict[str, object], result: object) -> None:
    runtime_gate = _runtime_gate_from_result(result)
    if runtime_gate:
        output_record["runtime_gate"] = runtime_gate
    error_facts = _error_facts_from_result(result)
    if error_facts:
        output_record.update(error_facts)
    operation_facts = _operation_facts_from_result(result)
    if operation_facts:
        output_record.update(operation_facts)
    result_refs = _tool_result_refs_from_result(result)
    if result_refs:
        output_record["tool_result_refs"] = result_refs
    result_envelope = _compact_result_envelope(result)
    if result_envelope:
        output_record["tool_result_envelope"] = result_envelope


# LLM: _runtime_gate_from_result carries runtime gate evidence into replayable archives.
# 函数用途: 只读取 ToolExecutionResult.result_envelope.runtime_gate 结构字段，不从输出文本推断。
def _runtime_gate_from_result(result: object) -> dict[str, object]:
    envelope = getattr(result, "result_envelope", None)
    if not isinstance(envelope, dict):
        return {}
    gate = envelope.get("runtime_gate")
    return dict(gate) if isinstance(gate, dict) else {}


# LLM: _error_facts_from_result carries error taxonomy fields into replayable archives.
# 函数用途: 归档失败工具的错误码和恢复建议，供 final gate/replay 只读机器字段判断。
def _error_facts_from_result(result: object) -> dict[str, object]:
    facts: dict[str, object] = {}
    for key in ("error_code", "error_category", "recommended_action", "recovery_hint"):
        _copy_text_fact(facts, key, getattr(result, key, ""))
    retryable = getattr(result, "retryable", None)
    if retryable is not None:
        facts["retryable"] = bool(retryable)
    return facts


# LLM: _operation_facts_from_result copies replay identity from the typed result envelope.
# 函数用途: 将 operation_id/idempotency_key 这些小型机器字段放入 archive 行。
def _operation_facts_from_result(result: object) -> dict[str, object]:
    envelope = getattr(result, "result_envelope", None)
    if not isinstance(envelope, dict):
        return {}
    facts: dict[str, object] = {}
    _copy_text_fact(facts, "operation_id", envelope.get("operation_id"))
    protocol = envelope.get("tool_protocol_v2")
    if isinstance(protocol, dict):
        output_record_protocol = {
            "operation_id": protocol.get("operation_id"),
            "idempotency_key": protocol.get("idempotency_key"),
        }
        _copy_text_fact(facts, "operation_id", output_record_protocol["operation_id"])
        _copy_text_fact(facts, "idempotency_key", output_record_protocol["idempotency_key"])
        facts["tool_protocol_v2"] = output_record_protocol
    return facts


# LLM: _copy_text_fact preserves only non-empty scalar operation fields.
# 函数用途: 避免空字符串覆盖已经存在的结构化 identity。
def _copy_text_fact(target: dict[str, object], key: str, value: object) -> None:
    text = str(value or "").strip()
    if text:
        target[key] = text


# LLM: _tool_result_refs_from_result extracts artifact/path refs from structured tool envelopes.
# 函数用途: 将工具产物来源写入 archive_tool_calls，供 closeout 按机器 ref 证明当前 run 生成了产物。
def _tool_result_refs_from_result(result: object) -> list[dict[str, object]]:
    envelope = getattr(result, "result_envelope", None)
    if not isinstance(envelope, dict):
        return []
    refs: list[dict[str, object]] = []
    _append_refs(refs, envelope)
    output = envelope.get("output")
    if isinstance(output, dict):
        _append_refs(refs, output)
    return refs


# LLM: _compact_result_envelope keeps path refs while avoiding full content copies.
# 函数用途: 给工具归档保留 target_path/artifact_ref 等小字段，不把大正文写进 archive 行。
def _compact_result_envelope(result: object) -> dict[str, object]:
    envelope = getattr(result, "result_envelope", None)
    if not isinstance(envelope, dict):
        return {}
    keys = ("artifact_ref", "source_ref", "path", "target_path", "output_path", "session_id", "action", "status")
    compact = {key: envelope[key] for key in keys if key in envelope}
    artifact_integrity = _compact_artifact_integrity(envelope.get("artifact_integrity"))
    if artifact_integrity:
        compact["artifact_integrity"] = artifact_integrity
    output = envelope.get("output")
    if isinstance(output, dict):
        compact["output"] = {key: output[key] for key in keys if key in output}
    return compact


# LLM: _compact_artifact_integrity keeps only small artifact integrity facts in tool archive rows.
# 函数用途: 裁剪 artifact integrity payload，避免归档里复制长 issue 或产物正文。
def _compact_artifact_integrity(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    keys = ("kind", "path", "ok", "blocker_codes", "warning_codes")
    compact = {key: value[key] for key in keys if key in value}
    issues = value.get("issues")
    if isinstance(issues, list):
        compact["issues"] = [
            {
                key: item[key]
                for key in ("code", "severity", "count", "examples")
                if isinstance(item, dict) and key in item
            }
            for item in issues[:12]
            if isinstance(item, dict)
        ]
    return compact


# LLM: _append_refs adds stable file refs from a structured envelope object.
# 函数用途: 收集 path/artifact_ref/source_ref/target_path 字段，避免从 output_preview 文本里猜路径。
def _append_refs(refs: list[dict[str, object]], payload: dict[str, object]) -> None:
    for key in ("artifact_ref", "source_ref", "path", "target_path", "output_path"):
        for value in _ref_values(payload.get(key)):
            _append_ref(refs, key, value)


# LLM: _ref_values normalizes scalar and nested target_path refs.
# 函数用途: 兼容 legacy session target_path 的 raw/resolved 对象形态，降低 refs 提取嵌套复杂度。
def _ref_values(value: object) -> list[object]:
    if not isinstance(value, dict):
        return [value]
    return [value.get(key) for key in ("resolved", "raw", "path", "artifact_ref")]


# LLM: _append_ref records one non-empty tool result reference.
# 函数用途: 统一 tool_result_refs 的 kind/path 形态，让 provenance gate 可直接消费。
def _append_ref(refs: list[dict[str, object]], kind: str, value: object) -> None:
    text = str(value or "").strip()
    if text:
        refs.append({"kind": kind, "path": text})


__all__ = ["archive_tool_call_record"]
