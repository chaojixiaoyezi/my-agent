
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from ...tooling.output_projection import project_tool_output_body
from ...tooling.runtime_contracts import ToolCall, ToolResult
from ..runner.ref_fields import _file_refs_from_value, _normalize_file_ref

_DEFAULT_LIMIT = 3
_DEFAULT_MAX_PREVIEW_CHARS = 600
_DEFAULT_MAX_OUTPUT_BYTES = 4000
_DEFAULT_MAX_DIRECTIVE_CHARS = 1600
_SHARED_CONTEXT_SOURCE_TOOLS = frozenset({
    "read_file",
    "read_artifact",
    "web_fetch",
    "web_search",
    "search_text",
    "list_files",
})


@dataclass(frozen=True)
class _SharedPackBounds:
    max_preview_chars: int
    max_output_bytes: int


@dataclass(frozen=True)
class _SharedToolValues:
    tool_name: object
    ok: object
    output: object
    payload: object
    ref: object
    output_trust: object
    output_redaction: object
    bounds: _SharedPackBounds


def append_parent_shared_context(agent: object, raw_params: dict[str, object]) -> dict[str, object]:
    # Exact source workers already carry the Audit objective and their own
    # source refs. Parent read previews may contain sibling sources, so copying
    # them would violate the one-source context boundary and waste attention.
    from ...common.audit_activation import (
        structured_audit_source_worker_attributes,
    )

    if structured_audit_source_worker_attributes(raw_params.get("attributes")):
        return raw_params
    packs = parent_task_directive_packs(agent) + parent_shared_context_packs(agent)
    if not packs:
        return raw_params
    merged = dict(raw_params)
    existing = _context_pack_list(merged.get("context_packs"))
    seen = _pack_identity_set(existing)
    for pack in packs:
        key = _pack_identity(pack)
        if key in seen:
            continue
        seen.add(key)
        existing.append(pack)
    merged["context_packs"] = existing
    return merged


def parent_task_directive_packs(
    agent: object,
    *,
    max_chars: int = _DEFAULT_MAX_DIRECTIVE_CHARS,
) -> list[dict[str, object]]:
    summary = _summary_text(_current_prompt_text(agent), max_preview_chars=max_chars)
    if not summary:
        return []
    return [{
        "kind": "parent_task_directive",
        "role": "primary_directive",
        "source": "current_user_prompt",
        "summary": summary,
        "handoff_note": (
            "这是父级原始任务摘要，用于补全局部子任务 goal 省略的背景。"
            "如果你在自己的资料或工作中发现需要其他代理补证据、换来源或协作研判的线索，"
            "请使用协作工具留下 case/request/evidence 引用。"
        ),
    }]


def parent_shared_context_packs(
    agent: object,
    *,
    limit: int = _DEFAULT_LIMIT,
    max_preview_chars: int = _DEFAULT_MAX_PREVIEW_CHARS,
    max_output_bytes: int = _DEFAULT_MAX_OUTPUT_BYTES,
) -> list[dict[str, object]]:
    params = getattr(agent, "_current_tool_loop_params", None)
    records = getattr(params, "archive_tool_calls", []) if params is not None else []
    packs = shared_context_packs_from_archive(
        records,
        limit=limit,
        max_preview_chars=max_preview_chars,
        max_output_bytes=max_output_bytes,
    )
    if packs:
        return packs
    cached = getattr(agent, "_parent_shared_context_packs", [])
    if isinstance(cached, list):
        return [dict(item) for item in cached if isinstance(item, dict)][: max(0, int(limit))]
    return []


def refresh_parent_shared_context_cache(agent: object, records: Iterable[object]) -> list[dict[str, object]]:
    packs = shared_context_packs_from_archive(records)
    if packs:
        agent._parent_shared_context_packs = packs
    return packs


def refresh_parent_shared_context_from_tool_record(agent: object, record: object) -> list[dict[str, object]]:
    result = getattr(record, "result", None)
    call = getattr(record, "call", None)
    if not isinstance(result, ToolResult) or not isinstance(call, ToolCall):
        return []
    pack = _shared_pack_from_tool_values(_SharedToolValues(
        tool_name=result.tool_name,
        ok=result.ok,
        output=result.output,
        payload=call.arguments,
        ref=result.call_id,
        output_trust=result.output_trust,
        output_redaction=result.output_redaction,
        bounds=_SharedPackBounds(_DEFAULT_MAX_PREVIEW_CHARS, _DEFAULT_MAX_OUTPUT_BYTES),
    ))
    if not pack:
        return []
    existing = getattr(agent, "_parent_shared_context_packs", [])
    packs = _merge_packs(_context_pack_list(existing), [pack], limit=_DEFAULT_LIMIT)
    agent._parent_shared_context_packs = packs
    return packs


def shared_context_packs_from_archive(
    records: Iterable[object],
    *,
    limit: int = _DEFAULT_LIMIT,
    max_preview_chars: int = _DEFAULT_MAX_PREVIEW_CHARS,
    max_output_bytes: int = _DEFAULT_MAX_OUTPUT_BYTES,
) -> list[dict[str, object]]:
    packs: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    for record in reversed([item for item in records if isinstance(item, dict)]):
        pack = _shared_pack_from_record(
            record,
            max_preview_chars=max_preview_chars,
            max_output_bytes=max_output_bytes,
        )
        if not pack:
            continue
        key = _pack_identity(pack)
        if key in seen:
            continue
        seen.add(key)
        packs.append(pack)
        if len(packs) >= max(0, int(limit)):
            break
    return list(reversed(packs))


def _shared_pack_from_record(
    record: dict[str, object],
    *,
    max_preview_chars: int,
    max_output_bytes: int,
) -> dict[str, object]:
    if not record.get("ok"):
        return {}
    source_tool = str(record.get("tool") or "").strip()
    if source_tool not in _SHARED_CONTEXT_SOURCE_TOOLS:
        return {}
    if _int_value(record.get("output_size_bytes")) > max(0, int(max_output_bytes)):
        return {}
    summary = _summary_text(
        record.get("output_preview"),
        max_preview_chars=max_preview_chars,
    )
    if not summary:
        return {}
    summary = project_tool_output_body(
        tool=source_tool,
        output=summary,
        trust=str(record.get("tool_output_trust") or "runtime"),
        redaction=str(record.get("tool_output_redaction") or "default"),
    )
    source_ref = _record_source_ref(record)
    if not source_ref:
        return {}
    pack: dict[str, object] = {
        "kind": "parent_recent_read",
        "role": "shared_brief",
        "summary": summary,
        "path": source_ref,
    }
    tool_ref = _record_tool_ref(record)
    if tool_ref:
        pack["ref"] = tool_ref
    if source_tool:
        pack["source_tool"] = source_tool
    return pack


def _shared_pack_from_tool_values(values: _SharedToolValues) -> dict[str, object]:
    if not values.ok:
        return {}
    source_tool = str(values.tool_name or "").strip()
    if source_tool not in _SHARED_CONTEXT_SOURCE_TOOLS:
        return {}
    if len(str(values.output or "").encode("utf-8")) > max(0, int(values.bounds.max_output_bytes)):
        return {}
    summary = _summary_text(
        values.output,
        max_preview_chars=values.bounds.max_preview_chars,
    )
    if not summary:
        return {}
    trust = str(values.output_trust or "runtime").strip().lower()
    redaction = str(values.output_redaction or "default").strip().lower()
    summary = project_tool_output_body(
        tool=source_tool,
        output=summary,
        trust=trust,
        redaction=redaction,
    )
    source_ref = _first_file_ref(values.payload)
    if not source_ref:
        return {}
    pack: dict[str, object] = {
        "kind": "parent_recent_read",
        "role": "shared_brief",
        "summary": summary,
        "path": source_ref,
    }
    text_ref = str(values.ref or "").strip()
    if text_ref:
        pack["ref"] = text_ref
    if source_tool:
        pack["source_tool"] = source_tool
    return pack


def _record_source_ref(record: dict[str, object]) -> str:
    parameters = record.get("parameters")
    if normalized := _first_file_ref(parameters):
        return normalized
    return _envelope_source_ref(record.get("tool_result_envelope"))


def _envelope_source_ref(value: object) -> str:
    if not isinstance(value, dict):
        return ""
    for key in ("source_ref", "path", "artifact_ref"):
        normalized = _normalize_file_ref(str(value.get(key) or ""))
        if normalized:
            return normalized
    return ""


def _first_file_ref(value: object) -> str:
    for ref in _file_refs_from_value(value):
        normalized = _normalize_file_ref(ref)
        if normalized:
            return normalized
    return ""


def _record_tool_ref(record: dict[str, object]) -> str:
    for key in ("scoped_call_id", "artifact_ref", "output_path", "call_id", "id"):
        text = str(record.get(key) or "").strip()
        if text:
            return text
    return ""


def _summary_text(value: object, *, max_preview_chars: int) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        return ""
    limit = max(0, int(max_preview_chars))
    return text[:limit]


def _current_prompt_text(agent: object) -> str:
    value = getattr(agent, "_current_user_prompt", "")
    return value if isinstance(value, str) else ""


def _context_pack_list(value: object) -> list[dict[str, object]]:
    if isinstance(value, dict):
        return [dict(value)]
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _pack_identity_set(packs: list[dict[str, object]]) -> set[tuple[str, str]]:
    return {_pack_identity(item) for item in packs}


def _merge_packs(
    existing: list[dict[str, object]],
    incoming: list[dict[str, object]],
    *,
    limit: int,
) -> list[dict[str, object]]:
    merged = list(existing)
    seen = _pack_identity_set(merged)
    for pack in incoming:
        key = _pack_identity(pack)
        if key in seen:
            continue
        seen.add(key)
        merged.append(pack)
    return merged[-max(0, int(limit)):] if limit else merged


def _pack_identity(pack: dict[str, object]) -> tuple[str, str]:
    return (
        str(pack.get("kind") or "").strip(),
        str(pack.get("path") or pack.get("ref") or "").strip(),
    )


def _int_value(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


__all__ = [
    "append_parent_shared_context",
    "parent_shared_context_packs",
    "parent_task_directive_packs",
    "refresh_parent_shared_context_cache",
    "refresh_parent_shared_context_from_tool_record",
    "shared_context_packs_from_archive",
]
