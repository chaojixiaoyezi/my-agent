
# LLM: 工具回执、文件引用及删除记录进入同一 exact run 账本；不从正文提取路径或完成状态。
# 模块用途: 归档工具输出与文件交接，供父子代理继续工作和恢复使用，不代写业务文件。
from __future__ import annotations

from pathlib import Path

from ..artifacts.registry import (
    ARTIFACT_ROLE_METADATA_KEY,
    ARTIFACT_ROLE_TOOL_OUTPUT_ARCHIVE,
    SHELL_PREIMAGE_POLICY_EXCLUDE,
    SHELL_PREIMAGE_POLICY_METADATA_KEY,
    ArtifactRegistration,
    register_artifact,
)
from ..memory_archive import ExternalizeToolOutputRequest, externalize_tool_output_record
from ..settings.defaults import default_config_int
from ..tooling.executor import ToolOutputProjection
from ..tooling.models import ToolHandlerOutcome, output_policy_for_outcome
from ..tooling.output_projection import project_tool_output_body
from ..tooling.runtime_contracts import ToolCall, ToolContentBlock, ToolResultRef
from .run_task_workspace_writer import (
    current_run_task_workspace_root,
    current_run_tool_output_archive_root,
)
from .runtime.owner_roots import runtime_owner_root
from .tool_loop.recovery import runtime_run_scope
from .tool_loop.round_execution import ToolCallRecordParams
from .tool_output_failsafe import write_tool_output_fail_safe_checkpoint

_MODEL_SUMMARY_MAX_CHARS = 12_000


# LLM: 原始输出先按 owner/run 及实际 ToolCall 的 attempt/turn 归档；仅真正外置且未声明保留正文的结果使用预览。
# 读取器已分页的正文、游标及文件版本必须完整进入 canonical ToolResult；同步检查 reducer、原生历史及外置输出回归。
# 函数用途: 按本次工具调用的真实身份写入归档并生成安全的模型结果；日志预览不能代替有界正文。
def archive_tool_output_projection(
    agent: object,
    params: object,
    call: ToolCall,
    outcome: ToolHandlerOutcome,
    *,
    model_call: ToolCall | None = None,
) -> ToolOutputProjection:
    """Archive the raw handler body before any model-facing projection is applied."""

    runtime_snapshot = getattr(params, "tool_runtime_snapshot", None)
    runtime = runtime_snapshot.runtime(call.tool_name) if runtime_snapshot is not None else None
    output_policy = (
        output_policy_for_outcome(runtime.runtime_policy, outcome)
        if runtime is not None
        else None
    )
    trust = output_policy.trust if output_policy is not None else "runtime"
    redaction = output_policy.redaction if output_policy is not None else "default"
    envelope = _archive_result_envelope(outcome.result_envelope, outcome)
    policy = (
        dict(envelope.get("tool_output_policy") or {})
        if isinstance(envelope.get("tool_output_policy"), dict)
        else {}
    )
    policy.update({"trust": trust, "redaction": redaction})
    envelope["tool_output_policy"] = policy
    request = ExternalizeToolOutputRequest(
        root=_tool_output_archive_root(agent, params),
        tool=call.tool_name,
        call_id=call.call_id,
        output=outcome.output,
        ok=outcome.ok,
        error_code=str(outcome.error_code or ""),
        reported_error_code=str(outcome.reported_error_code or ""),
        request_id=str(getattr(params, "request_id", "") or ""),
        conversation_request_id=_conversation_request_id(params),
        run_id=call.run_id,
        attempt_id=call.attempt_id,
        turn_id=call.turn_id,
        task_id=str(getattr(params, "task_id", "") or ""),
        min_chars=_config_int(agent, "tool_output_externalize_min_chars"),
        preview_chars=_config_int(agent, "tool_output_preview_chars"),
        parameters=dict(call.arguments),
        model_parameters=dict((model_call or call).arguments),
        result_envelope=envelope,
    )
    output_record = externalize_tool_output_record(request)
    output_record.update(write_tool_output_fail_safe_checkpoint(request))
    # 读取工具已限制单页大小，output_externalized=false 是归档层的明确内联裁决。
    # 日志 output_preview 无论是否外置都会缩短，不能用它作为所有模型回执的正文。
    use_preview = bool(output_record.get("output_externalized")) and not bool(
        policy.get("preserve_prompt_output")
    )
    body = project_tool_output_body(
        tool=call.tool_name,
        output=output_record.get("output_preview", "") if use_preview else outcome.output,
        trust=trust,
        redaction=redaction,
    )
    refs = _projection_refs(output_record, outcome)
    blocks: list[ToolContentBlock] = []
    if body:
        blocks.append(ToolContentBlock("text", text=body))
    blocks.extend(ToolContentBlock("ref", ref=ref.ref) for ref in refs)
    return ToolOutputProjection(
        content_blocks=tuple(blocks),
        refs=refs,
        metadata={
            "archive_output_record": output_record,
            "raw_output_chars": len(str(outcome.output or "")),
            "raw_output_bytes": int(output_record.get("output_size_bytes") or 0),
            "raw_output_sha256": str(output_record.get("output_hash") or ""),
            "projection_truncated": use_preview,
        },
    )


def _projection_refs(
    output_record: dict[str, object],
    outcome: ToolHandlerOutcome,
) -> tuple[ToolResultRef, ...]:
    refs: list[ToolResultRef] = []
    seen: set[str] = set()

    def append(kind: object, value: object, *, summary: object = "") -> None:
        ref = str(value or "").strip()
        if not ref or ref in seen:
            return
        seen.add(ref)
        refs.append(
            ToolResultRef(
                kind=str(kind or "artifact"),
                ref=ref,
                sha256=str(output_record.get("output_hash") or ""),
                size_bytes=int(output_record.get("output_size_bytes") or 0),
                summary=str(summary or "").strip(),
            )
        )

    append(
        "tool_output",
        output_record.get("output_path")
        or output_record.get("artifact_ref")
        or output_record.get("source_artifact_ref"),
        summary="complete raw tool output",
    )
    envelope = outcome.result_envelope if isinstance(outcome.result_envelope, dict) else {}
    for key in ("tool_result_refs", "artifact_refs", "output_refs"):
        values = envelope.get(key)
        for item in values if isinstance(values, list) else ():
            if not isinstance(item, dict):
                continue
            append(
                item.get("kind") or "artifact",
                item.get("ref") or item.get("path") or item.get("uri"),
                summary=item.get("summary"),
            )
    return tuple(refs)


# LLM: The canonical archive row must reuse output already externalized with the executed call's
# identity; direct callers must persist that same identity before index creation.
# 函数用途: 汇总一次工具调用的耐久记录；未缓存输出也按实际调用身份写入产物与索引。
def archive_tool_call_record(agent: object, record: ToolCallRecordParams) -> dict[str, object]:
    call_id = record.call.call_id
    cached = record.result.metadata.get("archive_output_record")
    if isinstance(cached, dict):
        output_record = dict(cached)
    else:
        request = ExternalizeToolOutputRequest(
            root=_tool_output_archive_root(agent, record.params),
            tool=record.result.tool_name,
            call_id=call_id,
            output=record.result.output,
            ok=record.result.ok,
            error_code=record.result.error_code,
            reported_error_code=record.result.reported_error_code,
            request_id=record.params.request_id,
            conversation_request_id=_conversation_request_id(record.params),
            run_id=record.call.run_id,
            attempt_id=record.call.attempt_id,
            turn_id=record.call.turn_id,
            task_id=record.params.task_id,
            min_chars=_config_int(agent, "tool_output_externalize_min_chars"),
            preview_chars=_config_int(agent, "tool_output_preview_chars"),
            parameters=dict(record.call.arguments),
            model_parameters=dict(record.model_visible_call.arguments),
            result_envelope=_archive_result_envelope(
                _result_details(record.result),
                record.result,
            ),
        )
        output_record = externalize_tool_output_record(request)
        output_record.update(write_tool_output_fail_safe_checkpoint(request))
    output_record["parameters"] = dict(record.call.arguments)
    output_record["model_parameters"] = dict(record.model_visible_call.arguments)
    # The round/index pair is runtime authority for ephemeral tool discovery:
    # a tool loaded by tool_search is pending only until the next model turn.
    # Persist it on the carried record so a compact continuation can distinguish
    # an unconsumed search from a search consumed by later rounds.
    output_record["tool_round"] = max(0, int(record.tool_rounds or 0))
    output_record["tool_index"] = max(0, int(record.idx or 0))
    output_record["source_protocol"] = record.call.source_protocol
    output_record["schema_hash"] = record.call.schema_hash
    output_record["turn_id"] = record.call.turn_id
    output_record["attempt_id"] = record.call.attempt_id
    output_record["required_action_id"] = record.call.required_action_id
    output_record["operation_id"] = record.call.operation_id
    output_record["idempotency_key"] = record.call.idempotency_key
    output_record["execution_states"] = list(record.execution_states)
    _attach_run_scope(output_record, agent, record)
    _attach_gate_and_refs(output_record, record.result)
    _attach_model_summary(output_record, record.result)
    _register_tool_result_artifacts(agent, output_record, record)
    return output_record


def _attach_model_summary(output_record: dict[str, object], result: object) -> None:
    """Persist the same bounded structured projection used by the live model."""

    from ..tooling.output_projection import (
        redact_tool_output_text,
        tool_output_projection_policy,
    )

    details = _result_details(result)
    trust, redaction = tool_output_projection_policy(details)
    if trust == "external_data":
        return
    from .orchestration.context.live_summary import orchestration_live_summary
    from .tool_context.action_summary import actionable_tool_result_summary

    policy = details.get("tool_output_policy")
    live_prompt_output = (
        str(policy.get("live_prompt_output") or "").strip()
        if isinstance(policy, dict)
        else ""
    )
    summary = live_prompt_output or orchestration_live_summary(result, output_record)
    if not summary:
        summary = actionable_tool_result_summary(result, output_record)
    if not summary:
        return
    redacted = redact_tool_output_text(summary, redaction=redaction)
    output_record["model_summary"] = _bounded_model_summary(redacted)


def _bounded_model_summary(value: str) -> str:
    if len(value) <= _MODEL_SUMMARY_MAX_CHARS:
        return value
    marker = "\n...[middle model summary truncated]...\n"
    budget = _MODEL_SUMMARY_MAX_CHARS - len(marker)
    head = int(budget * 0.6)
    tail = budget - head
    return f"{value[:head]}{marker}{value[-tail:]}"


def _config_int(agent: object, key: str) -> int:
    try:
        return int(getattr(agent.config, key))
    except (AttributeError, TypeError, ValueError):
        return default_config_int(key)


# LLM: Delegate to the per-run cached root; recomputing from a newly promoted task would orphan
# refs written earlier in the same active turn.
# 函数用途: 返回本轮固定不漂移的工具输出归档目录。
def _tool_output_archive_root(agent: object, params: object) -> Path:
    return current_run_tool_output_archive_root(agent, params)


# LLM: Tool archive rows need the originating ordinary conversation turn even when a
# background slice has a different request_id; task_id is a durable task, not a turn id.
# 函数用途: 从结构化运行参数读取工具所属用户回合编号，旧调用方缺字段时回退当前请求编号。
def _conversation_request_id(params: object) -> str:
    attributes = getattr(params, "task_attributes", None)
    if isinstance(attributes, dict):
        from ..conversation.authority import CONVERSATION_REQUEST_ID_ATTR

        value = str(attributes.get(CONVERSATION_REQUEST_ID_ATTR) or "").strip()
        if value:
            return value
    return str(getattr(params, "request_id", "") or "").strip()


def _attach_gate_and_refs(output_record: dict[str, object], result: object) -> None:
    output_record.update(_tool_execution_facts_from_result(result))
    approval = _applied_tool_approval_from_result(result)
    if approval:
        output_record["tool_approval"] = approval
        output_record["approval_id"] = approval["permission_id"]
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


# LLM: Durable approval evidence may only come from the typed host-owned ToolResult field.
# Generic handler metadata and output prose are deliberately ignored to prevent forged approval facts.
# 函数用途: 把已经应用到同一次调用的用户批准写入工具账本，并给运行时门账本提供批准编号。
def _applied_tool_approval_from_result(result: object) -> dict[str, object]:
    approval = getattr(result, "applied_approval", None)
    if approval is None or not hasattr(approval, "to_dict"):
        return {}
    value = approval.to_dict()
    return dict(value) if isinstance(value, dict) else {}


# LLM: 实际文件和显式已删除引用复用原 registry；删除须文件当前不存在，同一次回执同一路径状态只落一次。
# 函数用途: 登记工具返回的文件引用，并给完整工具回执标明“可读取但不属于用户交付物”的结构化角色。
def _register_tool_result_artifacts(
    agent: object,
    output_record: dict[str, object],
    record: ToolCallRecordParams,
) -> None:
    refs = output_record.get("tool_result_refs")
    if not isinstance(refs, list):
        return
    scope = output_record.get("run_scope")
    scope = scope if isinstance(scope, dict) else {}
    workspace_root = _artifact_registry_root(agent, record.params)
    seen: set[tuple[str, str]] = set()
    for ref in refs:
        if not isinstance(ref, dict):
            continue
        deleted = ref.get("status") == "deleted"
        path = _existing_file_ref(ref.get("ref") or ref.get("path"), allow_deleted=deleted)
        if path is None:
            continue
        status = "deleted" if deleted and not path.exists() else ""
        identity = (str(path), status)
        if identity in seen:
            continue
        seen.add(identity)
        kind = str(ref.get("kind") or "")
        metadata = {"call_id": str(output_record.get("call_id") or "")}
        if kind == "tool_output":
            metadata.update(
                {
                    ARTIFACT_ROLE_METADATA_KEY: ARTIFACT_ROLE_TOOL_OUTPUT_ARCHIVE,
                    SHELL_PREIMAGE_POLICY_METADATA_KEY: SHELL_PREIMAGE_POLICY_EXCLUDE,
                }
            )
        registered = register_artifact(
            ArtifactRegistration(
                workspace_root=workspace_root,
                path=path,
                run_id=str(scope.get("run_id") or record.params.run_id or ""),
                task_id=str(scope.get("task_id") or record.params.task_id or ""),
                agent_id=str(scope.get("owner_id") or scope.get("run_id") or record.params.run_id or ""),
                kind=kind,
                source="tool_result",
                created_by_tool=record.result.tool_name,
                status=status,
                metadata=metadata,
            )
        )
        ref["artifact_id"] = registered.artifact_id
        output_record.setdefault("artifact_registry_refs", [])
        if isinstance(output_record["artifact_registry_refs"], list):
            output_record["artifact_registry_refs"].append(registered.to_dict())


def _artifact_registry_root(agent: object, params: object) -> Path:
    task_root = current_run_task_workspace_root(agent, params)
    if task_root is not None:
        return task_root
    return runtime_owner_root(agent)


# LLM: 删除记录只接受绝对路径且当前确实缺失；普通引用继续要求实际文件，不把失败正文当文件。
# 函数用途: 检查要登记的文件路径，允许显式删除墓碑更新旧交接记录。
def _existing_file_ref(value: object, *, allow_deleted: bool = False) -> Path | None:
    text = str(value or "").strip()
    if not text or "://" in text:
        return None
    try:
        path = Path(text).expanduser()
        if allow_deleted and not path.is_absolute():
            return None
        path = path.resolve(strict=False)
    except OSError:
        return None
    return path if path.is_file() or (allow_deleted and not path.exists() and not path.is_symlink()) else None


def _attach_run_scope(output_record: dict[str, object], agent: object, record: ToolCallRecordParams) -> None:
    scope = _scope_from_result(record.result) or runtime_run_scope(agent, record.params).to_dict()
    output_record["run_scope"] = scope
    _copy_text_fact(output_record, "parent_run_id", scope.get("parent_run_id"))
    _copy_text_fact(output_record, "root_run_id", scope.get("root_run_id"))
    _copy_text_fact(output_record, "root_task_id", scope.get("root_task_id"))
    output_record["depth"] = _int_value(scope.get("depth"))
    _copy_text_fact(output_record, "agent_kind", scope.get("agent_kind"))


def _result_details(result: object) -> dict[str, object]:
    """Read handler details from canonical ToolResult metadata."""

    metadata = getattr(result, "metadata", None)
    if isinstance(metadata, dict):
        details = metadata.get("handler_details")
        if isinstance(details, dict):
            return dict(details)
    return {}


# LLM: This envelope is the sole pre-externalization lifecycle projection. It may copy typed
# ToolResult/ToolHandlerOutcome fields, but must never infer execution or operation state from output text.
# 函数用途: 给耐久工具索引补齐宿主确认的执行层级和副作用操作终态，供后台续接原样恢复。
def _archive_result_envelope(
    value: object,
    result: object,
) -> dict[str, object]:
    envelope = dict(value) if isinstance(value, dict) else {}
    envelope["tool_execution"] = _tool_execution_facts_from_result(result)
    operation_value = getattr(result, "operation", None)
    if operation_value is not None and hasattr(operation_value, "to_dict"):
        typed_operation = operation_value.to_dict()
        operation = (
            dict(envelope.get("tool_operation") or {})
            if isinstance(envelope.get("tool_operation"), dict)
            else {}
        )
        for key in (
            "operation_id",
            "result_ref",
            "status",
            "replayed",
        ):
            if key not in operation and key in typed_operation:
                operation[key] = typed_operation[key]
        if operation:
            envelope["tool_operation"] = operation
    return envelope


def _scope_from_result(result: object) -> dict[str, object]:
    scope = _result_details(result).get("scope")
    return dict(scope) if isinstance(scope, dict) else {}


def _runtime_gate_from_result(result: object) -> dict[str, object]:
    gate = _result_details(result).get("runtime_gate")
    if isinstance(gate, dict):
        return dict(gate)
    approval = _applied_tool_approval_from_result(result)
    if not approval:
        return {}
    return {
        "gate": "tool_approval",
        "status": "APPROVED",
        "allowed": True,
        "source": str(approval.get("schema_version") or "tool_approval_result.v1"),
        "evidence": {"approval_id": str(approval.get("permission_id") or "")},
    }


def _error_facts_from_result(result: object) -> dict[str, object]:
    facts: dict[str, object] = {}
    for key in ("error_code", "reported_error_code", "error_category", "recommended_action", "recovery_hint"):
        _copy_text_fact(facts, key, getattr(result, key, ""))
    retryable = getattr(result, "retryable", None)
    if retryable is not None:
        facts["retryable"] = bool(retryable)
    return facts


# LLM: archive 的执行层级只能投影 typed result，不能从 output_preview 或错误文案反推。
# 函数用途: 让成功、门前拒绝和 handler 后失败都能按同一字段快速定位。
def _tool_execution_facts_from_result(result: object) -> dict[str, object]:
    facts: dict[str, object] = {
        "handler_executed": bool(getattr(result, "handler_executed", False)),
        "duration_ms": max(0, _int_value(getattr(result, "duration_ms", 0))),
    }
    _copy_text_fact(facts, "failure_stage", getattr(result, "failure_stage", ""))
    return facts


# LLM: archive 顶层的操作事实来自 typed result/envelope；不得从 output 正文解析状态。
# 函数用途: 提取 compact、重启续跑和审计都需要的操作终态与副作用引用。
def _operation_facts_from_result(result: object) -> dict[str, object]:
    details = _result_details(result)
    facts: dict[str, object] = {}
    _copy_text_fact(facts, "effect_outcome", getattr(result, "effect_outcome", ""))
    _copy_text_fact(facts, "effect_source_ref", getattr(result, "effect_source_ref", ""))
    operation_value = getattr(result, "operation", None)
    if operation_value is not None and hasattr(operation_value, "to_dict"):
        operation = operation_value.to_dict()
        _copy_text_fact(facts, "operation_id", operation.get("operation_id"))
        _copy_text_fact(facts, "result_ref", operation.get("result_ref"))
        _copy_text_fact(facts, "idempotency_key", operation.get("idempotency_key"))
        _copy_text_fact(facts, "tool_operation_status", operation.get("status"))
        facts["tool_operation_replayed"] = operation.get("replayed") is True
        facts["tool_operation_attempt_count"] = max(
            0, _int_value(operation.get("attempt_count"))
        )
    _copy_text_fact(facts, "operation_id", details.get("operation_id"))
    operation = details.get("tool_operation")
    if isinstance(operation, dict):
        _copy_text_fact(facts, "operation_id", operation.get("operation_id"))
        _copy_text_fact(facts, "result_ref", operation.get("result_ref"))
        _copy_text_fact(facts, "tool_operation_status", operation.get("status"))
        _copy_text_fact(facts, "tool_operation_action", operation.get("action"))
        _copy_text_fact(
            facts,
            "tool_operation_idempotency_scope",
            operation.get("idempotency_scope"),
        )
        _copy_text_fact(
            facts,
            "tool_operation_reconciliation_source_ref",
            operation.get("reconciliation_source_ref"),
        )
        if "replayed" in operation:
            facts["tool_operation_replayed"] = operation.get("replayed") is True
    return facts


def _copy_text_fact(target: dict[str, object], key: str, value: object) -> None:
    text = str(value or "").strip()
    if text:
        target[key] = text


def _int_value(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _tool_result_refs_from_result(result: object) -> list[dict[str, object]]:
    typed_refs = getattr(result, "refs", ()) or ()
    refs = [
        item.to_dict()
        for item in typed_refs
        if hasattr(item, "to_dict")
    ]
    details = _result_details(result)
    _append_refs(refs, details)
    output = details.get("output")
    if isinstance(output, dict):
        _append_refs(refs, output)
    return refs


# LLM: 归档白名单可保留参数来源/类型/摘要，但绝不能复制原参数值或任意私有 result envelope。
# 函数用途: 压缩工具结果中恢复与收口所需的安全结构化事实，忽略未明确登记的实现私有字段。
def _compact_result_envelope(result: object) -> dict[str, object]:
    envelope = _result_details(result)
    if not envelope:
        return {}
    keys = (
        "artifact_ref",
        "source_ref",
        "path",
        "target_path",
        "output_path",
        "session_id",
        "action",
        "status",
        "read_window",
        "page_window",
        "schedule_lifecycle",
        "verification_evidence",
        "verification_state",
        "tool_search",
        "input_sources",
        "input_coercions",
        "input_facts",
    )
    compact = {key: envelope[key] for key in keys if key in envelope}
    artifact_integrity = _compact_artifact_integrity(envelope.get("artifact_integrity"))
    if artifact_integrity:
        compact["artifact_integrity"] = artifact_integrity
    delivery_evidence = _compact_delivery_evidence(envelope.get("delivery_evidence"))
    if delivery_evidence:
        compact["delivery_evidence"] = delivery_evidence
    tool_operation = _compact_tool_operation(envelope.get("tool_operation"))
    if tool_operation:
        compact["tool_operation"] = tool_operation
    reported_result = _compact_reported_tool_result(envelope.get("reported_tool_result"))
    if reported_result:
        compact["reported_tool_result"] = reported_result
    execution = _compact_tool_execution(envelope.get("tool_execution"))
    if execution:
        compact["tool_execution"] = execution
    output = envelope.get("output")
    if isinstance(output, dict):
        compact["output"] = {key: output[key] for key in keys if key in output}
    return compact


# LLM: compact 只携带执行生命周期字段；诊断详情和任意扩展字段不能借 envelope 越过归档白名单。
# 函数用途: 保存操作身份、终态、动作与核对引用，让重启后的模型仍能区分成功、失败和未知。
def _compact_tool_operation(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    compact: dict[str, object] = {}
    for key in (
        "schema_version",
        "operation_id",
        "status",
        "action",
        "idempotency_scope",
        "reconciliation_source_ref",
    ):
        _copy_text_fact(compact, key, value.get(key))
    if "replayed" in value:
        compact["replayed"] = value.get("replayed") is True
    original_execution = _compact_tool_execution(value.get("original_tool_execution"))
    if original_execution:
        compact["original_tool_execution"] = original_execution
    return compact


# LLM: 原工具报告是 unknown 的旁证而非权威终态；只保留布尔、错误码和副作用引用。
# 函数用途: 在不复制工具正文的前提下，解释为何系统把一次表面成功或超时降级为 unknown。
def _compact_reported_tool_result(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    compact: dict[str, object] = {}
    if "ok" in value:
        compact["ok"] = value.get("ok") is True
    for key in (
        "error_code",
        "reported_error_code",
        "effect_outcome",
        "effect_source_ref",
        "failure_stage",
    ):
        _copy_text_fact(compact, key, value.get(key))
    if "handler_executed" in value:
        compact["handler_executed"] = value.get("handler_executed") is True
    if "duration_ms" in value:
        compact["duration_ms"] = max(0, _int_value(value.get("duration_ms")))
    return compact


def _compact_tool_execution(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    compact: dict[str, object] = {
        "handler_executed": value.get("handler_executed") is True,
        "duration_ms": max(0, _int_value(value.get("duration_ms"))),
    }
    _copy_text_fact(compact, "failure_stage", value.get("failure_stage"))
    return compact


def _compact_delivery_evidence(value: object) -> dict[str, object]:
    """Keep only the typed current-owner delivery facts needed by finalization."""
    if not isinstance(value, dict):
        return {}
    if str(value.get("delivery_status") or "").strip().lower() != "sent":
        return {}
    if value.get("source_owner_delivery") is not True:
        return {}
    evidence: dict[str, object] = {
        "schema_version": str(value.get("schema_version") or "message_tool_delivery.v1"),
        "delivery_status": "sent",
        "source_owner_delivery": True,
        "channel": str(value.get("channel") or ""),
        "content": str(value.get("content") or ""),
        "receipt_id": str(value.get("receipt_id") or ""),
        "deduplicated": value.get("deduplicated") is True,
    }
    attachments = value.get("attachments")
    if isinstance(attachments, list):
        evidence["attachments"] = [
            {
                key: item[key]
                for key in (
                    "artifact_id",
                    "path",
                    "name",
                    "kind",
                    "sha256",
                    "size_bytes",
                    "ok",
                )
                if key in item
            }
            for item in attachments
            if isinstance(item, dict)
        ]
    refs = value.get("evidence_refs")
    if isinstance(refs, list):
        # Preserve only typed references: the archive may prove which durable
        # records supported a delivery, but it must not copy their payloads.
        evidence["evidence_refs"] = [
            str(item)
            for item in refs
            if isinstance(item, str) and str(item).strip()
        ]
    return evidence


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


# LLM: 仅消费工具结构化 refs；保留删除状态给 registry，不通过 files_modified 展示名猜物理路径。
# 函数用途: 汇入多文件补丁等工具的精确引用，同时保留已有单文件回执格式。
def _append_refs(refs: list[dict[str, object]], payload: dict[str, object]) -> None:
    for key in ("tool_result_refs", "artifact_refs", "output_refs"):
        values = payload.get(key)
        if isinstance(values, list):
            refs.extend(dict(item) for item in values if isinstance(item, dict))
    for key in ("artifact_ref", "source_ref", "path", "target_path", "output_path"):
        for value in _ref_values(payload.get(key)):
            _append_ref(refs, key, value)


def _ref_values(value: object) -> list[object]:
    if not isinstance(value, dict):
        return [value]
    return [value.get(key) for key in ("resolved", "raw", "path", "artifact_ref")]


def _append_ref(refs: list[dict[str, object]], kind: str, value: object) -> None:
    text = str(value or "").strip()
    if text:
        refs.append({"kind": kind, "path": text})


__all__ = ["archive_tool_call_record"]
