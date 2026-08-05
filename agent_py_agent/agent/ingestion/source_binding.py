from __future__ import annotations

"""Mechanical source bindings published for one named Audit.

The binding contains only transport and record-boundary facts already learned
by the Agent.  It deliberately contains no device taxonomy, business verdict,
score rule, or workflow template.
"""

import hashlib
import json
import re
from copy import deepcopy
from typing import Any

from ..common.audit_activation import AUDIT_SOURCE_OPEN_FIELDS
from ..contracts.tool_input_schema import normalize_tool_input, validate_tool_input
from ..tooling.tool_spec_schema import tool_spec_input_schema
from .source_adapter import normalize_source_adapter
from .source_http import (
    normalize_source_http_request,
    normalize_top_level_response_field,
    public_source_http_request,
)
from .watch_tool_spec import build_watch_stream_spec

_AUDIT_SOURCE_OPEN_FIELD_SET = frozenset(AUDIT_SOURCE_OPEN_FIELDS)
_MAX_AUDIT_SOURCES = 256
_SOURCE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


def audit_source_binding_schema() -> dict[str, Any]:
    """Return the canonical persisted watch-open transport schema."""

    watch_schema = tool_spec_input_schema(build_watch_stream_spec())
    properties = watch_schema.get("properties")
    properties = properties if isinstance(properties, dict) else {}
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            name: deepcopy(properties[name])
            for name in AUDIT_SOURCE_OPEN_FIELDS
            if isinstance(properties.get(name), dict)
        },
        # The host needs only a stable source identity and a proven transport.
        # Notes, scripts, tests, skills and documents are ordinary optional
        # Audit-workspace materials chosen by the coordinating Agent during
        # prepare; the runtime must not impose a business-document template.
        "required": ["source_id", "url"],
    }


def normalize_audit_source_bindings(value: object) -> tuple[dict[str, Any], ...]:
    """Validate and canonicalize an open-world list of source transports."""

    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > _MAX_AUDIT_SOURCES:
        raise ValueError(f"source_bindings 必须是最多 {_MAX_AUDIT_SOURCES} 项的数组")
    rows: list[dict[str, Any]] = []
    source_ids: set[str] = set()
    source_urls: set[str] = set()
    schema = audit_source_binding_schema()
    for index, raw in enumerate(value):
        if not isinstance(raw, dict):
            raise ValueError(f"source_bindings[{index}] 必须是对象")
        extras = sorted(str(key) for key in raw if key not in _AUDIT_SOURCE_OPEN_FIELD_SET)
        if extras:
            raise ValueError(f"source_bindings[{index}] 包含未知字段: {extras}")
        normalized = normalize_tool_input(dict(raw), schema)
        validation = validate_tool_input(normalized.value, schema)
        if not validation.ok:
            issues = ",".join(
                f"{item.path}:{item.keyword}" for item in validation.issues[:6]
            )
            raise ValueError(f"source_bindings[{index}] 结构无效: {issues}")
        row = _canonical_binding(dict(normalized.value), index=index)
        source_id = str(row["source_id"])
        source_url = str(row["url"])
        if source_id in source_ids:
            raise ValueError(f"source_bindings[{index}] source_id 重复: {source_id}")
        if source_url in source_urls:
            raise ValueError(
                f"source_bindings[{index}] 与另一来源地址相同；一个实际来源只能绑定一次"
            )
        source_ids.add(source_id)
        source_urls.add(source_url)
        rows.append(row)
    return tuple(rows)


def audit_source_binding_from_watch_state(state: object) -> dict[str, Any]:
    """Materialize one canonical binding from a successful durable watch probe.

    The model chooses and tests a transport through ``watch_stream``. Once the
    probe succeeds, its persisted state is authoritative for URL, request and
    record-boundary facts; publication must not ask the model to transcribe the
    same mechanical fields a second time.
    """

    source_url = str(getattr(state, "source_url", "") or "").strip()
    source_id = str(getattr(state, "source_id", "") or "").strip()
    profile_ref = str(getattr(state, "source_profile_ref", "") or "").strip()
    envelope = getattr(state, "source_envelope", None)
    if not source_url or not source_id or not isinstance(envelope, dict):
        raise ValueError("来源探针缺少可发布的地址、source_id 或响应结构")

    raw: dict[str, Any] = {
        "url": source_url,
        "source_id": source_id,
        "document_refs": list(getattr(state, "document_refs", []) or []),
    }
    if profile_ref:
        raw["source_profile_ref"] = profile_ref
    if envelope.get("mode") == "file":
        boundary = envelope.get("record_boundary")
        if not isinstance(boundary, dict) or not boundary:
            raise ValueError("文件来源探针缺少完整 record_boundary")
        raw["record_boundary"] = deepcopy(boundary)
    else:
        if envelope.get("valid") is not True:
            raise ValueError("HTTP 来源探针尚未得到有效响应结构")
        mode = str(envelope.get("mode") or "cursor").strip().lower()
        if mode == "cursor" and envelope.get("continuation_verified") is not True:
            raise ValueError("HTTP 游标来源尚未完成第二次增量续读验证")
        request = envelope.get("request")
        if not isinstance(request, dict) or not request:
            raise ValueError("HTTP 来源探针缺少已验证请求结构")
        raw.update({"mode": mode, "http_request": deepcopy(request)})
        if mode == "adapter":
            adapter = envelope.get("adapter")
            if not isinstance(adapter, dict):
                raise ValueError("动态 HTTP 来源探针缺少已验证 source_adapter")
            raw["source_adapter"] = deepcopy(adapter)
        else:
            raw.update(
                {
                    "record_list_field": envelope.get("record_list_key"),
                    "cursor_field": envelope.get("cursor_field"),
                    "cursor_semantics": envelope.get("cursor_semantics"),
                    "has_more_field": envelope.get("has_more_field"),
                }
            )
        if mode == "poll":
            tuning = getattr(state, "tuning", None)
            raw["poll_query_seconds"] = max(
                1,
                int(getattr(tuning, "poll_query_seconds", 60) or 60),
            )
        raw = {key: value for key, value in raw.items() if value is not None}

    return dict(normalize_audit_source_bindings([raw])[0])


def public_audit_source_bindings(value: object) -> list[dict[str, Any]]:
    """Project persisted bindings without secret references or secret values."""

    rows = value if isinstance(value, (list, tuple)) else []
    public: list[dict[str, Any]] = []
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        row = deepcopy(raw)
        if "http_request" in row:
            row["http_request"] = public_source_http_request(row.get("http_request"))
        public.append(row)
    return public


def merge_audit_source_bindings(
    current: object,
    updates: object,
) -> tuple[dict[str, Any], ...]:
    """Upsert canonical bindings by stable source_id while preserving order.

    Prepare normally learns one source at a time.  Making every turn repeat the
    whole source table turns an omission into an accidental delete.  Unmentioned
    rows therefore stay put, an exact source_id is replaced in place, and a new
    id is appended.  The merged table is validated again so updates cannot
    introduce duplicate ids or collide with another source URL.
    """

    existing = [dict(row) for row in (current or ()) if isinstance(row, dict)]
    changed = [dict(row) for row in (updates or ()) if isinstance(row, dict)]
    if not changed:
        return normalize_audit_source_bindings(existing)
    positions = {
        str(row.get("source_id") or "").strip(): index
        for index, row in enumerate(existing)
    }
    for row in changed:
        source_id = str(row.get("source_id") or "").strip()
        if source_id in positions:
            existing[positions[source_id]] = row
        else:
            positions[source_id] = len(existing)
            existing.append(row)
    return normalize_audit_source_bindings(existing)


def audit_source_binding_by_id(
    bindings: object,
    source_id: object,
) -> dict[str, Any] | None:
    selected = str(source_id or "").strip()
    if not selected:
        return None
    rows = bindings if isinstance(bindings, (list, tuple)) else []
    matches = [
        dict(row)
        for row in rows
        if isinstance(row, dict) and str(row.get("source_id") or "").strip() == selected
    ]
    return matches[0] if len(matches) == 1 else None


def audit_source_runtime_projection(binding: object) -> dict[str, Any]:
    """Project one published transport binding back into watch runtime facts.

    The named Audit task owns the canonical transport table.  A running watch
    keeps cursor/checkpoint/spool state separately, so a prepare revision may
    replace only these mechanical request/framing facts without replaying the
    source or inventing a second watch.
    """

    if not isinstance(binding, dict):
        raise ValueError("来源绑定必须是对象")
    row = dict(normalize_audit_source_bindings([dict(binding)])[0])
    source_url = str(row.get("url") or "").strip()
    source_id = str(row.get("source_id") or "").strip()
    profile_ref = str(row.get("source_profile_ref") or "").strip()
    document_refs = [
        str(item).strip()
        for item in row.get("document_refs", []) or []
        if str(item or "").strip()
    ]
    if source_url.lower().startswith("file://"):
        boundary = row.get("record_boundary")
        if not isinstance(boundary, dict) or not boundary:
            raise ValueError("文件来源绑定缺少完整 record_boundary")
        return {
            "source_id": source_id,
            "source_url": source_url,
            "source_mode": "",
            "source_envelope": {
                "mode": "file",
                "record_boundary": deepcopy(boundary),
            },
            "source_profile_ref": profile_ref,
            "document_refs": document_refs,
            "poll_query_seconds": None,
        }

    mode = str(row.get("mode") or "cursor").strip().lower()
    request = row.get("http_request")
    if not isinstance(request, dict) or not request:
        raise ValueError("HTTP 来源绑定缺少已验证请求结构")
    envelope: dict[str, Any] = {
        "mode": mode,
        "record_boundary": (
            "whole_response"
            if mode == "poll"
            else "adapter_records" if mode == "adapter" else "array_item"
        ),
        "request": deepcopy(request),
        "valid": True,
    }
    if mode == "adapter":
        adapter = row.get("source_adapter")
        if not isinstance(adapter, dict):
            raise ValueError("动态 HTTP 来源绑定缺少 source_adapter")
        envelope["adapter"] = deepcopy(adapter)
        # Publication is allowed only from a probe that already proved a
        # second incremental read.  Preserve that structural fact when the
        # minimal runtime envelope is reconstructed; observed sample counts
        # and other transient probe facts deliberately stay out.
        envelope["continuation_verified"] = True
    elif mode == "cursor":
        envelope.update(
            {
                "record_list_key": row.get("record_list_field"),
                "cursor_field": row.get("cursor_field"),
                "cursor_semantics": row.get("cursor_semantics"),
                "has_more_field": row.get("has_more_field"),
                "continuation_verified": True,
            }
        )
    return {
        "source_id": source_id,
        "source_url": source_url,
        "source_mode": "" if mode == "cursor" else mode,
        "source_envelope": envelope,
        "source_profile_ref": profile_ref,
        "document_refs": document_refs,
        "poll_query_seconds": (
            max(1, int(row.get("poll_query_seconds") or 60))
            if mode == "poll"
            else None
        ),
    }


def audit_source_config_version(
    *,
    source_url: object,
    source_mode: object,
    source_envelope: object,
    poll_query_seconds: object,
) -> str:
    """Hash the one canonical set of watch transport facts."""

    try:
        poll_seconds = int(poll_query_seconds or 0)
    except (TypeError, ValueError):
        poll_seconds = 0
    envelope = _source_envelope_config_projection(source_envelope)
    facts = {
        "source_url": str(source_url or ""),
        "source_mode": str(source_mode or ""),
        "source_envelope": envelope,
        "poll_query_seconds": poll_seconds,
    }
    encoded = json.dumps(
        facts,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def audit_prepare_probe_scope_id(
    *,
    task_id: object,
    prepare_request_id: object,
    source_id: object,
    source_mode: object,
    request_facts: object,
    adapter_facts: object,
    record_boundary: object,
    poll_query_seconds: object,
) -> str:
    """Return one immutable prepare-probe generation namespace.

    A prepare turn may revise an Agent-authored adapter or request several
    times.  Probe identity therefore includes only the trusted prepare turn,
    stable source id and normalized mechanical transport inputs.  It never
    includes source content, device type, field meaning or judgment prose.
    """

    task = str(task_id or "").strip()
    request_id = str(prepare_request_id or "").strip()
    stable_source = str(source_id or "").strip()
    if not task or not request_id or not stable_source:
        raise ValueError("prepare probe scope requires task, request and source identity")
    try:
        poll_seconds = max(0, int(poll_query_seconds or 0))
    except (TypeError, ValueError):
        poll_seconds = 0
    facts = {
        "source_id": stable_source,
        "source_mode": str(source_mode or ""),
        "request": deepcopy(request_facts) if isinstance(request_facts, dict) else {},
        "adapter": deepcopy(adapter_facts) if isinstance(adapter_facts, dict) else {},
        "record_boundary": (
            deepcopy(record_boundary) if isinstance(record_boundary, dict) else None
        ),
        "poll_query_seconds": poll_seconds,
    }
    encoded = json.dumps(
        facts,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()[:20]
    return f"prepare:{task}:{request_id}:{digest}"


def _source_envelope_config_projection(value: object) -> dict[str, Any]:
    """Keep only durable request/framing inputs in the runtime version hash."""

    if not isinstance(value, dict):
        return {}
    mode = str(value.get("mode") or "").strip().lower()
    if mode == "file":
        boundary = value.get("record_boundary")
        return {
            "mode": "file",
            "record_boundary": deepcopy(boundary) if isinstance(boundary, dict) else {},
        }
    projected: dict[str, Any] = {
        "mode": mode,
        "record_boundary": str(value.get("record_boundary") or ""),
        "request": (
            deepcopy(value.get("request"))
            if isinstance(value.get("request"), dict)
            else {}
        ),
    }
    if mode == "adapter":
        projected["adapter"] = (
            deepcopy(value.get("adapter"))
            if isinstance(value.get("adapter"), dict)
            else {}
        )
    elif mode == "cursor":
        projected.update(
            {
                "record_list_key": value.get("record_list_key"),
                "cursor_field": value.get("cursor_field"),
                "cursor_semantics": value.get("cursor_semantics"),
                "has_more_field": value.get("has_more_field"),
            }
        )
    return projected


def _canonical_binding(raw: dict[str, Any], *, index: int) -> dict[str, Any]:
    source_id = str(raw.get("source_id") or "").strip()
    if not _SOURCE_ID_RE.fullmatch(source_id):
        raise ValueError(
            f"source_bindings[{index}] source_id 只能包含字母、数字、点、下划线、冒号或短横线"
        )
    url = str(raw.get("url") or "").strip()
    mode = str(raw.get("mode") or "cursor").strip().lower()
    row = {key: deepcopy(raw[key]) for key in AUDIT_SOURCE_OPEN_FIELDS if key in raw}
    row["source_id"] = source_id
    for field in ("record_list_field", "cursor_field", "has_more_field"):
        if field in row:
            row[field] = normalize_top_level_response_field(row[field])
    if url.lower().startswith("file://") or url.startswith("/"):
        if raw.get("http_request") is not None or raw.get("source_adapter") is not None:
            raise ValueError(
                f"source_bindings[{index}] 文件来源不能配置 http_request 或 source_adapter"
            )
        from .sources import normalize_file_url

        row["url"] = normalize_file_url(url)
        row.pop("mode", None)
        return row
    if raw.get("record_boundary") is not None:
        raise ValueError(
            f"source_bindings[{index}].record_boundary 仅适用于文件来源，HTTP 响应记录边界由 record_list_field 表达"
        )
    canonical_url, request = normalize_source_http_request(
        url,
        raw.get("http_request"),
        poll=mode in {"poll", "adapter"},
    )
    if mode == "adapter":
        row["source_adapter"] = normalize_source_adapter(raw.get("source_adapter"))
    elif raw.get("source_adapter") is not None:
        raise ValueError(
            f"source_bindings[{index}].source_adapter 只适用于 mode=adapter"
        )
    row["url"] = canonical_url
    row["mode"] = mode
    row["http_request"] = request
    return row


__all__ = [
    "AUDIT_SOURCE_OPEN_FIELDS",
    "audit_source_binding_by_id",
    "audit_source_binding_from_watch_state",
    "audit_source_config_version",
    "audit_prepare_probe_scope_id",
    "audit_source_binding_schema",
    "audit_source_runtime_projection",
    "merge_audit_source_bindings",
    "normalize_audit_source_bindings",
    "public_audit_source_bindings",
]
