from __future__ import annotations

"""Context-scoped tools for publishing one prepared Audit configuration."""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..ingestion.source_binding import (
    audit_source_binding_by_id,
    audit_source_binding_from_watch_state,
    audit_source_runtime_projection,
    merge_audit_source_bindings,
    public_audit_source_bindings,
)
from ..memory_archive.artifact.reader import (
    ReadToolOutputArtifactRequest,
    read_tool_output_artifact,
)
from ..tooling.models import (
    BaseTool,
    ToolAvailability,
    ToolExecutionResult,
    ToolSpec,
)
from .audit_requirements import (
    canonical_audit_validated_notes,
    has_audit_host_section_marker,
)
from .authority import (
    CONVERSATION_AUDIT_PREPARE_ATTR,
    CONVERSATION_TRANSIENT_WORKSPACE_ATTR,
    CONVERSATION_TURN_REQUEST_ID_ATTR,
    current_conversation_task_attributes,
)
from .workspace_paths import validated_durable_work_path

_SOURCE_PROBE_REF_RE = re.compile(r"^ws-[0-9a-f]{10}$")
_SOURCE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_MAX_SOURCE_PROBE_REFS = 256


@dataclass(frozen=True)
class _AuditPublishRequest:
    task_id: str
    thread_id: str
    prepare_request_id: str
    prompt: str
    validation_status: str
    source_probe_refs: tuple[str, ...]
    source_update_mode: str
    remove_source_ids: tuple[str, ...]


@dataclass(frozen=True)
class _AuditPublishTarget:
    store: object
    link: object


@dataclass(frozen=True)
class _AuditPublishResult:
    ok: bool
    task_id: str = ""
    prepare_request_id: str = ""
    applied: bool = False
    revision: int = 0
    validation_status: str = ""
    audit_status: str = ""
    source_count: int = 0
    retired_source_probe_count: int = 0
    retired_active_source_count: int = 0
    prepare_consumed: bool = False
    applied_source_probe_count: int = 0
    applied_source_ids: list[str] = field(default_factory=list)
    runtime_sync_checked: int = 0
    runtime_sync_degraded: int = 0
    effective_sources: list[dict[str, str]] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    error_code: str = ""
    error_detail: str = ""


class PublishAuditUpdateTool(BaseTool):
    """The only program-authoritative transition from prepared text to effective config."""

    def __init__(self, agent: object):
        self.agent = agent
        self.spec = ToolSpec(
            name="publish_audit_update",
            category="conversation",
            description=(
                "Publish validated operational notes for the exact Audit scoped to this prepare turn. "
                "This tool is unavailable in ordinary chat and never selects an Audit by prose. "
                "A successful probe, watch open, or file write does not update the named Audit; "
                "only a successful call to this tool does. Publishing changes the effective "
                "configuration but the host preserves the current prepare message verbatim as "
                "the authoritative user requirement; derived notes cannot replace it. Publishing "
                "Prefer one final call after all requested checks pass. If an earlier successful call "
                "in this exact still-running prepare turn happened before a later probe completed, "
                "the same turn may call again to atomically amend its own publication. A different "
                "turn can never amend it and requires a new /audit <name> prepare command. "
                "A new or changed source requires opening that exact source once with a stable "
                "source_id and the final transport parameters. Notes, scripts, tests, skills and "
                "documents remain ordinary Audit-workspace materials chosen by the Agent; an "
                "optional source_profile_ref may point at one of them. Pass only the successful "
                "watch_id in source_probe_refs. The host copies the exact persisted transport "
                "facts; do not retype URL, cursor or record-boundary fields. "
                "Keep probe evidence in validation_refs/source_probe_refs instead of "
                "turning it into lasting source instructions. "
                "It does not start monitoring; start remains an explicit "
                "/audit <duration> <name> command."
            ),
            use_cases=[
                "The user explicitly asks to apply the prepared Audit change",
                "A requested probe or validation has completed and the user asked to publish the result",
            ],
            avoid_when=[
                "The user is asking a question, comparing options, or only testing a proposal",
                "The current turn is not an /audit <name> prepare turn",
            ],
            keywords=["publish audit update", "apply audit preparation", "生效审计配置"],
            parameters={
                "effective_prompt": (
                    "Durable cross-source operational notes only. Put source-specific stable "
                    "field meaning and judgment requirements in that source's profile. Do not "
                    "copy transient probe observations or duplicate every source profile here. "
                    "Do not silently rewrite identifiers or business conditions from the user; "
                    "the host separately preserves the current prepare message verbatim and "
                    "gives it precedence."
                ),
                "validation_status": "not_required, passed, or failed.",
                "validation_refs": (
                    "Required when validation_status=passed. Use an exact existing file path "
                    "returned by write_file under this Audit's work/output directory, or an "
                    "exact registered externalized tool artifact_ref/scoped_call_id from this "
                    "Audit. Never invent or edit a call id."
                ),
                "source_probe_refs": (
                    "Optional successful watch_id values returned by final watch_stream(open) "
                    "probes in this exact named Audit. The host atomically materializes and "
                    "applies their persisted transport facts by stable source_id according to "
                    "source_update_mode. Omit this parameter or pass an empty list when only "
                    "changing judgment instructions. Never invent or edit a watch_id."
                ),
                "source_update_mode": (
                    "Optional structured source-set mutation: upsert (default) adds or replaces "
                    "the selected source_ids and keeps all unmentioned sources; replace makes "
                    "source_probe_refs the complete effective source set. A replace that omits "
                    "existing sources must also list every omitted id in remove_source_ids; "
                    "otherwise it fails without changing the Audit. This field expresses collection "
                    "membership only; it never classifies source content or user prose."
                ),
                "remove_source_ids": (
                    "Exact existing source_id values intentionally removed by source_update_mode=replace. "
                    "The list must exactly match the persisted sources omitted from source_probe_refs. "
                    "Omit it for ordinary additions and corrections so unmentioned sources stay active."
                ),
            },
            parameter_schema={
                "effective_prompt": {"type": "string", "minLength": 1},
                "validation_status": {
                    "type": "string",
                    "enum": ["not_required", "passed", "failed"],
                },
                "validation_refs": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1},
                    "maxItems": 32,
                },
                "source_probe_refs": {
                    "type": "array",
                    "maxItems": _MAX_SOURCE_PROBE_REFS,
                    "items": {
                        "type": "string",
                        "pattern": r"^ws-[0-9a-f]{10}$",
                    },
                },
                "source_update_mode": {
                    "type": "string",
                    "enum": ["upsert", "replace"],
                },
                "remove_source_ids": {
                    "type": "array",
                    "maxItems": _MAX_SOURCE_PROBE_REFS,
                    "items": {
                        "type": "string",
                        "pattern": r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$",
                    },
                },
            },
            required_parameters=["effective_prompt", "validation_status"],
            examples=[
                '{"tool":"publish_audit_update","effective_prompt":"完整生效要求",'
                '"validation_status":"not_required"}',
                '{"tool":"publish_audit_update","effective_prompt":"经过探针验证的完整要求",'
                '"validation_status":"passed","validation_refs":'
                '["output/source-a-profile.json"],"source_probe_refs":'
                '["ws-ab12cd34ef"]}',
            ],
            effect="mutating",
            idempotency_scope="operation",
            promotes_task=True,
        )

    def availability(self) -> ToolAvailability:
        attrs = current_conversation_task_attributes(self.agent)
        task_id = str(attrs.get("conversation_task_id") or "").strip()
        prepare_request_id = str(attrs.get(CONVERSATION_TURN_REQUEST_ID_ATTR) or "").strip()
        if (
            attrs.get(CONVERSATION_AUDIT_PREPARE_ATTR) is not True
            or attrs.get(CONVERSATION_TRANSIENT_WORKSPACE_ATTR) is not True
            or not task_id
            or not prepare_request_id
        ):
            return ToolAvailability.unavailable("current turn is not scoped to Audit preparation")
        store = getattr(self.agent, "conversation_store", None)
        try:
            link = store.load_task_link(task_id) if store is not None else None
        except Exception:
            link = None
        if link is None:
            return ToolAvailability.unavailable(
                "current Audit prepare state is unavailable",
                error_code="CONVERSATION_TASK_STATE_UNAVAILABLE",
            )
        pending_request_id = str(getattr(link, "pending_prepare_request_id", "") or "").strip()
        effective_request_id = str(getattr(link, "effective_prepare_request_id", "") or "").strip()
        if not (
            (
                str(getattr(link, "pending_prompt", "") or "").strip()
                and pending_request_id == prepare_request_id
            )
            or effective_request_id == prepare_request_id
        ):
            return ToolAvailability.unavailable(
                "current Audit prepare revision has already been published",
                error_code="AUDIT_PREPARE_ALREADY_PUBLISHED",
            )
        return ToolAvailability.ready()

    def execute(self, params: dict[str, Any]) -> ToolExecutionResult:
        request, error = _audit_publish_request(self.agent, params)
        if error is not None or request is None:
            return _result(error or _AuditPublishResult(False))
        target, error = _audit_publish_target(self.agent, request)
        if error is not None or target is None:
            return _result(error or _AuditPublishResult(False))
        revision_error = _audit_publish_revision_error(target.link, request)
        if revision_error is not None:
            return _result(revision_error)
        if request.validation_status == "failed":
            return _result(_existing_revision_result(target.link, request))
        refs = _validated_evidence_refs(
            self.agent,
            target.link,
            params.get("validation_refs"),
        )
        evidence_error = _audit_publish_evidence_error(request, refs)
        if evidence_error is not None:
            return _result(evidence_error)
        bindings, error = _audit_publish_bindings(
            self.agent,
            target.link,
            request,
        )
        if error is not None:
            return _result(error)
        updated = _commit_audit_publish(target, request, refs or [], bindings)
        if updated is None:
            return _result(_AuditPublishResult(False, error_code="AUDIT_UPDATE_CONFLICT"))
        removed_source_ids = _removed_source_ids(target.link, updated)
        retired_active_sources = _retire_removed_audit_sources(
            self.agent,
            request.task_id,
            removed_source_ids,
        )
        retired = _retire_prepare_watches(self.agent, request.task_id)
        runtime_sync = _reconcile_published_audit_runtime(self.agent)
        return _result(
            _successful_publish_result(
                updated,
                request,
                refs or [],
                bindings,
                retired,
                retired_active_sources,
                runtime_sync,
            )
        )


def _audit_publish_request(
    agent: object,
    params: dict[str, Any],
) -> tuple[_AuditPublishRequest | None, _AuditPublishResult | None]:
    attrs = current_conversation_task_attributes(agent)
    source_update_mode = str(params.get("source_update_mode") or "upsert").strip().lower()
    if source_update_mode not in {"upsert", "replace"}:
        return None, _AuditPublishResult(
            False,
            error_code="AUDIT_SOURCE_UPDATE_MODE_INVALID",
            error_detail="source_update_mode 只支持 upsert 或 replace",
        )
    try:
        source_probe_refs = _normalized_source_probe_refs(params.get("source_probe_refs"))
    except ValueError as exc:
        return None, _AuditPublishResult(
            False,
            error_code="AUDIT_SOURCE_PROBE_REFS_INVALID",
            error_detail=str(exc),
        )
    try:
        remove_source_ids = _normalized_remove_source_ids(params.get("remove_source_ids"))
    except ValueError as exc:
        return None, _AuditPublishResult(
            False,
            error_code="AUDIT_SOURCE_BINDINGS_INVALID",
            error_detail=str(exc),
        )
    request = _AuditPublishRequest(
        task_id=str(attrs.get("conversation_task_id") or "").strip(),
        thread_id=str(attrs.get("conversation_thread_id") or "").strip(),
        prepare_request_id=str(attrs.get(CONVERSATION_TURN_REQUEST_ID_ATTR) or "").strip(),
        prompt=canonical_audit_validated_notes(str(params.get("effective_prompt") or "").strip()),
        validation_status=str(params.get("validation_status") or "").strip().lower(),
        source_probe_refs=source_probe_refs,
        source_update_mode=source_update_mode,
        remove_source_ids=remove_source_ids,
    )
    if (
        attrs.get(CONVERSATION_AUDIT_PREPARE_ATTR) is not True
        or attrs.get(CONVERSATION_TRANSIENT_WORKSPACE_ATTR) is not True
        or not request.task_id
        or not request.thread_id
        or not request.prepare_request_id
        or not request.prompt
        or request.validation_status not in {"not_required", "passed", "failed"}
    ):
        return None, _AuditPublishResult(
            False,
            error_code="AUDIT_PREPARE_SCOPE_REQUIRED",
        )
    if has_audit_host_section_marker(request.prompt):
        return None, _AuditPublishResult(
            False,
            error_code="AUDIT_EFFECTIVE_PROMPT_HOST_MARKER_FORBIDDEN",
            error_detail=(
                "effective_prompt 只能包含派生运行说明，不能复制 Audit 的宿主标题、"
                "用户原文区或生命周期区；请保留说明正文后重试。"
            ),
        )
    return request, None


def _audit_publish_target(
    agent: object,
    request: _AuditPublishRequest,
) -> tuple[_AuditPublishTarget | None, _AuditPublishResult | None]:
    store = getattr(agent, "conversation_store", None)
    if store is None:
        return None, _AuditPublishResult(
            False,
            error_code="CONVERSATION_TASK_STATE_UNAVAILABLE",
        )
    try:
        link = store.load_task_link(request.task_id)
    except Exception:
        return None, _AuditPublishResult(
            False,
            error_code="CONVERSATION_TASK_STATE_UNAVAILABLE",
        )
    if (
        link is None
        or str(getattr(link, "thread_id", "") or "") != request.thread_id
        or str(getattr(link, "work_kind", "") or "").strip().lower() != "audit"
    ):
        return None, _AuditPublishResult(
            False,
            error_code="AUDIT_PREPARE_SCOPE_REQUIRED",
        )
    return _AuditPublishTarget(store, link), None


def _audit_publish_revision_error(
    link: object,
    request: _AuditPublishRequest,
) -> _AuditPublishResult | None:
    pending_prompt = str(getattr(link, "pending_prompt", "") or "").strip()
    pending_request_id = str(getattr(link, "pending_prepare_request_id", "") or "").strip()
    effective_request_id = str(getattr(link, "effective_prepare_request_id", "") or "").strip()
    if (pending_prompt and pending_request_id == request.prepare_request_id) or (
        not pending_prompt and effective_request_id == request.prepare_request_id
    ):
        return None
    return _AuditPublishResult(
        False,
        prepare_request_id=request.prepare_request_id,
        revision=int(getattr(link, "effective_revision", 0) or 0),
        audit_status=str(getattr(link, "status", "") or ""),
        source_count=len(getattr(link, "effective_source_bindings", ()) or ()),
        error_code="AUDIT_PREPARE_ALREADY_PUBLISHED",
    )


def _existing_revision_result(
    link: object,
    request: _AuditPublishRequest,
) -> _AuditPublishResult:
    return _AuditPublishResult(
        True,
        task_id=request.task_id,
        prepare_request_id=request.prepare_request_id,
        revision=int(getattr(link, "effective_revision", 0) or 0),
        validation_status=request.validation_status,
        audit_status=str(getattr(link, "status", "") or ""),
        source_count=len(getattr(link, "effective_source_bindings", ()) or ()),
    )


def _audit_publish_evidence_error(
    request: _AuditPublishRequest,
    refs: list[str] | None,
) -> _AuditPublishResult | None:
    if refs is None or (
        request.validation_status == "passed"
        and not refs
        and not request.source_probe_refs
    ):
        return _AuditPublishResult(
            False,
            error_code="AUDIT_VALIDATION_EVIDENCE_INVALID",
            error_detail=(
                "validation_refs 必须是当前 Audit 的 work/output 内真实文件路径，"
                "或当前 Audit 已登记成功工具产物的 scoped_call_id/artifact_ref。"
                "过程说明请用 write_file 写到 work/<文件名>，再原样使用工具返回的 path；"
                "不要把 owner 根目录、普通 workspace 路径或自行拼接的 audit:// 引用当成发布证据。"
            ),
        )
    if request.source_probe_refs and request.validation_status != "passed":
        return _AuditPublishResult(
            False,
            error_code="AUDIT_VALIDATION_EVIDENCE_INVALID",
        )
    return None


def _audit_publish_bindings(
    agent: object,
    link: object,
    request: _AuditPublishRequest,
) -> tuple[tuple[dict[str, Any], ...], _AuditPublishResult | None]:
    bindings: tuple[dict[str, Any], ...] = ()
    if request.source_probe_refs:
        try:
            bindings = _validated_source_probe_bindings(
                agent,
                link,
                request.source_probe_refs,
                request.prepare_request_id,
            )
        except ValueError as exc:
            return (), _AuditPublishResult(
                False,
                error_code="AUDIT_SOURCE_PROBE_REFS_INVALID",
                error_detail=str(exc),
            )
    rebind_error = _active_source_rebind_error(link, bindings)
    if rebind_error is not None:
        return (), rebind_error
    removal_error = _source_removal_confirmation_error(link, request, bindings)
    if removal_error is not None:
        return (), removal_error
    try:
        merge_audit_source_bindings(
            ()
            if request.source_update_mode == "replace"
            else getattr(link, "effective_source_bindings", ()) or (),
            bindings,
        )
    except ValueError as exc:
        return (), _AuditPublishResult(
            False,
            error_code="AUDIT_SOURCE_BINDINGS_INVALID",
            error_detail=str(exc),
        )
    return bindings, None


def _source_removal_confirmation_error(
    link: object,
    request: _AuditPublishRequest,
    bindings: tuple[dict[str, Any], ...],
) -> _AuditPublishResult | None:
    """Require exact structured ids before a publication can retire sources."""

    if request.source_update_mode != "replace":
        if request.remove_source_ids:
            return _AuditPublishResult(
                False,
                error_code="AUDIT_SOURCE_BINDINGS_INVALID",
                error_detail="remove_source_ids 只允许与 source_update_mode=replace 一起使用",
            )
        return None
    current_ids = {
        str(row.get("source_id") or "").strip()
        for row in (getattr(link, "effective_source_bindings", ()) or ())
        if isinstance(row, dict) and str(row.get("source_id") or "").strip()
    }
    incoming_ids = {
        str(row.get("source_id") or "").strip()
        for row in bindings
        if str(row.get("source_id") or "").strip()
    }
    removed_ids = current_ids - incoming_ids
    confirmed_ids = set(request.remove_source_ids)
    if confirmed_ids == removed_ids:
        return None
    return _AuditPublishResult(
        False,
        error_code="AUDIT_SOURCE_BINDINGS_INVALID",
        error_detail=(
            "replace 会移除未提交的已有来源；remove_source_ids 必须与将被移除的来源精确一致。"
            f" expected={sorted(removed_ids)} confirmed={sorted(confirmed_ids)}"
        ),
    )


def _active_source_rebind_error(
    link: object,
    bindings: tuple[dict[str, Any], ...],
) -> _AuditPublishResult | None:
    """Reject only transport identity changes that cannot preserve progress."""

    if str(getattr(link, "status", "") or "").strip().lower() != "active":
        return None
    current = getattr(link, "effective_source_bindings", ()) or ()
    for binding in bindings:
        source_id = str(binding.get("source_id") or "").strip()
        previous = audit_source_binding_by_id(current, source_id)
        if previous is None:
            continue
        try:
            old_runtime = audit_source_runtime_projection(previous)
            new_runtime = audit_source_runtime_projection(binding)
        except ValueError as exc:
            return _AuditPublishResult(
                False,
                error_code="AUDIT_SOURCE_BINDINGS_INVALID",
                error_detail=str(exc),
            )
        old_envelope = old_runtime.get("source_envelope")
        new_envelope = new_runtime.get("source_envelope")
        old_boundary = (
            old_envelope.get("record_boundary")
            if isinstance(old_envelope, dict) and old_envelope.get("mode") == "file"
            else None
        )
        new_boundary = (
            new_envelope.get("record_boundary")
            if isinstance(new_envelope, dict) and new_envelope.get("mode") == "file"
            else None
        )
        if any(
            (
                old_runtime.get("source_url") != new_runtime.get("source_url"),
                old_runtime.get("source_mode") != new_runtime.get("source_mode"),
                old_boundary != new_boundary,
            )
        ):
            return _AuditPublishResult(
                False,
                error_code="AUDIT_SOURCE_REBIND_REQUIRED",
                error_detail=(
                    f"运行中的来源 {source_id} 改变了地址、推进方式或文件记录边界；"
                    "请用新的 source_id 建立新来源，不能静默继承旧游标。"
                ),
            )
    return None


def _commit_audit_publish(
    target: _AuditPublishTarget,
    request: _AuditPublishRequest,
    refs: list[str],
    bindings: tuple[dict[str, Any], ...],
) -> object | None:
    payload: dict[str, object] = {
        "task_id": request.task_id,
        "prompt": request.prompt,
        "prepare_request_id": request.prepare_request_id,
        "evidence_refs": refs,
        "source_update_mode": request.source_update_mode,
    }
    if request.source_probe_refs or request.source_update_mode == "replace":
        payload["source_bindings"] = list(bindings)
    try:
        with target.store.task_transition_guard(request.task_id):
            return target.store.publish_audit_effective_prompt(payload)
    except Exception:
        return None


def _retire_prepare_watches(agent: object, task_id: str) -> int:
    try:
        from ..ingestion.watch_state import close_prepare_watches_for_task

        owner_home = Path(
            str(
                getattr(
                    getattr(agent, "home_paths", None),
                    "owner_home_dir",
                    "",
                )
                or ""
            )
        )
        return len(close_prepare_watches_for_task(owner_home, task_id))
    except (OSError, RuntimeError, ValueError):
        # Durable publish already committed.  A later prepare/start can retry
        # typed probe retirement without rolling back source authority.
        return 0


def _removed_source_ids(previous: object, updated: object) -> tuple[str, ...]:
    before = {
        str(row.get("source_id") or "").strip()
        for row in (getattr(previous, "effective_source_bindings", ()) or ())
        if isinstance(row, dict) and str(row.get("source_id") or "").strip()
    }
    after = {
        str(row.get("source_id") or "").strip()
        for row in (getattr(updated, "effective_source_bindings", ()) or ())
        if isinstance(row, dict) and str(row.get("source_id") or "").strip()
    }
    return tuple(sorted(before - after))


def _retire_removed_audit_sources(
    agent: object,
    task_id: str,
    source_ids: tuple[str, ...],
) -> int:
    if not source_ids:
        return 0
    try:
        from ..ingestion.source_worker import retire_published_audit_sources

        return retire_published_audit_sources(agent, task_id, source_ids)
    except (OSError, RuntimeError, TypeError, ValueError):
        # The task link is already authoritative. The ordinary supervisor will
        # retry cleanup; never roll the durable source-set revision back.
        return 0


def _reconcile_published_audit_runtime(agent: object) -> dict[str, int]:
    """Apply a committed revision through the existing idempotent reconciler."""

    try:
        from ..ingestion.source_worker import reconcile_audit_source_workers

        summary = reconcile_audit_source_workers(agent)
    except (OSError, RuntimeError, TypeError, ValueError):
        # Publication is durable authority.  The existing periodic reconciler
        # retries a failed immediate projection; never roll the revision back.
        return {"checked": 0, "degraded": 1}
    return {
        "checked": max(0, int(summary.get("checked") or 0)),
        "degraded": max(0, int(summary.get("degraded") or 0)),
    }


def _successful_publish_result(
    updated: object,
    request: _AuditPublishRequest,
    refs: list[str],
    bindings: tuple[dict[str, Any], ...],
    retired_probe_count: int,
    retired_active_source_count: int,
    runtime_sync: dict[str, int],
) -> _AuditPublishResult:
    sources = getattr(updated, "effective_source_bindings", ()) or ()
    return _AuditPublishResult(
        True,
        task_id=request.task_id,
        prepare_request_id=request.prepare_request_id,
        applied=True,
        revision=int(getattr(updated, "effective_revision", 0) or 0),
        validation_status=request.validation_status,
        evidence_refs=refs,
        audit_status=str(getattr(updated, "status", "") or ""),
        source_count=len(sources),
        retired_source_probe_count=retired_probe_count,
        retired_active_source_count=retired_active_source_count,
        prepare_consumed=True,
        applied_source_probe_count=len(request.source_probe_refs),
        applied_source_ids=[
            str(binding.get("source_id") or "")
            for binding in bindings
            if str(binding.get("source_id") or "").strip()
        ],
        runtime_sync_checked=max(0, int(runtime_sync.get("checked") or 0)),
        runtime_sync_degraded=max(0, int(runtime_sync.get("degraded") or 0)),
        effective_sources=_public_effective_sources(sources),
    )


def _validated_evidence_refs(agent: object, link: object, value: object) -> list[str] | None:
    raw_refs = value if isinstance(value, list) else []
    owner_home = str(
        getattr(getattr(agent, "home_paths", None), "owner_home_dir", "") or ""
    ).strip()
    try:
        root = validated_durable_work_path(
            owner_home,
            str(getattr(link, "task_path", "") or ""),
            "audit",
            require_directory=True,
        )
    except (OSError, RuntimeError, ValueError):
        return None
    refs: list[str] = []
    for item in raw_refs:
        text = str(item or "").strip()
        if not text:
            return None
        local_file = _validated_local_evidence_file(root, text)
        if local_file:
            refs.append(local_file)
            continue
        tool_artifact = _validated_tool_artifact_ref(root, text)
        if tool_artifact:
            refs.append(tool_artifact)
            continue
        return None
    return list(dict.fromkeys(refs))


def _normalized_source_probe_refs(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > _MAX_SOURCE_PROBE_REFS:
        raise ValueError(
            f"source_probe_refs 必须是最多 {_MAX_SOURCE_PROBE_REFS} 项的 watch_id 数组"
        )
    refs: list[str] = []
    seen: set[str] = set()
    for index, raw in enumerate(value):
        ref = str(raw or "").strip()
        if not _SOURCE_PROBE_REF_RE.fullmatch(ref):
            raise ValueError(f"source_probe_refs[{index}] 不是有效 watch_id")
        if ref in seen:
            raise ValueError(f"source_probe_refs[{index}] 重复: {ref}")
        seen.add(ref)
        refs.append(ref)
    return tuple(refs)


def _normalized_remove_source_ids(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > _MAX_SOURCE_PROBE_REFS:
        raise ValueError(
            f"remove_source_ids 必须是最多 {_MAX_SOURCE_PROBE_REFS} 项的 source_id 数组"
        )
    ids: list[str] = []
    seen: set[str] = set()
    for index, raw in enumerate(value):
        source_id = str(raw or "").strip()
        if not _SOURCE_ID_RE.fullmatch(source_id):
            raise ValueError(f"remove_source_ids[{index}] 不是有效 source_id")
        if source_id in seen:
            raise ValueError(f"remove_source_ids[{index}] 重复: {source_id}")
        seen.add(source_id)
        ids.append(source_id)
    return tuple(ids)


def _validated_source_probe_bindings(
    agent: object,
    link: object,
    probe_refs: tuple[str, ...],
    prepare_request_id: str,
) -> tuple[dict[str, Any], ...]:
    """Resolve exact current-Audit probe handles into canonical transports."""

    from ..ingestion.source_binding import audit_prepare_probe_scope_id
    from ..ingestion.watch_state import load_state, watch_id_for

    owner_home_text = str(
        getattr(getattr(agent, "home_paths", None), "owner_home_dir", "") or ""
    ).strip()
    try:
        audit_root = validated_durable_work_path(
            owner_home_text,
            str(getattr(link, "task_path", "") or ""),
            "audit",
            require_directory=True,
        )
    except (OSError, RuntimeError, ValueError):
        raise ValueError("当前 Audit 工作区不可用，不能发布来源探针") from None

    owner_home = Path(owner_home_text)
    task_id = str(getattr(link, "task_id", "") or "").strip()
    bindings: list[dict[str, Any]] = []
    for index, probe_ref in enumerate(probe_refs):
        state = load_state(owner_home, probe_ref)
        if state is None:
            raise ValueError(f"source_probe_refs[{index}] 不存在或状态不可读")
        envelope = getattr(state, "source_envelope", None)
        envelope = envelope if isinstance(envelope, dict) else {}
        expected_scope = audit_prepare_probe_scope_id(
            task_id=task_id,
            prepare_request_id=prepare_request_id,
            source_id=getattr(state, "source_id", ""),
            source_mode=getattr(state, "source_mode", ""),
            request_facts=envelope.get("request"),
            adapter_facts=envelope.get("adapter"),
            record_boundary=(
                envelope.get("record_boundary") if envelope.get("mode") == "file" else None
            ),
            poll_query_seconds=getattr(getattr(state, "tuning", None), "poll_query_seconds", 0),
        )
        if (
            getattr(state, "audit_guarantee", False) is True
            or str(getattr(state, "prepare_root_task_id", "") or "") != task_id
            or probe_ref
            != watch_id_for(
                owner_home,
                str(getattr(state, "source_url", "") or ""),
                expected_scope,
            )
        ):
            raise ValueError(f"source_probe_refs[{index}] 不属于当前命名 Audit 的准备轮")
        profile = str(getattr(state, "source_profile_ref", "") or "").strip()
        resolved_profile = ""
        if profile:
            resolved_profile = _validated_local_evidence_file(audit_root, profile)
            if not resolved_profile:
                raise ValueError(
                    f"source_probe_refs[{index}] 的可选 source_profile_ref 必须是当前 "
                    "Audit 工作区内的真实文件"
                )
        try:
            binding = audit_source_binding_from_watch_state(state)
        except ValueError as exc:
            raise ValueError(f"source_probe_refs[{index}] 尚未形成成功探针: {exc}") from None
        if resolved_profile:
            binding["source_profile_ref"] = resolved_profile
        bindings.append(binding)
    return tuple(bindings)


def _validated_local_evidence_file(root: Path, value: str) -> str:
    try:
        candidate = Path(value).expanduser()
        candidate = candidate if candidate.is_absolute() else root / candidate
        candidate = candidate.resolve(strict=True)
        candidate.relative_to(root)
        tool_artifact_root = (root / "work" / "blobs" / "tool_outputs").resolve(strict=False)
        candidate.relative_to(tool_artifact_root)
    except (OSError, RuntimeError, ValueError):
        pass
    else:
        # Indexed tool artifacts must pass their own success/hash checks below;
        # they cannot be smuggled through the generic owner-local file branch.
        return ""
    try:
        candidate.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return ""
    return str(candidate) if candidate.is_file() else ""


def _validated_tool_artifact_ref(root: Path, artifact_ref: str) -> str:
    """Resolve an indexed successful tool artifact inside this exact Audit workspace."""

    payload = read_tool_output_artifact(
        ReadToolOutputArtifactRequest(
            root=root / "work",
            artifact_ref=artifact_ref,
            max_chars=1,
        )
    )
    if payload.get("ok") is not True or payload.get("content_hash_verified") is not True:
        return ""
    artifact_path = str(payload.get("artifact_path") or "").strip()
    if not artifact_path:
        return ""
    try:
        path = Path(artifact_path).expanduser().resolve(strict=True)
        path.relative_to((root / "work" / "blobs" / "tool_outputs").resolve(strict=False))
        wrapper = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError):
        return ""
    if wrapper.get("kind") != "tool_output" or wrapper.get("ok") is not True:
        return ""
    return str(path)


def _result(result: _AuditPublishResult) -> ToolExecutionResult:
    payload = {
        "ok": result.ok,
        "applied": result.applied,
        "audit_id": result.task_id,
        "prepare_request_id": result.prepare_request_id,
        "effective_revision": result.revision,
        "validation_status": result.validation_status,
        "audit_status": result.audit_status,
        "monitoring_started": False,
        "source_count": max(0, int(result.source_count or 0)),
        "source_binding_change_applied": bool(
            result.applied and result.applied_source_probe_count > 0
        ),
        "applied_source_probe_count": max(
            0,
            int(result.applied_source_probe_count or 0),
        ),
        "applied_source_ids": list(result.applied_source_ids),
        "retired_source_probe_count": max(
            0,
            int(result.retired_source_probe_count or 0),
        ),
        "retired_active_source_count": max(
            0,
            int(result.retired_active_source_count or 0),
        ),
        "runtime_sync_checked": max(0, int(result.runtime_sync_checked or 0)),
        "runtime_sync_degraded": max(0, int(result.runtime_sync_degraded or 0)),
        "prepare_consumed": result.prepare_consumed,
        "effective_sources": list(result.effective_sources),
        "evidence_refs": list(result.evidence_refs),
        "error_code": result.error_code,
        "error_detail": str(result.error_detail or "")[:800],
    }
    return ToolExecutionResult(
        "publish_audit_update",
        result.ok,
        json.dumps(payload, ensure_ascii=False),
        result_envelope={"audit_publish": payload},
        error_code=result.error_code,
    )


def _public_effective_sources(value: object) -> list[dict[str, str]]:
    """Return bounded, non-secret persisted facts for model-authored closeout."""

    return [
        {
            "source_id": str(row.get("source_id") or ""),
            "url": str(row.get("url") or ""),
        }
        for row in public_audit_source_bindings(value)
    ]


__all__ = ["PublishAuditUpdateTool"]
