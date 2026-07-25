
from __future__ import annotations

from pathlib import Path

from ..artifacts.registry import ArtifactRegistration, register_artifact
from ..memory_archive import ExternalizeToolOutputRequest, externalize_tool_output_record
from ..settings.defaults import default_config_int
from .native_tool_protocol import native_tool_use_active
from .run_task_workspace_writer import current_run_task_work_dir, current_run_task_workspace_root
from .runtime.owner_roots import runtime_owner_root
from .tool_loop.recovery import runtime_run_id, runtime_run_scope
from .tool_loop.round_execution import ToolCallRecordParams
from .tool_output_failsafe import write_tool_output_fail_safe_checkpoint


def archive_tool_call_record(agent: object, record: ToolCallRecordParams) -> dict[str, object]:
    call_id = _tool_call_archive_call_id(agent, record)
    request = ExternalizeToolOutputRequest(
        root=_tool_output_archive_root(agent, record.params),
        tool=record.result.tool,
        call_id=call_id,
        output=record.result.output,
        ok=record.result.ok,
        error_code=str(getattr(record.result, "error_code", "") or ""),
        reported_error_code=str(getattr(record.result, "reported_error_code", "") or ""),
        request_id=record.params.request_id,
        run_id=runtime_run_id(agent, record.params),
        task_id=record.params.task_id,
        min_chars=_config_int(agent, "tool_output_externalize_min_chars"),
        preview_chars=_config_int(agent, "tool_output_preview_chars"),
        parameters=record.payload,
        result_envelope=getattr(record.result, "result_envelope", None),
    )
    output_record = externalize_tool_output_record(request)
    output_record.update(write_tool_output_fail_safe_checkpoint(request))
    output_record["parameters"] = record.payload
    _attach_run_scope(output_record, agent, record)
    _attach_gate_and_refs(output_record, record.result)
    _register_tool_result_artifacts(agent, output_record, record)
    return output_record


def _tool_call_archive_call_id(agent: object, record: ToolCallRecordParams) -> str:
    """选用这次工具记录的 call_id。

    text 协议：沿用合成 ``round-idx``（既有外置/锚点/账本口径不变）。
    native 协议：优先用模型返回的真实 provider tool_use id（``payload["call_id"]``，
    由 ``_flatten_tool_use_block`` 注入），使出站 tool_result 的 ``tool_use_id`` 能与
    assistant ``tool_use.id`` 配对；同时把它回写到 ``result.call_id``，让 IR 历史
    （``record_tool_call_ir``）拿到同一个真实 id。真实 id 缺失时回退合成 id，永不空。
    """
    synthetic = f"{record.tool_rounds}-{record.idx}"
    if not native_tool_use_active(agent):
        return synthetic
    provider_id = ""
    if isinstance(record.payload, dict):
        provider_id = str(record.payload.get("call_id") or "")
    if not provider_id:
        provider_id = str(getattr(record.result, "call_id", "") or "")
    call_id = provider_id or synthetic
    _stamp_result_call_id(record.result, call_id)
    return call_id


def _stamp_result_call_id(result: object, call_id: str) -> None:
    if not call_id or str(getattr(result, "call_id", "") or "") == call_id:
        return
    try:
        result.call_id = call_id  # type: ignore[attr-defined]
    except (AttributeError, TypeError):
        return


def _config_int(agent: object, key: str) -> int:
    try:
        return int(getattr(agent.config, key))
    except (AttributeError, TypeError, ValueError):
        return default_config_int(key)


def _tool_output_archive_root(agent: object, params: object) -> Path:
    work_dir = current_run_task_work_dir(agent, params)
    if work_dir is not None:
        return work_dir
    return runtime_owner_root(agent)


def _attach_gate_and_refs(output_record: dict[str, object], result: object) -> None:
    output_record.update(_tool_execution_facts_from_result(result))
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
    for ref in refs:
        if not isinstance(ref, dict):
            continue
        path = _existing_file_ref(ref.get("path"))
        if path is None:
            continue
        registered = register_artifact(
            ArtifactRegistration(
                workspace_root=workspace_root,
                path=path,
                run_id=str(scope.get("run_id") or record.params.run_id or ""),
                task_id=str(scope.get("task_id") or record.params.task_id or ""),
                agent_id=str(scope.get("owner_id") or scope.get("run_id") or record.params.run_id or ""),
                kind=str(ref.get("kind") or ""),
                source="tool_result",
                created_by_tool=str(record.result.tool or ""),
                metadata={"call_id": str(output_record.get("call_id") or "")},
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


def _existing_file_ref(value: object) -> Path | None:
    text = str(value or "").strip()
    if not text or "://" in text:
        return None
    try:
        path = Path(text).expanduser().resolve(strict=False)
    except OSError:
        return None
    return path if path.is_file() else None


def _attach_run_scope(output_record: dict[str, object], agent: object, record: ToolCallRecordParams) -> None:
    scope = _scope_from_result(record.result) or runtime_run_scope(agent, record.params).to_dict()
    output_record["run_scope"] = scope
    _copy_text_fact(output_record, "parent_run_id", scope.get("parent_run_id"))
    _copy_text_fact(output_record, "root_run_id", scope.get("root_run_id"))
    _copy_text_fact(output_record, "root_task_id", scope.get("root_task_id"))
    output_record["depth"] = _int_value(scope.get("depth"))
    _copy_text_fact(output_record, "agent_kind", scope.get("agent_kind"))


def _scope_from_result(result: object) -> dict[str, object]:
    envelope = getattr(result, "result_envelope", None)
    if not isinstance(envelope, dict):
        return {}
    scope = envelope.get("scope")
    return dict(scope) if isinstance(scope, dict) else {}


def _runtime_gate_from_result(result: object) -> dict[str, object]:
    envelope = getattr(result, "result_envelope", None)
    if not isinstance(envelope, dict):
        return {}
    gate = envelope.get("runtime_gate")
    return dict(gate) if isinstance(gate, dict) else {}


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
    envelope = getattr(result, "result_envelope", None)
    facts: dict[str, object] = {}
    _copy_text_fact(facts, "effect_outcome", getattr(result, "effect_outcome", ""))
    _copy_text_fact(facts, "effect_source_ref", getattr(result, "effect_source_ref", ""))
    if not isinstance(envelope, dict):
        return facts
    _copy_text_fact(facts, "operation_id", envelope.get("operation_id"))
    operation = envelope.get("tool_operation")
    if isinstance(operation, dict):
        _copy_text_fact(facts, "operation_id", operation.get("operation_id"))
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
    envelope = getattr(result, "result_envelope", None)
    if not isinstance(envelope, dict):
        return []
    refs: list[dict[str, object]] = []
    _append_refs(refs, envelope)
    output = envelope.get("output")
    if isinstance(output, dict):
        _append_refs(refs, output)
    return refs


# LLM: 归档白名单可保留参数来源/类型/摘要，但绝不能复制原参数值或任意私有 result envelope。
# 函数用途: 压缩工具结果中恢复与收口所需的安全结构化事实，忽略未明确登记的实现私有字段。
def _compact_result_envelope(result: object) -> dict[str, object]:
    envelope = getattr(result, "result_envelope", None)
    if not isinstance(envelope, dict):
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


def _append_refs(refs: list[dict[str, object]], payload: dict[str, object]) -> None:
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
