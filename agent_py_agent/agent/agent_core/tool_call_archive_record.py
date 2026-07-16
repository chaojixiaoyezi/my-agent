
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
    )
    compact = {key: envelope[key] for key in keys if key in envelope}
    artifact_integrity = _compact_artifact_integrity(envelope.get("artifact_integrity"))
    if artifact_integrity:
        compact["artifact_integrity"] = artifact_integrity
    output = envelope.get("output")
    if isinstance(output, dict):
        compact["output"] = {key: output[key] for key in keys if key in output}
    return compact


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
