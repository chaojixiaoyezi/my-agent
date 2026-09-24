
# LLM: 工具输出归档保留 canonical 调用来源；attempt/turn 只取执行方传入事实，不由 request/scoped 别名推导。
# 模块用途: 保存大小输出及恢复索引，并把执行身份完整传给后续 Compact 过滤。

from __future__ import annotations

"""externalizes large runtime tool outputs into artifact files.

Human version:
Tool results can be useful evidence, but large bodies do not belong in raw
archive rows, token ledgers, or compact metadata. This module writes the full
tool output to an artifact file and returns a compact record with preview,
hash, size, and path. Internal orchestration/status tool outputs are stored in
their original form. Failed-call records preserve both the normalized control
error code and the provider/tool-reported code for recovery and diagnosis.
The full owner-scoped body remains audit evidence; every model-facing preview
is redacted here and carries the same trust/redaction metadata used by the live
tool-context reducer, compact and recovery.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..common.log_redaction import redact_sensitive_value
from ..common.path_segments import safe_path_segment
from ..common.tool_output_paths import tool_output_root
from ..settings.defaults import default_agent_config
from ..tooling.models import ToolFailureStage
from ..tooling.output_projection import (
    redact_tool_output_text,
    tool_output_projection_policy,
)
from ..tooling.runtime_facts import project_process_runtime_facts
from .schema import (
    RuntimeMemorySchemaOptions,
    runtime_memory_schema_payload,
)

TOOL_OUTPUT_RECORD_SCHEMA = RuntimeMemorySchemaOptions("tool_output_archive_record")
TOOL_OUTPUT_ARTIFACT_SCHEMA = RuntimeMemorySchemaOptions("tool_output_artifact")
TOOL_OUTPUT_INDEX_SCHEMA = RuntimeMemorySchemaOptions("tool_output_index")
_TOOL_FAILURE_STAGE_VALUES = frozenset(item.value for item in ToolFailureStage)
_SAFE_PARAMETER_MAX_DEPTH = 4
_SAFE_PARAMETER_MAX_ITEMS = 20


# LLM: The request carries the executed call's exact run/attempt/turn identity and two parameter
# views; model_parameters is the only durable provider-replay authority.
# 类用途: 描述一次工具输出归档的真实调用身份，并分开保存实际执行参数与模型原始参数。
@dataclass(frozen=True)
class ExternalizeToolOutputRequest:
    root: str | Path
    tool: str
    call_id: str
    output: str
    ok: bool
    status: str = ""
    error_code: str = ""
    # Control flow uses error_code; diagnosis keeps the source/tool code separately.
    reported_error_code: str = ""
    run_id: str = ""
    attempt_id: str = ""
    turn_id: str = ""
    task_id: str = ""
    request_id: str = ""
    # 同一个 durable task 可经历多个普通用户回合；该字段固定工具真正所属的
    # conversation active turn，不能用 task_id 或后台切片 request_id 代替。
    conversation_request_id: str = ""
    min_chars: int = -1
    preview_chars: int = -1
    parameters: dict[str, Any] | None = None
    model_parameters: dict[str, Any] | None = None
    result_envelope: dict[str, Any] | None = None


def externalize_tool_output_record(request: ExternalizeToolOutputRequest) -> dict[str, Any]:
    output = str(request.output or "")
    digest = _sha256_text(output)
    resolved = _resolved_request_limits(request)
    record = _base_record(request, output, digest, preview_chars=resolved.preview_chars)
    if _is_bounded_read_artifact_output(request, output):
        record.update(_read_artifact_record_fields(output))
        return record
    if _is_bounded_read_file_output(request):
        record.update(_archive_bounded_read_file_output(request, output, resolved))
        if not record.get("output_externalized") and not record.get("source_output_archived"):
            _append_tool_call_index(request, record, digest)
        return record
    if _output_requires_recovery_artifact(output, resolved, request.result_envelope):
        path = _write_output_artifact(request, output, digest)
        # _write_output_artifact 内部已 _append_index 写 kind=tool_output 行
        # (带 path)——这里不需要再补 tool_call 行, 否则同 path 双行会破坏
        # read_artifact 的唯一 basename 修复匹配。
        record.update({
            "output_externalized": True,
            "output_path": str(path),
            "artifact_ref": str(path),
        })
    else:
        _append_tool_call_index(request, record, digest)
    return record


def _archive_bounded_read_file_output(
    request: ExternalizeToolOutputRequest,
    output: str,
    resolved: _ResolvedOutputLimits,
) -> dict[str, Any]:
    if not _output_meets_archive_threshold(output, resolved.min_chars):
        return {
            "source_output_archived": False,
            "source_output_path": "",
            "source_artifact_ref": "",
        }
    path = _write_output_artifact(request, output, _sha256_text(output))
    return {
        "output_externalized": False,
        "output_path": "",
        "artifact_ref": str(path),
        "source_output_archived": True,
        "source_output_path": str(path),
        "source_artifact_ref": str(path),
    }


@dataclass(frozen=True)
class _ResolvedOutputLimits:
    min_chars: int
    preview_chars: int


def _resolved_request_limits(request: ExternalizeToolOutputRequest) -> _ResolvedOutputLimits:
    defaults = default_agent_config()
    return _ResolvedOutputLimits(
        min_chars=_request_limit(request.min_chars, defaults.tool_output_externalize_min_chars),
        preview_chars=_request_limit(request.preview_chars, defaults.tool_output_preview_chars),
    )


def _request_limit(value: object, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = -1
    return int(default) if parsed < 0 else max(0, parsed)


def _output_meets_archive_threshold(output: str, min_chars: int) -> bool:
    threshold = max(0, int(min_chars))
    if threshold <= 0:
        return True
    return max(len(output), len(output.encode("utf-8"))) >= threshold


# LLM: 显式可恢复要求与全局大小阈值同属归档决策事实；工具只能请求保留完整输出，不能指定宿主路径。
# 函数用途: 判断当前完整输出是否必须落成可分页读取的归档，即使它尚未达到全局大输出阈值。
def _output_requires_recovery_artifact(
    output: str,
    limits: _ResolvedOutputLimits,
    result_envelope: object = None,
) -> bool:
    """Keep a recovery anchor whenever the carried preview cannot hold the body."""

    policy = (
        result_envelope.get("tool_output_policy")
        if isinstance(result_envelope, dict)
        else None
    )
    if isinstance(policy, dict) and policy.get("requires_recovery_artifact") is True:
        return bool(output)
    return _output_meets_archive_threshold(output, limits.min_chars) or len(output) > max(
        0,
        limits.preview_chars,
    )


def _request_status(request: ExternalizeToolOutputRequest) -> str:
    status = str(request.status or "").strip()
    if status:
        return status
    return "ok" if request.ok else "error"


# LLM: Base records preserve the request's exact call identity and bounded/redacted model parameters;
# callers may enrich the record but must not infer missing identity from later runtime state.
# 函数用途: 构造包含真实调用身份、两套参数和安全预览的工具输出基础记录。
def _base_record(request: ExternalizeToolOutputRequest, output: str, digest: str, *, preview_chars: int) -> dict[str, Any]:
    created_at = datetime.now(tz=timezone.utc).isoformat()
    _trust, redaction = tool_output_projection_policy(request.result_envelope)
    record = {
        "version": TOOL_OUTPUT_RECORD_SCHEMA.version,
        "schema": runtime_memory_schema_payload(TOOL_OUTPUT_RECORD_SCHEMA),
        "tool": request.tool,
        "id": request.call_id,
        "call_id": request.call_id,
        "request_id": request.request_id,
        "conversation_request_id": request.conversation_request_id,
        "run_id": request.run_id,
        "attempt_id": request.attempt_id,
        "turn_id": request.turn_id,
        "task_id": request.task_id,
        "scoped_call_id": _scoped_call_id(request),
        "ok": request.ok,
        "status": _request_status(request),
        "error_code": str(request.error_code or "").strip(),
        "reported_error_code": str(request.reported_error_code or "").strip(),
        "output_preview": _preview(
            redact_tool_output_text(output, redaction=redaction),
            preview_chars,
        ),
        "output_hash": digest,
        "output_size_bytes": len(output.encode("utf-8")),
        "output_externalized": False,
        "output_path": "",
        "model_parameters": _safe_parameters(
            request.model_parameters
            if request.model_parameters is not None
            else request.parameters
        ),
        "created_at": created_at,
    }
    record.update(_result_envelope_index_metadata(request.result_envelope))
    return record


# LLM: Artifact bodies are owner-scoped audit evidence. Persist the actual attempt/turn and both
# parameter views, then append the same bounded metadata to the canonical index.
# 函数用途: 把完整大输出、真实调用身份及两套参数写入归档文件和索引。
def _write_output_artifact(request: ExternalizeToolOutputRequest, output: str, digest: str) -> Path:
    path = _artifact_path(request, digest)
    created_at = datetime.now(tz=timezone.utc).isoformat()
    payload = {
        "version": TOOL_OUTPUT_ARTIFACT_SCHEMA.version,
        "schema": runtime_memory_schema_payload(TOOL_OUTPUT_ARTIFACT_SCHEMA),
        "kind": "tool_output",
        "tool": request.tool,
        "call_id": request.call_id,
        "scoped_call_id": _scoped_call_id(request),
        "ok": request.ok,
        "status": _request_status(request),
        "error_code": str(request.error_code or "").strip(),
        "reported_error_code": str(request.reported_error_code or "").strip(),
        "request_id": request.request_id,
        "conversation_request_id": request.conversation_request_id,
        "run_id": request.run_id,
        "attempt_id": request.attempt_id,
        "turn_id": request.turn_id,
        "task_id": request.task_id,
        "parameters": _safe_parameters(request.parameters),
        "model_parameters": _safe_parameters(
            request.model_parameters
            if request.model_parameters is not None
            else request.parameters
        ),
        **_result_envelope_index_metadata(request.result_envelope),
        "source_input": _source_input(request.parameters),
        "sha256": digest,
        "size_bytes": len(output.encode("utf-8")),
        "created_at": created_at,
        "content": output,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _append_index(path, payload)
    return path


# LLM: The append-only index is the cross-process recovery source; preserve artifact identity and
# model parameters independently from execution parameters without copying raw output content.
# 函数用途: 为外置输出追加携带原始 attempt/turn 身份的轻量索引行。
def _append_index(path: Path, payload: dict[str, Any]) -> None:
    record = {
        "version": TOOL_OUTPUT_INDEX_SCHEMA.version,
        "schema": runtime_memory_schema_payload(TOOL_OUTPUT_INDEX_SCHEMA),
        "kind": payload["kind"],
        "tool": payload["tool"],
        "call_id": payload["call_id"],
        "scoped_call_id": payload.get("scoped_call_id", ""),
        "request_id": payload["request_id"],
        "conversation_request_id": str(payload.get("conversation_request_id") or ""),
        "run_id": payload["run_id"],
        "attempt_id": payload["attempt_id"],
        "turn_id": payload["turn_id"],
        "task_id": payload["task_id"],
        "ok": bool(payload.get("ok")),
        "status": str(payload.get("status") or ""),
        "error_code": str(payload.get("error_code") or ""),
        "reported_error_code": str(payload.get("reported_error_code") or ""),
        "parameters": _safe_parameters(payload.get("parameters")),
        "model_parameters": _safe_parameters(payload.get("model_parameters")),
        **_index_metadata_from_record(payload),
        "source_input": str(payload.get("source_input") or ""),
        "path": str(path),
        "sha256": payload["sha256"],
        "size_bytes": payload["size_bytes"],
        "created_at": payload["created_at"],
    }
    index_path = path.parent / "index.jsonl"
    with index_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


# LLM: Small outputs retain exact attempt/turn and dual parameters in the original append-only
# index even though no artifact body is written; never derive identity from later runtime state.
# 函数用途: 为无需外置正文的小工具调用追加带真实身份的轻量索引行。
def _append_tool_call_index(request: ExternalizeToolOutputRequest, record: dict[str, Any], digest: str) -> None:
    created_at = str(record.get("created_at") or datetime.now(tz=timezone.utc).isoformat())
    payload = {
        "version": TOOL_OUTPUT_INDEX_SCHEMA.version,
        "schema": runtime_memory_schema_payload(TOOL_OUTPUT_INDEX_SCHEMA),
        "kind": "tool_call",
        "tool": request.tool,
        "call_id": request.call_id,
        "scoped_call_id": _scoped_call_id(request),
        "request_id": request.request_id,
        "conversation_request_id": request.conversation_request_id,
        "run_id": request.run_id,
        "attempt_id": request.attempt_id,
        "turn_id": request.turn_id,
        "task_id": request.task_id,
        "ok": request.ok,
        "status": str(record.get("status") or _request_status(request)),
        "error_code": str(record.get("error_code") or request.error_code or "").strip(),
        "reported_error_code": str(
            record.get("reported_error_code") or request.reported_error_code or ""
        ).strip(),
        "parameters": _safe_parameters(request.parameters),
        "model_parameters": _safe_parameters(
            request.model_parameters
            if request.model_parameters is not None
            else request.parameters
        ),
        **_index_metadata_from_record(record),
        "source_input": _source_input(request.parameters),
        "path": "",
        "sha256": digest,
        "size_bytes": int(record.get("output_size_bytes", 0) or 0),
        "output_externalized": False,
        "created_at": created_at,
    }
    index_path = tool_output_root(request.root) / "index.jsonl"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    with index_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


# LLM: New complete identities distinguish equal bodies from different attempts/turns at the
# original artifact path; incomplete legacy requests keep their old name without invented ids.
# 函数用途: 用真实四元调用身份区分同名同正文产物，旧身份缺失时沿原文件名落盘。
def _artifact_path(request: ExternalizeToolOutputRequest, digest: str) -> Path:
    identity = (request.run_id, request.attempt_id, request.turn_id, request.call_id)
    identity_suffix = ""
    if all(identity):
        identity_text = json.dumps(identity, ensure_ascii=False, separators=(",", ":"))
        identity_suffix = f"-{_sha256_text(identity_text)[:24]}"
    return (
        tool_output_root(request.root)
        / (
            f"{safe_path_segment(request.tool, default='item', replacement='_')}-"
            f"{safe_path_segment(request.call_id, default='item', replacement='_')}"
            f"{identity_suffix}-{digest[:12]}.json"
        )
    )


def _preview(output: str, max_chars: int) -> str:
    if max_chars <= 0:
        return ""
    if len(output) <= max_chars:
        return output
    cut = output.rfind("\n", max_chars // 2, max_chars)
    if cut < 0:
        cut = max_chars
    return output[:cut] + f"\n... [truncated {len(output) - cut} chars]"


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _read_window_from_envelope(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    window = value.get("read_window")
    if not isinstance(window, dict):
        return {}
    kind = str(window.get("kind") or "").strip()
    if kind == "char_window":
        return _char_window(window)
    if kind == "line_window":
        return _line_window(window)
    return {}


def _page_window_from_envelope(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    window = value.get("page_window")
    if not isinstance(window, dict):
        return {}
    if str(window.get("kind") or "").strip() != "offset_page":
        return {}
    tool = str(window.get("tool") or "").strip()
    source_path = str(window.get("source_path") or "").strip()
    offset = _optional_nonnegative_int(window.get("offset"))
    limit = _optional_positive_int(window.get("limit"))
    returned = _optional_nonnegative_int(window.get("returned"))
    next_offset = _optional_nonnegative_int(window.get("next_offset"))
    if not tool or not source_path or offset is None or limit is None or returned is None:
        return {}
    if next_offset is None:
        next_offset = 0 if bool(window.get("complete")) else offset + returned
    payload: dict[str, object] = {
        "kind": "offset_page",
        "tool": tool,
        "source_path": source_path,
        "offset": offset,
        "limit": limit,
        "returned": returned,
        "next_offset": next_offset,
        "complete": bool(window.get("complete")) or next_offset <= 0,
    }
    output_mode = str(window.get("output_mode") or "").strip()
    if output_mode:
        payload["output_mode"] = output_mode
    return payload


def _char_window(window: dict[str, object]) -> dict[str, object]:
    offset = _optional_nonnegative_int(window.get("offset"))
    chars = _optional_nonnegative_int(window.get("chars"))
    total = _optional_nonnegative_int(window.get("total_chars"))
    next_offset = _optional_nonnegative_int(window.get("next_offset"))
    if offset is None or total is None:
        return {}
    if chars is None and next_offset is not None:
        chars = max(0, next_offset - offset)
    if next_offset is None and chars is not None:
        next_offset = offset + chars
    if chars is None or next_offset is None:
        return {}
    return {
        "kind": "char_window",
        "offset": offset,
        "chars": chars,
        "next_offset": next_offset,
        "total_chars": total,
        "complete": bool(window.get("complete")) or bool(total and next_offset >= total),
    }


def _line_window(window: dict[str, object]) -> dict[str, object]:
    start = _optional_positive_int(window.get("start_line"))
    end = _optional_nonnegative_int(window.get("end_line"))
    total = _optional_nonnegative_int(window.get("total_lines"))
    next_start = _optional_nonnegative_int(window.get("next_start_line"))
    if start is None or end is None or total is None:
        return {}
    if next_start is None:
        next_start = end + 1 if end < total else 0
    return {
        "kind": "line_window",
        "start_line": start,
        "end_line": end,
        "next_start_line": next_start,
        "total_lines": total,
        "complete": bool(window.get("complete")) or bool(total and start <= 1 and end >= total),
    }


def _optional_nonnegative_int(value: object) -> int | None:
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _optional_positive_int(value: object) -> int | None:
    number = _optional_nonnegative_int(value)
    return number if number and number > 0 else None


def _is_bounded_read_artifact_output(request: ExternalizeToolOutputRequest, output: str) -> bool:
    if request.tool != "read_artifact" or not request.ok:
        return False
    payload = _json_object(output)
    return bool(payload and payload.get("reads_artifact_body") is True)


def _is_bounded_read_file_output(request: ExternalizeToolOutputRequest) -> bool:
    return request.tool == "read_file" and request.ok


def _read_artifact_record_fields(output: str) -> dict[str, Any]:
    payload = _json_object(output) or {}
    return {
        "reads_artifact_body": True,
        "source_artifact_ref": str(payload.get("artifact_ref") or ""),
        "source_tool": str(payload.get("tool") or ""),
        "source_call_id": str(payload.get("call_id") or ""),
    }


# LLM: Canonical tool-call recovery needs bounded nested arguments (for example Todo items and
# child covers), while credentials and arbitrary objects must never enter the durable index.
# 函数用途: 将工具参数递归投影成可恢复、有限且脱敏的 JSON 结构，供同一 active turn 续跑。
def _safe_parameters(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, Any] = {}
    for key, item in list(value.items())[:_SAFE_PARAMETER_MAX_ITEMS]:
        text_key = str(key).strip()
        if not text_key:
            continue
        safe_item = _safe_parameter_item(item, field_name=text_key, depth=0)
        if safe_item is not _UNSAFE_PARAMETER:
            result[text_key] = safe_item
    return result


# LLM: Model replay must prefer the explicitly persisted provider payload. Legacy rows may derive
# it only from value-free input provenance; trusted/default host additions are never model input.
# 函数用途: 从工具归档中取得可回放给模型的参数，并兼容尚未写入 model_parameters 的旧索引。
def model_visible_tool_parameters(record: Any) -> dict[str, Any]:
    if not isinstance(record, dict):
        return {}
    explicit = record.get("model_parameters")
    if isinstance(explicit, dict):
        return _safe_parameters(explicit)
    parameters = _safe_parameters(record.get("parameters"))
    sources = record.get("input_sources")
    if not isinstance(sources, list | tuple):
        return parameters
    public_names = {
        name
        for item in sources
        if isinstance(item, dict)
        and str(item.get("source") or "")
        in {"model_proposed", "caller_supplied", "trusted_context_verified"}
        and (name := _top_level_parameter_name(item.get("path")))
    }
    return {
        key: value
        for key, value in parameters.items()
        if str(key) in public_names
    }


# LLM: Input provenance paths are JSONPath-like top-level fields. Reject nested/quoted forms here
# instead of guessing because a wrong extraction could expose a host-only binding during replay.
# 函数用途: 从形如 $.field 的来源路径中安全取出顶层参数名。
def _top_level_parameter_name(value: Any) -> str:
    path = str(value or "").strip()
    if not path.startswith("$."):
        return ""
    name = path[2:]
    return name if name and name.replace("_", "").isalnum() else ""


# LLM: 持久索引只保存执行/操作生命周期、有界进程事实、字段路径、来源类别和结构化引用，
# 不得复制参数值、工具正文或任意 envelope 私有字段。
# 函数用途: 把工具入口的终态、进程清理事实、value-free 参数来源与读取窗口投影到耐久工具索引。
def _result_envelope_index_metadata(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    metadata: dict[str, Any] = {}
    if tool_execution := _safe_tool_execution(value.get("tool_execution")):
        metadata["tool_execution"] = tool_execution
    if tool_operation := _safe_tool_operation(value.get("tool_operation")):
        metadata["tool_operation"] = tool_operation
    if process := project_process_runtime_facts(value.get("process")):
        metadata["tool_process"] = process
    if read_window := _read_window_from_envelope(value):
        metadata["read_window"] = read_window
    if page_window := _page_window_from_envelope(value):
        metadata["page_window"] = page_window
    if input_sources := _safe_input_sources(value.get("input_sources")):
        metadata["input_sources"] = input_sources
    output_policy = value.get("tool_output_policy")
    if isinstance(output_policy, dict):
        trust, redaction = tool_output_projection_policy(value)
        metadata["tool_output_trust"] = trust
        metadata["tool_output_redaction"] = redaction
    return metadata


# LLM: Artifact and short-call index rows must carry the same bounded lifecycle metadata;
# process uses the shared bounded projection; never copy arbitrary payload fields into the index.
# 函数用途: 从工具归档记录中挑出后台续接需要的安全元数据，保证大小输出采用同一恢复语义。
def _index_metadata_from_record(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    metadata: dict[str, Any] = {}
    for key in (
        "read_window",
        "page_window",
        "tool_execution",
        "tool_operation",
        "tool_output_trust",
        "tool_output_redaction",
    ):
        item = value.get(key)
        if isinstance(item, dict):
            metadata[key] = dict(item)
        elif key.startswith("tool_output_") and isinstance(item, str):
            metadata[key] = item
    if process := project_process_runtime_facts(value.get("tool_process")):
        metadata["tool_process"] = process
    if input_sources := _safe_input_sources(value.get("input_sources")):
        metadata["input_sources"] = input_sources
    return metadata


# LLM: 耐久工具索引只接受 Registry 已写入的有限执行事实；任意 envelope 私有字段不能借此进入恢复上下文。
# 函数用途: 校验并复制 handler 是否进入、失败层级和耗时，供文件事实源与 SQLite 账本交叉排障。
def _safe_tool_execution(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    handler_executed = value.get("handler_executed")
    duration_ms = _optional_nonnegative_int(value.get("duration_ms"))
    failure_stage = str(value.get("failure_stage") or "").strip().lower()
    if not isinstance(handler_executed, bool) or duration_ms is None:
        return {}
    if failure_stage not in {"", *_TOOL_FAILURE_STAGE_VALUES}:
        return {}
    payload: dict[str, Any] = {
        "handler_executed": handler_executed,
        "duration_ms": duration_ms,
    }
    if failure_stage:
        payload["failure_stage"] = failure_stage
    return payload


# LLM: Operation recovery accepts only the coordinator's bounded identity/lifecycle fields.
# Unknown private keys, diagnostics and arbitrary nested values must not enter the durable index.
# 函数用途: 清洗副作用操作终态，让后台续轮能区分成功、失败、未知和重放，同时不保存实现私有载荷。
def _safe_tool_operation(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    operation_id = _single_line_text(value.get("operation_id"), max_chars=512)
    status = _single_line_text(value.get("status"), max_chars=64)
    if not operation_id or not status:
        return {}
    payload: dict[str, Any] = {
        "operation_id": operation_id,
        "status": status,
    }
    for key, max_chars in (
        ("schema_version", 64),
        ("result_ref", 1024),
        ("action", 128),
        ("idempotency_scope", 128),
        ("reconciliation_source_ref", 1024),
    ):
        text = _single_line_text(value.get(key), max_chars=max_chars)
        if text:
            payload[key] = text
    if isinstance(value.get("replayed"), bool):
        payload["replayed"] = value.get("replayed") is True
    original_execution = _safe_tool_execution(value.get("original_tool_execution"))
    if original_execution:
        payload["original_tool_execution"] = original_execution
    return payload


def _safe_input_sources(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list | tuple):
        return []
    sources: list[dict[str, str]] = []
    for item in value[:100]:
        if not isinstance(item, dict):
            continue
        path = _single_line_text(item.get("path"), max_chars=256)
        source = _single_line_text(item.get("source"), max_chars=64)
        source_ref = _single_line_text(item.get("source_ref"), max_chars=512)
        if not path or not source or not source_ref:
            continue
        sources.append(
            {
                "path": path,
                "source": source,
                "source_ref": source_ref,
            }
        )
    return sources


def _single_line_text(value: Any, *, max_chars: int) -> str:
    text = str(value or "").strip()
    if not text or "\n" in text or "\r" in text:
        return ""
    return text[:max_chars]


_UNSAFE_PARAMETER = object()


# LLM: Keep only JSON-like values to a fixed depth/width and redact every scalar under its real
# field name. Do not stringify unknown objects because their repr may contain private runtime state.
# 函数用途: 递归保留 Todo、批量派工等嵌套参数，同时限制体量并清除凭据字段。
def _safe_parameter_item(
    item: Any,
    *,
    field_name: str = "",
    depth: int,
) -> object:
    if depth > _SAFE_PARAMETER_MAX_DEPTH:
        return _UNSAFE_PARAMETER
    if isinstance(item, str | int | float | bool) or item is None:
        return redact_sensitive_value(item, field_name=field_name)
    if isinstance(item, list | tuple):
        values: list[object] = []
        for entry in list(item)[:_SAFE_PARAMETER_MAX_ITEMS]:
            safe_entry = _safe_parameter_item(
                entry,
                field_name=field_name,
                depth=depth + 1,
            )
            if safe_entry is not _UNSAFE_PARAMETER:
                values.append(safe_entry)
        return values
    if isinstance(item, dict):
        values: dict[str, object] = {}
        for child_key, child_value in list(item.items())[:_SAFE_PARAMETER_MAX_ITEMS]:
            text_key = str(child_key).strip()
            if not text_key:
                continue
            safe_value = _safe_parameter_item(
                child_value,
                field_name=text_key,
                depth=depth + 1,
            )
            if safe_value is not _UNSAFE_PARAMETER:
                values[text_key] = safe_value
        return values
    return _UNSAFE_PARAMETER


def _source_input(value: Any) -> str:
    params = _safe_parameters(value)
    for key in ("path", "url", "artifact_ref", "query", "command"):
        candidate = str(params.get(key) or "").strip()
        if candidate:
            return candidate
    for item in params.values():
        if isinstance(item, str) and item.strip():
            return item.strip()
    return ""


def _json_object(text: str) -> dict[str, Any] | None:
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _scoped_call_id(request: ExternalizeToolOutputRequest) -> str:
    scope = request.run_id or request.task_id or request.request_id
    return f"{scope}:{request.call_id}" if scope else request.call_id


__all__ = [
    "ExternalizeToolOutputRequest",
    "externalize_tool_output_record",
    "model_visible_tool_parameters",
]
