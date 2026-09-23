"""/audit 的 typed activation facts shared by Gateway, Agent and ingestion.

The slash command is parsed once at the trusted conversation boundary.  Only
its opaque task body reaches the model, while the runtime mode is carried in
``task_attributes`` across turns and descendants.  No prompt text is scanned
to activate, authorize, route or settle Audit work.
"""

from __future__ import annotations

import hashlib
from typing import Any

# task_attributes / spawn 继承里承载"本任务树在保证档"的结构化键(跨轮/跨子代理稳定)。
AUDIT_ATTR = "audit_guarantee"
AUDIT_WINDOW_ATTR = "audit_window_seconds"
# Absolute task expiry is minted by the durable conversation task store.  A
# descendant uses it to calculate the *remaining* watch window instead of
# restarting the user's full duration after setup or a background wake.
AUDIT_DEADLINE_ATTR = "audit_deadline_unix"
# A stable named Audit can run more than once. The host increments this exact
# epoch on every explicit start. Durable source cursors stay stable across
# epochs; runner scopes, leases and trace facts use it only as mechanical
# isolation, never as business meaning.
AUDIT_RUN_EPOCH_ATTR = "audit_run_epoch"
# The task body is opaque model context, not machine authority.  It is carried
# separately because the history-free audit judge intentionally has no access
# to the long-lived parent/child conversation.
AUDIT_OBJECTIVE_ATTR = "audit_objective"
# Exact user-authored body of this run's start command.  It is model context,
# not transport/configuration authority: the published objective and source
# bindings still win on conflicts, while restart/recovery must not lose the
# user's run-specific request.
AUDIT_RUN_PROMPT_ATTR = "audit_run_prompt"
# One logical source worker owns one watch inside an Audit.  These typed
# attributes are runtime authority; names, roles and generated prompt text are
# deliberately not used for routing or authorization.
AUDIT_SOURCE_WORKER_ATTR = "audit_source_worker"
AUDIT_SOURCE_ID_ATTR = "audit_source_id"
AUDIT_SOURCE_WATCH_ID_ATTR = "audit_source_watch_id"
AUDIT_SOURCE_WORKER_KEY_ATTR = "audit_source_worker_key"
AUDIT_SOURCE_OWNER_HOME_ATTR = "audit_source_owner_home"
AUDIT_SOURCE_PROFILE_REF_ATTR = "audit_source_profile_ref"
AUDIT_SOURCE_DOCUMENT_REFS_ATTR = "audit_source_document_refs"
AUDIT_SOURCE_CONFIG_VERSION_ATTR = "audit_source_config_version"
# A direct Audit worker may need one short model turn to open its source before
# the durable watch id exists.  This is a lifecycle phase of the same source
# worker, not a second worker type or execution runtime.
AUDIT_SOURCE_BINDING_PENDING_ATTR = "audit_source_binding_pending"
# A source can become durably bound during the model turn that opened it.  That
# turn still owns the immutable pre-binding prompt/tool snapshot, so its prose
# is stale by construction.  The exact attempt id lets result projection drop
# only that prose while retaining the raw runner archive.
AUDIT_SOURCE_CONTEXT_REFRESH_ATTEMPT_ATTR = (
    "audit_source_context_refresh_attempt_id"
)
# Exact transport facts published by the named Audit and the one row selected
# for a pending child.  These are program-owned mechanical bindings, not
# semantic source templates or model-authored routing prose.
AUDIT_SOURCE_BINDINGS_ATTR = "audit_source_bindings"
AUDIT_SOURCE_OPEN_ATTR = "audit_source_open"
AUDIT_SOURCE_OPEN_FIELDS = (
    "url",
    "mode",
    "http_request",
    "source_adapter",
    "record_list_field",
    "cursor_field",
    "cursor_semantics",
    "has_more_field",
    "record_boundary",
    "source_id",
    "source_profile_ref",
    "document_refs",
    "poll_query_seconds",
)
# LLM: This immutable minimum tool surface is shared by creation, mid-turn
# adoption, model exposure and the final execution gate.
# 函数用途: 一源一子代理无论在哪条路径绑定，都只能看见并执行同一组最小工具。
AUDIT_SOURCE_WORKER_TOOLS = (
    "watch_stream",
    "read_file",
    "read_artifact",
)
AUDIT_SOURCE_BINDING_TOOLS = (
    "watch_stream",
    "read_file",
    "read_artifact",
)


# LLM: Source-worker identity is composed only from durable typed ids; generated
# names and model prose never participate in authorization or idempotency.
# 函数用途: 为一条 Audit 数据源生成跨重启稳定、可直接核对的工作者身份。
def audit_source_worker_key(audit_id: object, watch_id: object) -> str:
    audit = str(audit_id or "").strip()
    watch = str(watch_id or "").strip()
    if not audit or not watch:
        return ""
    return f"audit-source:{audit}:{watch}"


def audit_watch_scope_id(audit_id: object, run_epoch: object = 0) -> str:
    """Return the stable watch namespace for one named Audit.

    ``run_epoch`` is intentionally ignored.  A new run owns a new deadline,
    runner attempt and lease, but it continues each source from the same
    durable mechanical checkpoint.  Putting the epoch into watch identity
    would create a fresh cursor and silently replay already-ACKed history.
    """

    del run_epoch
    return str(audit_id or "").strip()


def audit_source_work_scope_key(worker_key: object, run_epoch: object = 0) -> str:
    """Return one source worker's idempotency scope within an Audit run.

    The logical ``worker_key`` remains stable across runs.  The epoch belongs
    only to execution identity, so a continued Audit receives a fresh runner
    attempt while retries and duplicate provisioning inside that run reuse one
    child.
    """

    selected = str(worker_key or "").strip()
    if not selected:
        return ""
    try:
        epoch = max(0, int(run_epoch or 0))
    except (TypeError, ValueError):
        epoch = 0
    identity = selected if epoch <= 0 else f"{selected}:run:{epoch}"
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    return f"audit-source-scope:{digest}"


# LLM: Treat the internal exact-tool and active-lineage exceptions as trusted
# only when all source-worker attributes form one self-consistent identity.
# 函数用途: 校验任务属性确实描述一条完整的一源一子代理绑定，避免半套字段获得特殊待遇。
def structured_audit_source_worker_attributes(attrs: Any) -> bool:
    if not isinstance(attrs, dict) or attrs.get(AUDIT_SOURCE_WORKER_ATTR) is not True:
        return False
    from ..conversation.authority import CONVERSATION_REQUEST_ID_ATTR

    audit_id = str(attrs.get(CONVERSATION_REQUEST_ID_ATTR) or "").strip()
    source_id = str(attrs.get(AUDIT_SOURCE_ID_ATTR) or "").strip()
    watch_id = str(attrs.get(AUDIT_SOURCE_WATCH_ID_ATTR) or "").strip()
    worker_key = str(attrs.get(AUDIT_SOURCE_WORKER_KEY_ATTR) or "").strip()
    owner_home = str(attrs.get(AUDIT_SOURCE_OWNER_HOME_ATTR) or "").strip()
    return bool(
        audit_id
        and source_id
        and watch_id
        and owner_home
        and worker_key == audit_source_worker_key(audit_id, watch_id)
    )


def structured_audit_source_binding_attributes(attrs: Any) -> bool:
    """Validate the short pre-watch phase of one Audit source worker.

    The phase has no source authority yet: it cannot pull, acknowledge or
    report findings.  It exists only so a child is least-privilege before its
    first successful ``watch_stream(open)`` establishes the durable binding.
    """

    if (
        not isinstance(attrs, dict)
        or attrs.get(AUDIT_SOURCE_BINDING_PENDING_ATTR) is not True
        or attrs.get(AUDIT_ATTR) is not True
        or attrs.get(AUDIT_SOURCE_WORKER_ATTR) is True
    ):
        return False
    from ..conversation.authority import CONVERSATION_REQUEST_ID_ATTR

    return bool(str(attrs.get(CONVERSATION_REQUEST_ID_ATTR) or "").strip())


def structured_audit_supervised_worker_attributes(attrs: Any) -> bool:
    """Return whether the run is a bound or not-yet-bound Audit source worker."""

    return structured_audit_source_worker_attributes(
        attrs
    ) or structured_audit_source_binding_attributes(attrs)


def audit_worker_tool_scope(attrs: Any) -> tuple[str, ...]:
    """Return the immutable tool scope for a trusted Audit worker phase."""

    if structured_audit_source_worker_attributes(attrs):
        # A bound source with no explicit documents has no legitimate file
        # read target.  Do not advertise generic run checkpoints or artifact
        # readers merely because the process-wide registry contains them.
        # When references are pinned, both readers remain available because a
        # reference may resolve to either a local owner file or an externalized
        # tool artifact; the exact read boundary still decides what can open.
        refs = [
            str(attrs.get(AUDIT_SOURCE_PROFILE_REF_ATTR) or "").strip(),
            *[
                str(item or "").strip()
                for item in attrs.get(AUDIT_SOURCE_DOCUMENT_REFS_ATTR, []) or []
            ],
        ]
        if any(refs):
            return AUDIT_SOURCE_WORKER_TOOLS
        # 无显式 refs: 只给采集面(watch_stream); 文件工具留给有 refs 的读取边界。
        return AUDIT_SOURCE_WORKER_TOOLS[:1]
    if structured_audit_source_binding_attributes(attrs):
        return AUDIT_SOURCE_BINDING_TOOLS
    return ()


def audit_worker_slice_seconds(agent: object) -> int:
    """Bound one model attempt even when ordinary runner timeouts are disabled."""

    raw = getattr(
        getattr(agent, "config", None),
        "lease_stale_without_heartbeat_seconds",
        300,
    )
    try:
        seconds = max(30, int(raw or 300))
    except (TypeError, ValueError):
        seconds = 300
    return max(60, min(900, seconds))


def attributes_request_audit(attrs: Any) -> bool:
    """task_attributes(或子代理 task.attributes)里是否已结构化置位保证档标志。
    这是跨轮/跨 spawn 树可靠的激活判据——一次盖上,之后每轮每个子代理都读得到。"""
    return isinstance(attrs, dict) and bool(attrs.get(AUDIT_ATTR))


# LLM: Runner-local child attributes take precedence over the reusable agent's
# main-turn parameters. This keeps concurrent descendants from reading a stale
# parent task while preserving the main-agent fallback.
# 函数用途: 读取当前实际执行者的 Audit 属性，兼容主代理与线程隔离的子代理上下文。
def current_audit_attributes(agent: object) -> dict[str, Any] | None:
    try:
        from ..runtime_context import current_task_attributes

        attrs = current_task_attributes(agent)
        if isinstance(attrs, dict) and attrs:
            return attrs
    except Exception:
        pass
    current = getattr(agent, "_current_run_params", None)
    attrs = getattr(current, "task_attributes", None) if current is not None else None
    return attrs if isinstance(attrs, dict) else None


# LLM: Resolve the durable Audit lineage only from typed run attributes and ids.
# Keep this shared by ingestion and orchestration so descendants never substitute
# their own run id for the named Audit root or infer identity from prompt text.
# 函数用途: 取得当前任务树所属 Audit 的稳定根编号，供数据源隔离和等待活性边界共用。
def audit_lineage_task_id(agent: object) -> str:
    current = getattr(agent, "_current_run_params", None)
    attrs = current_audit_attributes(agent)
    if isinstance(attrs, dict):
        from ..conversation.authority import CONVERSATION_REQUEST_ID_ATTR

        inherited = str(attrs.get(CONVERSATION_REQUEST_ID_ATTR) or "").strip()
        if inherited:
            return inherited
    return str(
        getattr(current, "request_id", "")
        or getattr(current, "run_id", "")
        or ""
    ).strip()


__all__ = [
    "AUDIT_ATTR",
    "AUDIT_DEADLINE_ATTR",
    "AUDIT_OBJECTIVE_ATTR",
    "AUDIT_RUN_PROMPT_ATTR",
    "AUDIT_RUN_EPOCH_ATTR",
    "AUDIT_SOURCE_CONFIG_VERSION_ATTR",
    "AUDIT_SOURCE_BINDING_PENDING_ATTR",
    "AUDIT_SOURCE_BINDING_TOOLS",
    "AUDIT_SOURCE_BINDINGS_ATTR",
    "AUDIT_SOURCE_DOCUMENT_REFS_ATTR",
    "AUDIT_SOURCE_ID_ATTR",
    "AUDIT_SOURCE_PROFILE_REF_ATTR",
    "AUDIT_SOURCE_WATCH_ID_ATTR",
    "AUDIT_SOURCE_OWNER_HOME_ATTR",
    "AUDIT_SOURCE_OPEN_ATTR",
    "AUDIT_SOURCE_OPEN_FIELDS",
    "AUDIT_SOURCE_WORKER_ATTR",
    "AUDIT_SOURCE_WORKER_KEY_ATTR",
    "AUDIT_SOURCE_WORKER_TOOLS",
    "AUDIT_WINDOW_ATTR",
    "audit_lineage_task_id",
    "audit_worker_slice_seconds",
    "audit_worker_tool_scope",
    "attributes_request_audit",
    "audit_source_worker_key",
    "audit_watch_scope_id",
    "audit_source_work_scope_key",
    "current_audit_attributes",
    "structured_audit_source_binding_attributes",
    "structured_audit_supervised_worker_attributes",
    "structured_audit_source_worker_attributes",
]
