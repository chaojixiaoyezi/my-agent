"""Durable one-watch/one-worker binding for named Audit tasks.

This module is deliberately a thin adapter over the existing subagent kernel.
It owns no model loop, memory, compaction, scheduler or business classifier.
It only materializes and authorizes the structured relationship:
``audit_id + watch_id -> worker_key -> run_id + attempt_id lease``.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from ..common.audit_activation import (
    AUDIT_ATTR,
    AUDIT_OBJECTIVE_ATTR,
    AUDIT_RUN_EPOCH_ATTR,
    AUDIT_RUN_PROMPT_ATTR,
    AUDIT_SOURCE_BINDING_PENDING_ATTR,
    AUDIT_SOURCE_BINDINGS_ATTR,
    AUDIT_SOURCE_CONFIG_VERSION_ATTR,
    AUDIT_SOURCE_CONTEXT_REFRESH_ATTEMPT_ATTR,
    AUDIT_SOURCE_DOCUMENT_REFS_ATTR,
    AUDIT_SOURCE_ID_ATTR,
    AUDIT_SOURCE_OPEN_ATTR,
    AUDIT_SOURCE_OWNER_HOME_ATTR,
    AUDIT_SOURCE_PROFILE_REF_ATTR,
    AUDIT_SOURCE_WATCH_ID_ATTR,
    AUDIT_SOURCE_WORKER_ATTR,
    AUDIT_SOURCE_WORKER_KEY_ATTR,
    AUDIT_SOURCE_WORKER_TOOLS,
    audit_source_work_scope_key,
    audit_source_worker_key,
    audit_worker_slice_seconds,
    audit_worker_tool_scope,
    current_audit_attributes,
    structured_audit_source_binding_attributes,
    structured_audit_source_worker_attributes,
    structured_audit_supervised_worker_attributes,
)
from ..common.json_io import (
    locked_json_path,
    read_json_object_report,
    write_json_file_atomic_unlocked,
)
from ..concurrency.retry import jittered_backoff
from ..conversation.audit_requirements import audit_runtime_requirement_text
from ..conversation.authority import CONVERSATION_REQUEST_ID_ATTR
from ..conversation.workspace_paths import audit_workspace_path
from ..settings.runtime_guard_config import runtime_guard_int
from ..subagents.model_capabilities import capability_request_counts_as_open
from ..subagents.models import (
    PROVIDER_SUPPLY_FAILURE_TYPES,
    FailureType,
    TaskStatus,
    VerificationStatus,
    task_status_in,
)
from .watch_state import (
    WatchState,
    list_states,
    load_state,
    persist_source_binding_revision,
    persist_state,
    refresh_scalars_from_disk,
    registry,
    state_dir,
)

_LEASE_SCHEMA = "audit-source-worker-lease.v1"
_RECOVERY_SCHEMA = "audit-source-worker-recovery.v1"
_FINDING_OUTBOX_SCHEMA = "audit-finding-outbox-cursor.v1"
_INLINE_FINDING_PROJECTION_SCHEMA = "audit-inline-finding-projection.v1"
_CAPACITY_ALERT_CURSOR_SCHEMA = "audit-capacity-alert-cursor.v2"
_ACTIVE_WORKER_STATUSES = frozenset(
    {
        TaskStatus.PLANNING.value,
        TaskStatus.PENDING.value,
        TaskStatus.RUNNING.value,
        TaskStatus.BLOCKED.value,
        TaskStatus.PAUSED.value,
    }
)


# LLM: Published Audit sources are a host-owned cardinality fact.  The model
# still owns every semantic judgment and may create any additional investigation
# workers, but it must not be responsible for remembering to materialize the
# mandatory one-source/one-worker baseline.
# 函数用途: Audit 启动轮沿既有 create_subagents 主链幂等补齐每个已发布来源的直属叶子，
# 不新增调度器、Agent loop 或业务模板。
def provision_published_audit_source_workers(
    agent: object,
    *,
    prompt: str = "",
    task_id: str = "",
    task_attributes: dict[str, object] | None = None,
    source: str = "audit_source_provision",
) -> dict[str, object]:
    """Provision published sources, optionally inside an explicit root task scope.

    The source-worker boundary owns the temporary runtime scope needed by its
    create-subagent call.  Gateway callers therefore pass structured facts and
    do not reach back into Agent Core internals.
    """

    if task_attributes is not None:
        from ..agent_core.runtime.loop_models import RunParams
        from ..agent_core.runtime_mixin import current_prompt_scope

        scoped_task_id = str(task_id or "").strip()
        with current_prompt_scope(
            agent,
            str(prompt or ""),
            RunParams(
                request_id=scoped_task_id,
                run_id=scoped_task_id,
                task_id=scoped_task_id,
                task_attributes=dict(task_attributes),
                source=str(source or "audit_source_provision"),
                context_scope="control_plane",
                root_user_prompt=str(prompt or ""),
            ),
        ):
            return _provision_published_audit_source_workers(agent)
    return _provision_published_audit_source_workers(agent)


def _provision_published_audit_source_workers(agent: object) -> dict[str, object]:
    attrs = current_audit_attributes(agent)
    if not isinstance(attrs, dict) or attrs.get(AUDIT_ATTR) is not True:
        return {}
    if attrs.get(
        AUDIT_SOURCE_BINDING_PENDING_ATTR
    ) is True or structured_audit_supervised_worker_attributes(attrs):
        return {}

    # A descendant may inherit the Audit lineage for investigation or
    # coordination.  Only the ordinary root turn owns mandatory source
    # cardinality; descendants must never recursively provision siblings.
    from ..agent_core.runner.context import current_subagent_run_id

    if current_subagent_run_id(agent):
        return {}

    raw_bindings = attrs.get(AUDIT_SOURCE_BINDINGS_ATTR)
    if not isinstance(raw_bindings, (list, tuple)) or not raw_bindings:
        return {
            "schema": "audit-source-provision.v1",
            "ok": False,
            "required": 0,
            "ready": 0,
            "waiting": 0,
            "error_code": "AUDIT_NO_PUBLISHED_SOURCES",
            "sources": [],
        }
    from .source_binding import normalize_audit_source_bindings

    try:
        bindings = normalize_audit_source_bindings(list(raw_bindings))
    except ValueError as exc:
        return {
            "schema": "audit-source-provision.v1",
            "ok": False,
            "required": len(raw_bindings),
            "ready": 0,
            "waiting": len(raw_bindings),
            "error_code": "AUDIT_SOURCE_BINDINGS_INVALID",
            "error": str(exc),
            "sources": [],
        }

    sources = [_provision_published_audit_source(agent, binding) for binding in bindings]

    ready = sum(1 for item in sources if item.get("ok") is True)
    return {
        "schema": "audit-source-provision.v1",
        "ok": ready == len(sources),
        "required": len(sources),
        "ready": ready,
        "waiting": len(sources) - ready,
        "sources": sources,
    }


def _provision_published_audit_source(
    agent: object,
    binding: dict[str, object],
) -> dict[str, object]:
    from ..agent_core.orchestration_tools import CreateSubagentsTool

    source_id = str(binding.get("source_id") or "").strip()
    refs = [
        str(ref).strip()
        for ref in [
            binding.get("source_profile_ref"),
            *(binding.get("document_refs") or []),
        ]
        if str(ref or "").strip()
    ]
    result = CreateSubagentsTool(agent).execute(
        {
            "goal": (
                "持续处理当前命名 Audit 已发布的这一条来源。"
                f"结构化 source_id={source_id} 已由宿主绑定；"
                "先通过授权入口打开或续接该来源，再依据 Audit 生效要求和该来源资料"
                "持续研判完整记录。不要消费兄弟来源，也不要直接面向用户汇报。"
            ),
            "agent_name": f"audit-source-{source_id}",
            "role": "worker",
            "audit_source_id": source_id,
            "context_manifest": {"required_read_paths": refs},
            "long_running": True,
        }
    )
    payload = _tool_payload(result)
    created_ids = _payload_run_ids(payload, "created_run_ids")
    reused_ids = _payload_run_ids(payload, "reused_run_ids")
    auto_start = payload.get("auto_start")
    start_status = str(auto_start.get("status") or "") if isinstance(auto_start, dict) else ""
    error_code = str(getattr(result, "error_code", "") or "")
    return {
        "source_id": source_id,
        "state": _provision_state(created_ids, reused_ids, error_code),
        "ok": bool(getattr(result, "ok", False)) and bool(created_ids or reused_ids),
        "run_id": (created_ids or reused_ids or [""])[0],
        "dispatch_status": start_status,
        "error_code": error_code,
    }


def _payload_run_ids(payload: dict[str, object], key: str) -> list[str]:
    raw = payload.get(key)
    if not isinstance(raw, list):
        return []
    return [str(item or "").strip() for item in raw if str(item or "").strip()]


def _provision_state(created: list[str], reused: list[str], error_code: str) -> str:
    if created:
        return "created"
    if reused:
        return "reused"
    if error_code in {"SUBAGENT_CAPACITY_EXCEEDED", "SUBAGENT_QUOTA_EXCEEDED"}:
        return "waiting_for_capacity"
    return "degraded"


# LLM: The lease is a machine-authoritative capability token tied to one
# persisted run attempt, not a role/name/prompt convention.
# 函数用途: 校验当前工具调用者是本 watch 的唯一来源工作者，并续租其消费权限。
def authorize_source_worker_action(
    agent: object,
    state: WatchState,
    *,
    now: float | None = None,
    require_current_config: bool = False,
) -> dict[str, object]:
    observed_at = time.time() if now is None else float(now)
    attrs = current_audit_attributes(agent)
    if not structured_audit_source_worker_attributes(attrs):
        return _denied(
            "AUDIT_SOURCE_WORKER_REQUIRED",
            "这条 Audit 数据源只能由结构化绑定的来源子代理消费。",
        )
    assert isinstance(attrs, dict)
    audit_id = str(attrs.get(CONVERSATION_REQUEST_ID_ATTR) or "").strip()
    source_id = str(attrs.get(AUDIT_SOURCE_ID_ATTR) or "").strip()
    watch_id = str(attrs.get(AUDIT_SOURCE_WATCH_ID_ATTR) or "").strip()
    worker_key = str(attrs.get(AUDIT_SOURCE_WORKER_KEY_ATTR) or "").strip()
    owner_home = str(attrs.get(AUDIT_SOURCE_OWNER_HOME_ATTR) or "").strip()
    runtime_config_version = str(
        attrs.get(AUDIT_SOURCE_CONFIG_VERSION_ATTR) or ""
    ).strip()
    run_epoch = _normalized_run_epoch(attrs.get(AUDIT_RUN_EPOCH_ATTR))
    if (
        state.closed
        or audit_id != str(state.audit_root_task_id or "").strip()
        or source_id != state.source_id
        or watch_id != state.watch_id
        or owner_home != str(state.owner_home)
        or worker_key != audit_source_worker_key(audit_id, watch_id)
        or run_epoch != _normalized_run_epoch(state.audit_run_epoch)
        or not _source_state_matches_current_audit_epoch(agent, state)
    ):
        return _denied(
            "AUDIT_SOURCE_BINDING_MISMATCH",
            "当前来源子代理与这条 watch 的结构化绑定不一致。",
        )
    if require_current_config and runtime_config_version != state.source_config_version:
        return _denied(
            "AUDIT_SOURCE_CONTEXT_REFRESH_REQUIRED",
            "来源配置已更新；当前工作片可签收已经领取的完整批次，但不能再领取新批次。",
        )
    from ..agent_core.runner.context import (
        current_subagent_attempt_id,
        current_subagent_run_id,
    )

    run_id = current_subagent_run_id(agent)
    attempt_id = current_subagent_attempt_id(agent)
    if not run_id or not attempt_id:
        return _denied(
            "AUDIT_SOURCE_RUN_IDENTITY_REQUIRED",
            "来源消费缺少当前子代理 run_id 或 attempt_id。",
        )
    task_error = _validate_current_worker_task(
        agent,
        run_id=run_id,
        attempt_id=attempt_id,
        attrs=attrs,
    )
    if task_error is not None:
        return task_error
    authority = _acquire_or_renew_lease(
        agent,
        state,
        attrs=attrs,
        worker_key=worker_key,
        run_id=run_id,
        attempt_id=attempt_id,
        now=observed_at,
    )
    if bool(authority.get("ok")):
        authority["source_config_version"] = runtime_config_version
    return authority


# LLM: Tool entry authorization is not enough when an old process can be
# suspended after the check. Keep the lease lock held through the caller's
# cursor mutation so a replacement attempt and the stale writer cannot commit
# concurrently.
# 函数用途: 在游标/结论真正落盘期间再次核对 attempt 与租约世代，并持锁阻止接替竞态。
@contextmanager
def source_worker_lease_fence(
    state: WatchState,
    authority: dict[str, object] | None,
    *,
    now: float | None = None,
    require_current_config: bool = False,
) -> Iterator[dict[str, object]]:
    path = _lease_path(state)
    stack = ExitStack()
    try:
        stack.enter_context(locked_json_path(path))
    except OSError:
        yield _denied(
            "AUDIT_SOURCE_LEASE_UNAVAILABLE",
            "来源工作者租约无法锁定，已按 fail-closed 拒绝写账。",
        )
        return
    try:
        stack.enter_context(state.lock)
        report = read_json_object_report(
            path,
            context="audit_source_worker.lease_fence",
        )
        if report.load_error is not None:
            result = _denied(
                "AUDIT_SOURCE_LEASE_UNAVAILABLE",
                "来源工作者租约损坏，已按 fail-closed 拒绝写账。",
            )
        else:
            result = _validate_fenced_authority(
                state,
                report.payload,
                authority,
                now=time.time() if now is None else float(now),
            )
            if (
                bool(result.get("ok"))
                and require_current_config
                and str((authority or {}).get("source_config_version") or "")
                != state.source_config_version
            ):
                result = _denied(
                    "AUDIT_SOURCE_CONTEXT_REFRESH_REQUIRED",
                    "来源配置已更新；当前工作片不能再领取新批次。",
                )
        yield result
    finally:
        stack.close()


def _validate_fenced_authority(
    state: WatchState,
    lease: dict[str, Any],
    authority: dict[str, object] | None,
    *,
    now: float,
) -> dict[str, object]:
    if not isinstance(authority, dict):
        return _denied(
            "AUDIT_SOURCE_AUTHORITY_REQUIRED",
            "来源写账缺少本次工具调用的结构化租约凭证。",
        )
    expected = {
        "audit_id": str(authority.get("audit_id") or ""),
        "audit_run_epoch": _normalized_run_epoch(
            authority.get("audit_run_epoch")
        ),
        "source_id": str(authority.get("source_id") or ""),
        "watch_id": str(authority.get("watch_id") or ""),
        "worker_key": str(authority.get("worker_key") or ""),
        "run_id": str(authority.get("run_id") or ""),
        "attempt_id": str(authority.get("attempt_id") or ""),
        "epoch": int(authority.get("lease_epoch") or 0),
    }
    observed = {
        "audit_id": str(lease.get("audit_id") or ""),
        "audit_run_epoch": _normalized_run_epoch(
            lease.get("audit_run_epoch")
        ),
        "source_id": str(lease.get("source_id") or ""),
        "watch_id": str(lease.get("watch_id") or ""),
        "worker_key": str(lease.get("worker_key") or ""),
        "run_id": str(lease.get("run_id") or ""),
        "attempt_id": str(lease.get("attempt_id") or ""),
        "epoch": int(lease.get("epoch") or 0),
    }
    if (
        expected["audit_id"] != str(state.audit_root_task_id or "")
        or expected["audit_run_epoch"]
        != _normalized_run_epoch(state.audit_run_epoch)
        or expected["source_id"] != state.source_id
        or expected["watch_id"] != state.watch_id
        or not all(
            value
            for key, value in expected.items()
            if key != "audit_run_epoch"
        )
        or expected != observed
    ):
        return _denied(
            "AUDIT_SOURCE_ATTEMPT_STALE",
            "来源工作者 attempt 或租约世代已失效，本次迟到写入已拒绝。",
        )
    if _float(lease.get("expires_at")) <= now:
        return _denied(
            "AUDIT_SOURCE_LEASE_EXPIRED",
            "来源工作者租约已过期，本次写入已拒绝并等待接替者重投。",
        )
    return {
        "ok": True,
        "error_code": "",
        **expected,
        "lease_epoch": expected["epoch"],
    }


# LLM: Status reads the same lease and task records used by the execution gate;
# it never reports a worker merely because a model claimed to have created one.
# 函数用途: 返回某 watch 当前绑定、租约和任务状态，供 open/status/list 与验收读取。
def source_worker_facts(agent: object | None, state: WatchState) -> dict[str, object]:
    worker_key = audit_source_worker_key(state.audit_root_task_id, state.watch_id)
    task = _matching_worker_task(
        agent,
        worker_key,
        run_epoch=state.audit_run_epoch,
    )
    lease = _read_lease(_lease_path(state))
    now = time.time()
    lease_fresh = bool(
        lease
        and str(lease.get("worker_key") or "") == worker_key
        and _normalized_run_epoch(lease.get("audit_run_epoch"))
        == _normalized_run_epoch(state.audit_run_epoch)
        and _float(lease.get("expires_at")) > now
    )
    recovery = _source_worker_recovery(task)
    worker_state = _worker_state(task, lease_fresh)
    failure_type = str(getattr(task, "failure_type", "") or "") if task is not None else ""
    if failure_type == FailureType.PROVIDER_QUOTA_EXHAUSTED.value:
        worker_state = "awaiting_operator"
    if worker_state == "waiting_for_worker" and str(recovery.get("last_reason") or "") in {
        "runner_process_died",
        "runner_session_stalled",
        "runner_session_ended",
        "runner_timeout",
        "provider_timeout",
        "provider_transient",
    }:
        worker_state = "recovering"
    return {
        "source_id": state.source_id,
        "worker_key": worker_key,
        "run_id": str(getattr(task, "id", "") or "") if task is not None else "",
        "task_status": str(getattr(task, "status", "") or "") if task is not None else "",
        "failure_type": failure_type,
        "attempt_id": str(lease.get("attempt_id") or "") if lease_fresh else "",
        "lease_epoch": int(lease.get("epoch") or 0) if lease_fresh else 0,
        "lease_fresh": lease_fresh,
        "state": worker_state,
        "recovery": recovery,
    }


# LLM: Worker creation reuses the canonical create_subagents path and stable
# work-scope idempotency; this adapter never starts a second execution runtime.
# 函数用途: 为一条未关闭的 Audit watch 确保存在唯一来源子代理，容量不足时如实降级等待。
def ensure_audit_source_worker(agent: object, state: WatchState) -> dict[str, object]:
    audit_id, worker_key, terminal = _source_worker_preflight(agent, state)
    if terminal is not None:
        return terminal
    existing, duplicate_workers_retired = _canonical_worker_task(
        agent,
        worker_key,
        retire_duplicates=True,
        run_epoch=state.audit_run_epoch,
    )
    if existing is None:
        adopted = _adopt_current_audit_subagent(agent, state, worker_key)
        if adopted is not None:
            return adopted
    if existing is not None:
        existing_result = _existing_source_worker_result(
            agent,
            state,
            existing,
            worker_key,
            duplicate_workers_retired,
        )
        if existing_result is not None:
            return existing_result
    return _create_audit_source_worker(
        agent,
        state,
        worker_key,
        duplicate_workers_retired,
    )


def _source_worker_preflight(
    agent: object,
    state: WatchState,
) -> tuple[str, str, dict[str, object] | None]:
    audit_id = str(state.audit_root_task_id or "").strip()
    worker_key = audit_source_worker_key(audit_id, state.watch_id)
    if not state.audit_guarantee or state.closed:
        return audit_id, worker_key, {"ok": False, "state": "not_required", "error_code": ""}
    if not _source_state_matches_current_audit_epoch(agent, state):
        return (
            audit_id,
            worker_key,
            {
                "ok": False,
                "state": "not_required",
                "error_code": "AUDIT_RUN_EPOCH_MISMATCH",
            },
        )
    if not _source_watch_incomplete(state):
        return (
            audit_id,
            worker_key,
            _worker_result(
                True,
                "complete",
                worker_key,
                task_status=TaskStatus.DONE.value,
            ),
        )
    if not worker_key:
        return (
            audit_id,
            worker_key,
            {
                "ok": False,
                "state": "degraded",
                "error_code": "AUDIT_SOURCE_IDENTITY_UNAVAILABLE",
            },
        )
    if not str(state.source_task_goal or "").strip():
        return (
            audit_id,
            worker_key,
            {
                "ok": False,
                "state": "degraded",
                "error_code": "AUDIT_SOURCE_TASK_CONTEXT_UNAVAILABLE",
                "worker_key": worker_key,
                "created": False,
            },
        )
    parent_active, parent_state = audit_parent_reconcile_state(agent, audit_id)
    if parent_active:
        return audit_id, worker_key, None
    return (
        audit_id,
        worker_key,
        {
            "ok": parent_state == "inactive",
            "state": "not_required" if parent_state == "inactive" else "degraded",
            "error_code": "" if parent_state == "inactive" else "AUDIT_PARENT_STATE_UNAVAILABLE",
            "worker_key": worker_key,
            "created": False,
            "parent_state": parent_state,
        },
    )


def _existing_source_worker_result(
    agent: object,
    state: WatchState,
    existing: object,
    worker_key: str,
    retired: int,
) -> dict[str, object] | None:
    _sync_source_worker_runtime_context(agent, state, existing)
    workspace_state = _reconcile_source_worker_workspace(agent, state, existing)
    if workspace_state == "waiting_for_idle":
        return _worker_result(
            False,
            "degraded",
            worker_key,
            existing=existing,
            retired=retired,
            error_code="AUDIT_SOURCE_WORKER_WORKSPACE_MIGRATION_PENDING",
        )
    status = str(getattr(existing, "status", "") or "").upper()
    if status == TaskStatus.BLOCKED.value:
        blocked = _blocked_source_worker_result(agent, state, existing, worker_key, retired)
        if blocked is not None:
            return blocked
    if task_status_in(status, _ACTIVE_WORKER_STATUSES):
        return _active_source_worker_result(existing, worker_key, retired)
    if status != TaskStatus.DONE.value:
        return None
    if source_worker_task_incomplete(existing):
        _requeue_completed_worker(agent, existing)
        return _worker_result(
            True,
            "waiting_for_worker",
            worker_key,
            existing=existing,
            retired=retired,
            task_status=TaskStatus.PENDING.value,
        )
    return _worker_result(
        True,
        "complete",
        worker_key,
        existing=existing,
        retired=retired,
        task_status=TaskStatus.DONE.value,
    )


def _blocked_source_worker_result(
    agent: object,
    state: WatchState,
    existing: object,
    worker_key: str,
    retired: int,
) -> dict[str, object] | None:
    recovery = _blocked_source_worker_recovery(agent, existing, state)
    action = str(recovery.get("action") or "")
    if action == "requeue":
        pending_failure_type = (
            str(recovery["pending_failure_type"])
            if "pending_failure_type" in recovery
            else FailureType.INCOMPLETE_DELIVERABLES.value
        )
        _requeue_blocked_source_worker(
            agent,
            existing,
            pending_failure_type=pending_failure_type,
        )
        return {
            **_worker_result(
                True,
                "waiting_for_worker",
                worker_key,
                existing=existing,
                retired=retired,
                task_status=TaskStatus.PENDING.value,
            ),
            "recovery_reason": str(recovery.get("reason") or ""),
        }
    if action == "wait":
        return {
            **_worker_result(
                True,
                "waiting_for_retry",
                worker_key,
                existing=existing,
                retired=retired,
                task_status=TaskStatus.BLOCKED.value,
            ),
            "recovery_reason": str(recovery.get("reason") or ""),
            "retry_not_before": float(recovery.get("retry_not_before") or 0.0),
            "retry_delay_seconds": float(recovery.get("retry_delay_seconds") or 0.0),
        }
    if (
        str(getattr(existing, "failure_type", "") or "")
        == FailureType.PROVIDER_QUOTA_EXHAUSTED.value
    ):
        return _worker_result(
            False,
            "awaiting_operator",
            worker_key,
            existing=existing,
            retired=retired,
            task_status=TaskStatus.BLOCKED.value,
            error_code="PROVIDER_QUOTA_EXHAUSTED",
        )
    return None


def _active_source_worker_result(
    existing: object, worker_key: str, retired: int
) -> dict[str, object]:
    status = str(getattr(existing, "status", "") or "").upper()
    state = (
        "active"
        if status == TaskStatus.RUNNING.value
        else (
            "waiting_for_worker"
            if status in {TaskStatus.PLANNING.value, TaskStatus.PENDING.value}
            else "degraded"
        )
    )
    return _worker_result(
        status not in {TaskStatus.BLOCKED.value, TaskStatus.PAUSED.value},
        state,
        worker_key,
        existing=existing,
        retired=retired,
        task_status=status,
    )


def _worker_result(
    ok: bool,
    state: str,
    worker_key: str,
    *,
    existing: object | None = None,
    retired: int = 0,
    task_status: str = "",
    error_code: str = "",
) -> dict[str, object]:
    return {
        "ok": ok,
        "state": state,
        "run_id": str(getattr(existing, "id", "") or ""),
        "task_status": task_status or str(getattr(existing, "status", "") or "").upper(),
        "worker_key": worker_key,
        "created": False,
        "duplicate_workers_retired": retired,
        **({"error_code": error_code} if error_code else {}),
    }


def _create_audit_source_worker(
    agent: object,
    state: WatchState,
    worker_key: str,
    duplicate_workers_retired: int,
) -> dict[str, object]:
    params = _source_worker_create_params(agent, state, worker_key)
    try:
        from ..agent_core.orchestration_tools import CreateSubagentsTool

        result = CreateSubagentsTool(agent).execute(params)
    except Exception as exc:
        return {
            "ok": False,
            "state": "degraded",
            "error_code": "AUDIT_SOURCE_WORKER_CREATE_FAILED",
            "error": f"{type(exc).__name__}: {exc}",
            "worker_key": worker_key,
        }
    payload = _tool_payload(result)
    run_ids = [
        str(item or "").strip()
        for key in ("created_run_ids", "reused_run_ids")
        for item in (payload.get(key) if isinstance(payload.get(key), list) else [])
        if str(item or "").strip()
    ]
    run_id = run_ids[0] if run_ids else ""
    if run_ids and _cancel_if_watch_closed_during_create(agent, state, run_ids):
        return {
            "ok": False,
            "state": "cancelled",
            "run_id": run_id,
            "worker_key": worker_key,
            "created": bool(payload.get("created")),
            "duplicate_workers_retired": duplicate_workers_retired,
            "error_code": "AUDIT_SOURCE_WATCH_CLOSED",
        }
    auto_start = payload.get("auto_start")
    start_status = str(auto_start.get("status") or "") if isinstance(auto_start, dict) else ""
    if bool(getattr(result, "ok", False)) and run_id:
        return {
            "ok": True,
            "state": "active" if start_status == "started" else "waiting_for_worker",
            "run_id": run_id,
            "task_status": "",
            "worker_key": worker_key,
            "created": bool(payload.get("created")),
            "duplicate_workers_retired": duplicate_workers_retired,
            "dispatch_status": start_status,
        }
    return {
        "ok": False,
        "state": (
            "waiting_for_capacity"
            if str(getattr(result, "error_code", "") or "")
            in {"SUBAGENT_CAPACITY_EXCEEDED", "SUBAGENT_QUOTA_EXCEEDED"}
            else "degraded"
        ),
        "error_code": str(getattr(result, "error_code", "") or "AUDIT_SOURCE_WORKER_CREATE_FAILED"),
        "error": str(payload.get("error") or getattr(result, "output", "") or ""),
        "worker_key": worker_key,
        "duplicate_workers_retired": duplicate_workers_retired,
    }


# LLM: A harvester's durable empty->nonempty transition may wake only the
# already-authorized source position through the canonical orphan dispatcher.
# It does not run a model itself and it never chooses work from event content.
# 函数用途: 新记录落耐久队列后立即唤醒对应来源岗位，避免靠 60 秒兜底扫描或空轮询烧模型。
def wake_audit_source_worker(agent: object, state: WatchState) -> bool:
    result = ensure_audit_source_worker(agent, state)
    state_name = str(result.get("state") or "")
    if state_name in {"complete", "not_required", "cancelled"}:
        return True
    run_id = str(result.get("run_id") or "").strip()
    if not bool(result.get("ok")) or not run_id:
        return False
    from ..agent_core.orchestration.dispatch.capability_auto_sweep import (
        auto_start_orphan_run,
    )

    wake = auto_start_orphan_run(agent, run_id)
    return str(wake.get("status") or "") in {"started", "not_needed"}


# LLM: A committed collection boundary is a typed lifecycle event. It may
# project only the existing canonical source worker through the same terminal
# gate used by periodic supervision; it never infers completion from prose.
# 函数用途: 最终补拉事务落盘后立即收口已清账来源岗位，60 秒巡检只保留为恢复兜底。
def settle_audit_source_worker(agent: object, state: WatchState) -> bool:
    worker_key = audit_source_worker_key(state.audit_root_task_id, state.watch_id)
    task = _matching_worker_task(
        agent,
        worker_key,
        run_epoch=state.audit_run_epoch,
    )
    if task is None:
        return True
    from ..agent_core.orchestration.dispatch.capability_auto_sweep import (
        complete_settled_source_worker,
    )

    outcome = complete_settled_source_worker(agent, task)
    if outcome == "completed":
        # The finding ledger remains the durable outbox even after the source
        # worker itself settles. Reconcile it before asking the canonical root
        # terminal gate to close the named Audit.
        reconcile_audit_finding_outbox(agent, str(getattr(task, "id", "") or ""))
        from ..conversation.task_promotion import complete_named_audit_task_if_settled

        complete_named_audit_task_if_settled(agent, state.audit_root_task_id)
    return outcome in {"completed", "pending"}


def _sync_source_worker_runtime_context(
    agent: object,
    state: WatchState,
    task: object,
) -> bool:
    """Project the active named Audit revision into watch and next worker slice."""

    from ..conversation.audit_requirements import audit_runtime_requirement_text
    from .source_binding import (
        audit_source_binding_by_id,
        audit_source_config_version,
        audit_source_runtime_projection,
    )

    store = getattr(agent, "conversation_store", None)
    link = None
    loader = getattr(store, "load_task_link", None)
    if callable(loader):
        try:
            link = loader(state.audit_root_task_id)
        except (OSError, TypeError, ValueError):
            link = None
    if (
        link is None
        or str(getattr(link, "task_id", "") or "") != state.audit_root_task_id
        or str(getattr(link, "work_kind", "") or "").strip().lower() != "audit"
        or str(getattr(link, "status", "") or "").strip().lower() != "active"
    ):
        return False
    objective = audit_runtime_requirement_text(getattr(link, "goal", ""))
    run_prompt = str(getattr(link, "run_prompt", "") or "").strip()
    binding = audit_source_binding_by_id(
        getattr(link, "effective_source_bindings", ()) or (),
        state.source_id,
    )
    state_changed = False
    if binding is not None:
        try:
            projection = audit_source_runtime_projection(binding)
        except ValueError:
            projection = None
        if projection is not None:
            state_changed = _apply_source_runtime_projection(
                state,
                projection,
                objective=objective,
                run_prompt=run_prompt,
                config_version_factory=audit_source_config_version,
            )
    else:
        state_changed = _sync_audit_runtime_text(
            state,
            objective=objective,
            run_prompt=run_prompt,
        )

    attrs = getattr(task, "attributes", None)
    if not isinstance(attrs, dict):
        return state_changed
    updates: dict[str, object] = {}
    if objective and str(attrs.get(AUDIT_OBJECTIVE_ATTR) or "").strip() != objective:
        updates[AUDIT_OBJECTIVE_ATTR] = objective
    if run_prompt and str(attrs.get(AUDIT_RUN_PROMPT_ATTR) or "").strip() != run_prompt:
        updates[AUDIT_RUN_PROMPT_ATTR] = run_prompt
    for key, value in (
        (AUDIT_SOURCE_PROFILE_REF_ATTR, state.source_profile_ref),
        (AUDIT_SOURCE_DOCUMENT_REFS_ATTR, list(state.document_refs)),
        (AUDIT_SOURCE_CONFIG_VERSION_ATTR, state.source_config_version),
    ):
        if attrs.get(key) != value:
            updates[key] = value
    required_reads = _source_worker_required_read_paths(state)
    context_manifest = getattr(task, "context_manifest", None)
    manifest_changed = False
    if context_manifest is not None:
        if list(getattr(context_manifest, "required_read_paths", []) or []) != required_reads:
            context_manifest.required_read_paths = required_reads
            manifest_changed = True
        if list(getattr(context_manifest, "hint_read_paths", []) or []):
            context_manifest.hint_read_paths = []
            manifest_changed = True
    projected_goal = _source_worker_goal(state)
    goal_changed = bool(projected_goal and str(getattr(task, "goal", "") or "") != projected_goal)
    active_attempt_id = str(
        getattr(task, "runner_active_attempt_id", "") or ""
    ).strip()
    if (
        active_attempt_id
        and str(getattr(task, "status", "") or "").upper() == TaskStatus.RUNNING.value
        and (updates or manifest_changed or goal_changed)
        and attrs.get(AUDIT_SOURCE_CONTEXT_REFRESH_ATTEMPT_ATTR) != active_attempt_id
    ):
        # The running model turn keeps its immutable prompt/tool snapshot.  Mark
        # that exact attempt so the existing runner-result transition discards
        # its stale closeout and requeues the same logical worker with the
        # newly persisted task context.  A claimed batch may still be signed;
        # config-version fencing prevents this attempt claiming another one.
        updates[AUDIT_SOURCE_CONTEXT_REFRESH_ATTEMPT_ATTR] = active_attempt_id
    if not updates and not manifest_changed and not goal_changed:
        return state_changed
    if updates:
        task.attributes = {**attrs, **updates}
    if goal_changed:
        task.goal = projected_goal
    task.updated_at = time.time()
    manager = getattr(agent, "subagents", None)
    saver = getattr(manager, "save", None)
    if not callable(saver):
        return state_changed
    saver(task)
    return True


def _apply_source_runtime_projection(
    state: WatchState,
    projection: dict[str, Any],
    *,
    objective: str,
    run_prompt: str,
    config_version_factory,
) -> bool:
    """Atomically adopt same-source transport facts without touching progress.

    The harvester owns this same lock for its complete fetch-to-spool commit,
    so an adapter revision can take effect only between complete source
    transactions. Cursor, opaque checkpoint, spool and run epoch are never
    replaced here.
    """

    source_id = str(projection.get("source_id") or "").strip()
    source_url = str(projection.get("source_url") or "").strip()
    source_mode = str(projection.get("source_mode") or "")
    source_envelope = projection.get("source_envelope")
    if (
        source_id != state.source_id
        or source_url != state.source_url
        or not isinstance(source_envelope, dict)
    ):
        return _record_source_rebind_required(state)

    with state.lock:
        refresh_scalars_from_disk(state)
        if state.closed:
            return False
        if source_id != state.source_id or source_url != state.source_url:
            return _record_source_rebind_required_locked(state)
        if source_mode != str(state.source_mode or ""):
            return _record_source_rebind_required_locked(state)
        if (
            source_envelope.get("mode") == "file"
            and source_envelope.get("record_boundary")
            != state.source_envelope.get("record_boundary")
            and (state.cursor > 0 or state.file_fragment_bytes > 0)
        ):
            return _record_source_rebind_required_locked(state)

        poll_seconds = projection.get("poll_query_seconds")
        desired_poll_seconds = (
            max(1, int(poll_seconds))
            if poll_seconds is not None
            else int(state.tuning.poll_query_seconds or 0)
        )
        desired_version = config_version_factory(
            source_url=source_url,
            source_mode=source_mode,
            source_envelope=source_envelope,
            poll_query_seconds=desired_poll_seconds,
        )
        changed = any(
            (
                state.source_envelope != source_envelope,
                state.source_profile_ref
                != str(projection.get("source_profile_ref") or ""),
                list(state.document_refs)
                != list(projection.get("document_refs") or []),
                state.source_config_version != desired_version,
                objective and state.audit_objective != objective,
                run_prompt and state.audit_run_prompt != run_prompt,
                poll_seconds is not None
                and int(state.tuning.poll_query_seconds or 0) != desired_poll_seconds,
                state.last_error_code
                in {
                    "SOURCE_ADAPTER_INVALID",
                    "SOURCE_ENVELOPE_INVALID",
                    "SOURCE_REQUEST_INVALID",
                    "AUDIT_SOURCE_REBIND_REQUIRED",
                },
            )
        )
        if not changed:
            return False
        state.source_envelope = dict(source_envelope)
        state.source_profile_ref = str(projection.get("source_profile_ref") or "")
        state.document_refs = list(projection.get("document_refs") or [])
        state.source_config_version = desired_version
        if poll_seconds is not None:
            state.tuning = replace(
                state.tuning,
                poll_query_seconds=desired_poll_seconds,
            )
        if objective:
            state.audit_objective = objective
        if run_prompt:
            state.audit_run_prompt = run_prompt
        if state.last_error_code in {
            "SOURCE_ADAPTER_INVALID",
            "SOURCE_ENVELOPE_INVALID",
            "SOURCE_REQUEST_INVALID",
            "AUDIT_SOURCE_REBIND_REQUIRED",
        }:
            state.last_error = ""
            state.last_error_code = ""
        persist_source_binding_revision(state)
        return True


def _sync_audit_runtime_text(
    state: WatchState,
    *,
    objective: str,
    run_prompt: str,
) -> bool:
    """Update only the current named-Audit text when no source row changed."""

    with state.lock:
        refresh_scalars_from_disk(state)
        if state.closed:
            return False
        changed = bool(
            (objective and state.audit_objective != objective)
            or (run_prompt and state.audit_run_prompt != run_prompt)
        )
        if not changed:
            return False
        if objective:
            state.audit_objective = objective
        if run_prompt:
            state.audit_run_prompt = run_prompt
        persist_state(state)
        return True


def _record_source_rebind_required(state: WatchState) -> bool:
    with state.lock:
        refresh_scalars_from_disk(state)
        return _record_source_rebind_required_locked(state)


def _record_source_rebind_required_locked(state: WatchState) -> bool:
    message = "生效来源改变了来源身份、地址、推进模式或已有文件记录边界，需要显式重新绑定"
    if state.last_error_code == "AUDIT_SOURCE_REBIND_REQUIRED" and state.last_error == message:
        return False
    state.last_error_code = "AUDIT_SOURCE_REBIND_REQUIRED"
    state.last_error = message
    persist_state(state)
    return True


# LLM: Named clear may race a worker creation in another thread or process.
# Re-read the persisted watch after creation and retire every just-created run
# through the canonical subagent cancellation lifecycle if the watch closed.
# 函数用途: 堵住“关 Audit 的同时恰好创建子代理”的竞态，避免 clear 后漏出一个孤儿来源工作者。
def _cancel_if_watch_closed_during_create(
    agent: object,
    state: WatchState,
    run_ids: list[str],
) -> bool:
    with state.lock:
        refresh_scalars_from_disk(state)
        closed = bool(state.closed)
    if not closed:
        return False
    from ..agent_core.orchestration.tools.cancel import (
        CancelSubagentTaskRequest,
        cancel_subagent_task,
    )

    for run_id in run_ids:
        try:
            task = agent.subagents.load(run_id)
            if not task_status_in(getattr(task, "status", ""), _ACTIVE_WORKER_STATUSES):
                continue
            cancel_subagent_task(
                agent,
                CancelSubagentTaskRequest(
                    task=task,
                    reason="audit_watch_closed_during_worker_create",
                    source="audit_source_worker",
                ),
            )
        except Exception:
            continue
    return True


# LLM: Periodic reconciliation scans persisted watch facts and invokes the same
# idempotent ensure function; it is not a separate scheduler or model loop.
# 函数用途: 在现有孤儿巡检节拍中补齐本 owner 丢失或提前退出的来源子代理。
def reconcile_audit_source_workers(agent: object) -> dict[str, object]:
    owner_home = _agent_owner_home(agent)
    if owner_home is None:
        return {"checked": 0, "active": 0, "waiting": 0, "degraded": 0}
    summary = _source_reconcile_summary()
    workers: list[dict[str, object]] = []
    capacity_groups: dict[str, list[tuple[WatchState, str]]] = {}
    for row in list_states(owner_home):
        state = _open_audit_watch(owner_home, row)
        if state is None:
            continue
        result = _reconcile_one_audit_source_worker(agent, state)
        workers.append(result)
        _record_source_reconcile_result(summary, capacity_groups, state, result)
    summary["capacity_alerts"] = _reconcile_capacity_groups(
        agent,
        summary,
        capacity_groups,
    )
    summary["workers"] = workers
    return summary


def retire_published_audit_sources(
    agent: object,
    audit_id: object,
    source_ids: object,
) -> int:
    """Retire only explicitly removed source memberships after publication.

    The source ids come from the before/after persisted binding tables. No URL,
    source content or natural-language instruction participates in selection.
    In-flight claims return through the existing cancellation/retry ledger.
    """

    owner_home = _agent_owner_home(agent)
    selected_audit = str(audit_id or "").strip()
    selected_sources = {
        str(item or "").strip()
        for item in (source_ids or ())
        if str(item or "").strip()
    }
    if owner_home is None or not selected_audit or not selected_sources:
        return 0
    from .watch_state import close_watch_state

    retired = 0
    for row in list_states(owner_home):
        if (
            str(row.get("audit_root_task_id") or "") != selected_audit
            or str(row.get("source_id") or "") not in selected_sources
            or bool(row.get("closed"))
        ):
            continue
        state = registry.get_or_load(owner_home, str(row.get("watch_id") or ""))
        if state is None or state.closed:
            continue
        if (
            state.audit_root_task_id != selected_audit
            or state.source_id not in selected_sources
        ):
            continue
        close_watch_state(state, reason="audit_source_membership_removed")
        _cancel_closed_source_worker(
            agent,
            state,
            reason="audit_source_membership_removed",
        )
        retired += 1
    return retired


def _source_reconcile_summary() -> dict[str, object]:
    return {
        "checked": 0,
        "active": 0,
        "waiting": 0,
        "degraded": 0,
        "finding_events_published": 0,
        "finding_events_pending": 0,
        "capacity_events_published": 0,
        "capacity_events_pending": 0,
        "duplicate_workers_retired": 0,
        "workers": [],
    }


def _open_audit_watch(owner_home: Path, row: dict[str, object]) -> WatchState | None:
    if not bool(row.get("audit_guarantee")) or bool(row.get("closed")):
        return None
    watch_id = str(row.get("watch_id") or "").strip()
    state = registry.get_or_load(owner_home, watch_id)
    return state if state is not None and not state.closed else None


def _reconcile_one_audit_source_worker(
    agent: object,
    state: WatchState,
) -> dict[str, object]:
    if not _source_state_matches_current_audit_epoch(agent, state):
        return _close_reconciled_source(
            agent,
            state,
            reason="audit_run_superseded",
            error_code="AUDIT_RUN_EPOCH_MISMATCH",
        )
    parent_active, parent_state = audit_parent_reconcile_state(
        agent,
        state.audit_root_task_id,
    )
    if not parent_active:
        if parent_state == "inactive":
            return _close_reconciled_source(
                agent,
                state,
                reason="audit_parent_inactive",
                parent_state=parent_state,
            )
        return {
            "ok": False,
            "state": "degraded",
            "error_code": "AUDIT_PARENT_STATE_UNAVAILABLE",
            "worker_key": audit_source_worker_key(state.audit_root_task_id, state.watch_id),
            "watch_id": state.watch_id,
            "parent_state": parent_state,
            "created": False,
        }
    result = ensure_audit_source_worker(agent, state)
    run_id = str(result.get("run_id") or "").strip()
    if run_id:
        result["finding_outbox"] = reconcile_audit_finding_outbox(agent, run_id)
    return result


def _close_reconciled_source(
    agent: object,
    state: WatchState,
    *,
    reason: str,
    error_code: str = "",
    parent_state: str = "",
) -> dict[str, object]:
    from .watch_state import close_watch_state

    close_watch_state(state, reason=reason)
    result: dict[str, object] = {
        "ok": True,
        "state": "not_required",
        "error_code": error_code,
        "worker_key": audit_source_worker_key(state.audit_root_task_id, state.watch_id),
        "watch_id": state.watch_id,
        "created": False,
        "closed": True,
        "worker_cancelled": _cancel_closed_source_worker(agent, state),
    }
    if parent_state:
        result["parent_state"] = parent_state
    return result


def _record_source_reconcile_result(
    summary: dict[str, object],
    capacity_groups: dict[str, list[tuple[WatchState, str]]],
    state: WatchState,
    result: dict[str, object],
) -> None:
    summary["checked"] = int(summary["checked"]) + 1
    if result.get("closed") is True:
        summary["skipped"] = int(summary.get("skipped") or 0) + 1
        return
    if result.get("error_code") == "AUDIT_PARENT_STATE_UNAVAILABLE":
        summary["degraded"] = int(summary["degraded"]) + 1
        return
    summary["duplicate_workers_retired"] = int(summary["duplicate_workers_retired"]) + int(
        result.get("duplicate_workers_retired") or 0
    )
    run_id = str(result.get("run_id") or "").strip()
    capacity_groups.setdefault(state.audit_root_task_id, []).append((state, run_id))
    outbox = result.get("finding_outbox")
    if isinstance(outbox, dict):
        summary["finding_events_published"] = int(summary["finding_events_published"]) + int(
            outbox.get("published") or 0
        )
        if not bool(outbox.get("ok")):
            summary["finding_events_pending"] = int(summary["finding_events_pending"]) + 1
    state_name = str(result.get("state") or "")
    if state_name == "active":
        summary["active"] = int(summary["active"]) + 1
    elif state_name.startswith("waiting"):
        summary["waiting"] = int(summary["waiting"]) + 1
    elif not bool(result.get("ok")):
        summary["degraded"] = int(summary["degraded"]) + 1


def _reconcile_capacity_groups(
    agent: object,
    summary: dict[str, object],
    capacity_groups: dict[str, list[tuple[WatchState, str]]],
) -> list[dict[str, object]]:
    capacity_alerts: list[dict[str, object]] = []
    for audit_id in sorted(capacity_groups):
        capacity = reconcile_audit_capacity_alert(agent, capacity_groups[audit_id])
        capacity_alerts.append({"audit_id": audit_id, **capacity})
        summary["capacity_events_published"] = int(summary["capacity_events_published"]) + int(
            capacity.get("published") or 0
        )
        if not bool(capacity.get("ok")):
            summary["capacity_events_pending"] = int(summary["capacity_events_pending"]) + 1
    return capacity_alerts


def reconcile_audit_capacity_alert(
    agent: object,
    source_workers: list[tuple[WatchState, str]],
    *,
    now: float | None = None,
) -> dict[str, object]:
    """Publish one edge/cooldown capacity event for one exact named Audit."""
    if not source_workers:
        return _outbox_pending("capacity_sources_missing")
    observed_at = time.time() if now is None else float(now)
    states = [state for state, _run_id in source_workers]
    audit_ids = {str(state.audit_root_task_id or "").strip() for state in states}
    owner_homes = {str(Path(state.owner_home).resolve(strict=False)) for state in states}
    run_epochs = {max(0, int(state.audit_run_epoch or 0)) for state in states}
    if "" in audit_ids or len(audit_ids) != 1:
        return _outbox_pending("capacity_audit_scope_mismatch")
    if len(owner_homes) != 1 or len(run_epochs) != 1:
        return _outbox_pending("capacity_source_scope_mismatch")
    audit_id = next(iter(audit_ids))
    run_epoch = next(iter(run_epochs))
    capacity = _aggregate_audit_capacity(source_workers, now=observed_at)
    alert = capacity.get("capacity_alert")
    active = isinstance(alert, dict) and alert.get("active") is True
    owner_home = Path(next(iter(owner_homes)))
    try:
        cursor_path = (
            audit_workspace_path(owner_home, audit_id) / "work" / "runtime" / "capacity-alert.json"
        )
    except ValueError:
        return _outbox_pending("capacity_audit_path_invalid")
    try:
        with locked_json_path(cursor_path):
            report = read_json_object_report(
                cursor_path,
                context="audit_capacity_alert.cursor",
            )
            if report.load_error is not None:
                return _outbox_pending("capacity_cursor_unreadable")
            cursor = dict(report.payload)
            try:
                cursor_epoch = max(0, int(cursor.get("run_epoch") or 0))
            except (TypeError, ValueError):
                cursor_epoch = -1
            same_epoch = cursor_epoch == run_epoch
            was_active = same_epoch and cursor.get("active") is True
            notification_count = (
                max(0, int(cursor.get("notification_count") or 0)) if same_epoch else 0
            )
            cooldown = min(_capacity_alert_cooldown(state) for state in states)
            last_notified_at = float(cursor.get("last_notified_at") or 0.0) if same_epoch else 0.0
            should_publish = (
                active and (not was_active or observed_at >= last_notified_at + cooldown)
            ) or (not active and was_active)
            if not should_publish:
                return {
                    "ok": True,
                    "state": "unchanged",
                    "published": 0,
                    "active": active,
                    "source_count": len(states),
                    "alert_source_count": int(capacity.get("alert_source_count") or 0),
                }
            published = _publish_audit_capacity_event(
                agent,
                audit_id=audit_id,
                run_epoch=run_epoch,
                capacity=capacity,
                active=active,
                sequence=notification_count + 1,
            )
            if not bool(published.get("ok")):
                return published
            write_json_file_atomic_unlocked(
                cursor_path,
                {
                    "schema_version": _CAPACITY_ALERT_CURSOR_SCHEMA,
                    "audit_id": audit_id,
                    "run_epoch": run_epoch,
                    "active": active,
                    "notification_count": notification_count + 1,
                    "last_notified_at": observed_at,
                    "source_count": len(states),
                    "alert_source_count": int(capacity.get("alert_source_count") or 0),
                    "updated_at": observed_at,
                },
            )
    except OSError as exc:
        return _outbox_pending(f"capacity_cursor_io_failed:{type(exc).__name__}")
    return {
        **published,
        "published": 1,
        "active": active,
        "source_count": len(states),
        "alert_source_count": int(capacity.get("alert_source_count") or 0),
    }


def _capacity_alert_cooldown(state: WatchState) -> int:
    try:
        return max(
            30,
            int(
                getattr(
                    state.tuning,
                    "capacity_alert_cooldown_seconds",
                    900,
                )
                or 900
            ),
        )
    except (TypeError, ValueError):
        return 900


def _aggregate_audit_capacity(
    source_workers: list[tuple[WatchState, str]],
    *,
    now: float,
) -> dict[str, object]:
    """Combine per-source mechanical health without changing source authority."""
    from .harvester import audit_capacity_facts

    sources: list[dict[str, object]] = []
    for state, run_id in source_workers:
        facts = audit_capacity_facts(state, now=now)
        alert = facts.get("capacity_alert")
        throughput = facts.get("processing_throughput")
        latency = facts.get("processing_latency")
        sources.append(
            {
                "source_id": state.source_id,
                "watch_id": state.watch_id,
                "run_id": str(run_id or ""),
                # This aggregate is built from the currently supervised source
                # worker set.  Expose that mechanical fact so an owner-facing
                # capacity report does not mistake a cold throughput sample for
                # "no worker exists" and recommend duplicate dispatch.
                "worker_active": bool(str(run_id or "").strip()),
                "pending": int(facts.get("pending") or 0),
                "oldest_pending_age_seconds": float(facts.get("oldest_pending_age_seconds") or 0.0),
                "ingest_records_per_second": float(facts.get("ingest_records_per_second") or 0.0),
                "processing_records_per_second": float(throughput.get("records_per_second") or 0.0)
                if isinstance(throughput, dict)
                else 0.0,
                "processing_latency": dict(latency) if isinstance(latency, dict) else {},
                "alert_active": isinstance(alert, dict) and alert.get("active") is True,
                "reasons": list(alert.get("reasons") or []) if isinstance(alert, dict) else [],
            }
        )
    sources.sort(key=lambda item: str(item.get("watch_id") or ""))
    alert_sources = [item for item in sources if item["alert_active"] is True]
    latency_keys = ("p50_seconds", "p95_seconds", "p99_seconds", "max_seconds")
    latency = {
        key: max(
            (
                float(item["processing_latency"].get(key) or 0.0)
                for item in sources
                if isinstance(item.get("processing_latency"), dict)
            ),
            default=0.0,
        )
        for key in latency_keys
    }
    latency["sample_count"] = sum(
        int(item["processing_latency"].get("sample_count") or 0)
        for item in sources
        if isinstance(item.get("processing_latency"), dict)
    )
    return {
        "schema_version": "audit-capacity-aggregate.v1",
        "source_count": len(sources),
        "active_worker_count": sum(1 for item in sources if item["worker_active"] is True),
        "alert_source_count": len(alert_sources),
        "pending": sum(int(item["pending"]) for item in sources),
        "oldest_pending_age_seconds": max(
            (float(item["oldest_pending_age_seconds"]) for item in sources),
            default=0.0,
        ),
        "ingest_records_per_second": round(
            sum(float(item["ingest_records_per_second"]) for item in sources),
            6,
        ),
        "processing_throughput": {
            "records_per_second": round(
                sum(float(item["processing_records_per_second"]) for item in sources),
                6,
            ),
        },
        "processing_latency": latency,
        "capacity_alert": {
            "active": bool(alert_sources),
            "reasons": sorted(
                {str(reason) for item in alert_sources for reason in item["reasons"] if str(reason)}
            ),
        },
        "sources": sources,
        "observed_at": now,
    }


def _publish_audit_capacity_event(
    agent: object,
    *,
    audit_id: str,
    run_epoch: int,
    capacity: dict[str, Any],
    active: bool,
    sequence: int,
) -> dict[str, object]:
    store = getattr(agent, "conversation_store", None)
    if store is None or not callable(getattr(store, "append_observation_with_wake", None)):
        return _outbox_pending("conversation_event_store_unavailable")
    thread_id = _thread_id_for_task(agent, audit_id)
    if not thread_id:
        return _outbox_pending("conversation_thread_unavailable")
    alert = capacity.get("capacity_alert")
    reasons = list(alert.get("reasons") or []) if isinstance(alert, dict) else []
    metadata = {
        "schema_version": "audit-capacity-event.v2",
        "audit_id": audit_id,
        "run_epoch": run_epoch,
        "capacity_state": "alert" if active else "recovered",
        "source_count": int(capacity.get("source_count") or 0),
        "active_worker_count": int(capacity.get("active_worker_count") or 0),
        "alert_source_count": int(capacity.get("alert_source_count") or 0),
        "pending": int(capacity.get("pending") or 0),
        "oldest_pending_age_seconds": float(capacity.get("oldest_pending_age_seconds") or 0.0),
        "ingest_records_per_second": float(capacity.get("ingest_records_per_second") or 0.0),
        "processing_throughput": dict(capacity.get("processing_throughput") or {}),
        "processing_latency": dict(capacity.get("processing_latency") or {}),
        "reasons": reasons,
        "sources": [
            dict(item) for item in (capacity.get("sources") or []) if isinstance(item, dict)
        ],
    }
    summary = (
        f"Audit 容量告警：{metadata['source_count']} 路来源中 "
        f"{metadata['alert_source_count']} 路超过阈值，待判共 {metadata['pending']} 条，"
        f"最老待判 {metadata['oldest_pending_age_seconds']:.1f} 秒。"
        if active
        else (
            f"Audit 容量已恢复：{metadata['source_count']} 路来源均回到配置阈值以内，"
            f"当前待判共 {metadata['pending']} 条。"
        )
    )
    source_agent_ids = sorted(
        {
            str(item.get("run_id") or "")
            for item in metadata["sources"]
            if str(item.get("run_id") or "")
        }
    )
    source_agent_id = source_agent_ids[0] if source_agent_ids else ""
    try:
        observation, signal = store.append_observation_with_wake(
            {
                "thread_id": thread_id,
                "event_type": "audit_capacity_alert",
                "summary": summary,
                "urgency": "urgent" if active else "normal",
                "source_agent_id": source_agent_id,
                "parent_agent_id": audit_id,
                "root_task_id": audit_id,
                "requires_main_agent": True,
                "metadata": metadata,
            },
            {
                "thread_id": thread_id,
                "reason": "audit_capacity_alert",
                "urgency": "urgent" if active else "normal",
                "source_agent_id": source_agent_id,
                "parent_agent_id": audit_id,
                "root_task_id": audit_id,
                "summary": summary,
                "dedupe_key": (
                    f"audit-capacity:{audit_id}:{run_epoch}:"
                    f"{sequence}:{'alert' if active else 'recovered'}"
                ),
                "metadata": metadata,
            },
        )
    except Exception as exc:
        return _outbox_pending(f"capacity_event_publish_failed:{type(exc).__name__}")
    return {
        "ok": True,
        "state": "published",
        "observation_id": str(getattr(observation, "observation_id", "") or ""),
        "wake_signal_id": str(getattr(signal, "wake_signal_id", "") or ""),
    }


# LLM: Restart recovery may recreate workers only for the exact durable Audit
# link that is still active. A persisted watch alone cannot resurrect completed
# or cancelled named work, and unreadable lifecycle state fails closed.
# 函数用途: 用会话任务账核验来源工作者的父 Audit 仍有效，避免重启后反复创建再取消孤儿。
def audit_parent_reconcile_state(
    agent: object,
    audit_id: object,
) -> tuple[bool, str]:
    selected = str(audit_id or "").strip()
    store = getattr(agent, "conversation_store", None)
    if store is None:
        # Standalone/internal agents without a conversation store retain the
        # pre-existing direct worker lifecycle.
        return True, "unscoped"
    if not selected or not callable(getattr(store, "thread_for_task", None)):
        return False, "unavailable"
    try:
        thread = store.thread_for_task(selected)
        thread_id = str(getattr(thread, "thread_id", "") or "").strip()
        if not thread_id:
            return False, "inactive"
        if callable(getattr(store, "task_links_report", None)):
            links, errors = store.task_links_report(thread_id)
            if errors:
                return False, "unavailable"
        else:
            links = store.task_links(thread_id)
    except Exception:
        return False, "unavailable"
    matches = [
        link for link in links if str(getattr(link, "task_id", "") or "").strip() == selected
    ]
    if len(matches) != 1:
        return False, "unavailable" if len(matches) > 1 else "inactive"
    link = matches[0]
    # ``expires_at`` is the collection deadline, not the destruction time of
    # already-durable work.  The harvester performs its final boundary read and
    # stops; the exact active Audit link continues authorizing its source
    # workers until every queued record is ACKed.  Only an explicit clear or a
    # completed/cancelled link removes that authority.
    active = (
        str(getattr(link, "status", "") or "").strip().lower() == "active"
        and str(getattr(link, "work_kind", "") or "").strip().lower() == "audit"
    )
    return (True, "active") if active else (False, "inactive")


def _source_state_matches_current_audit_epoch(
    agent: object,
    state: WatchState,
) -> bool:
    """Fence old-run source attempts after the same named Audit is restarted."""

    store = getattr(agent, "conversation_store", None)
    loader = getattr(store, "load_task_link", None)
    if not callable(loader):
        return True
    try:
        link = loader(str(state.audit_root_task_id or "").strip())
    except Exception:
        return False
    if link is None:
        return False
    return _normalized_run_epoch(
        getattr(link, "run_epoch", 0),
    ) == _normalized_run_epoch(
        state.audit_run_epoch,
    )


def _normalized_run_epoch(value: object) -> int:
    """Normalize one typed named-Audit run epoch without reading prose."""

    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


# LLM: The append-only finding ledger is the durable outbox.  Its cursor moves
# only after the shared conversation event store accepted one exact revision,
# so a crash replays safely through the existing wake dedupe key.
# 函数用途: 把来源子代理已落盘但尚未投影的 Audit finding 补发到统一会话事件链。
def reconcile_audit_finding_outbox(
    agent: object,
    run_id: str,
) -> dict[str, object]:
    manager = getattr(agent, "subagents", None)
    if manager is None or not callable(getattr(manager, "load", None)):
        return _outbox_pending("subagent_manager_unavailable")
    try:
        task = manager.load(str(run_id or "").strip())
    except Exception as exc:
        return _outbox_pending(f"task_load_failed:{type(exc).__name__}")
    attrs = getattr(task, "attributes", {}) or {}
    if not structured_audit_source_worker_attributes(attrs):
        return {"ok": True, "published": 0, "state": "not_applicable"}
    inline_projection = _project_inline_audit_findings(agent, task)
    if not bool(inline_projection.get("ok")):
        return {
            **_outbox_pending(str(inline_projection.get("error") or "inline_projection_pending")),
            "inline_projection": inline_projection,
        }
    ledger_text = str(getattr(task, "agent_run_findings_jsonl", "") or "").strip()
    if not ledger_text:
        return _outbox_pending("finding_ledger_unavailable")
    ledger_path = Path(ledger_text)
    if not ledger_path.exists():
        return {"ok": True, "published": 0, "state": "empty"}
    cursor_path = ledger_path.with_name("finding_events.cursor.json")
    try:
        result = _drain_finding_outbox(agent, task, ledger_path, cursor_path)
    except OSError as exc:
        return _outbox_pending(f"io_failed:{type(exc).__name__}")
    if not bool(result.get("ok")):
        return {**result, "inline_projection": inline_projection}
    delivery_replay = _requeue_unreported_finding_deliveries(agent, task, ledger_path)
    return {
        **result,
        "inline_projection": inline_projection,
        "owner_delivery_replay": delivery_replay,
    }


def audit_task_has_unreported_findings(agent: object, audit_id: str) -> bool:
    """Fail closed while a report-required finding lacks a sent receipt."""

    selected = str(audit_id or "").strip()
    manager = getattr(agent, "subagents", None)
    if not selected or manager is None:
        return False
    try:
        report = manager.list_runs_report()
        if list(getattr(report, "load_errors", []) or []):
            return True
        tasks = list(getattr(report, "runs", []) or [])
    except Exception:
        return True
    refs_by_owner: dict[Path, list[str]] = {}
    for task in tasks:
        attrs = dict(getattr(task, "attributes", {}) or {})
        if (
            not structured_audit_source_worker_attributes(attrs)
            or str(attrs.get(CONVERSATION_REQUEST_ID_ATTR) or "").strip() != selected
        ):
            continue
        ledger_text = str(getattr(task, "agent_run_findings_jsonl", "") or "").strip()
        if not ledger_text:
            continue
        rows, readable = _strict_finding_ledger_rows(Path(ledger_text))
        if not readable:
            return True
        owner_home = Path(str(attrs.get(AUDIT_SOURCE_OWNER_HOME_ATTR) or ""))
        for row in rows:
            if row.get("audit_finding") is not True or not bool(
                row.get("requires_llm_report")
            ):
                continue
            if _audit_finding_task_mismatch(task, row):
                return True
            refs = _finding_delivery_refs(row)
            if not refs:
                return True
            refs_by_owner.setdefault(owner_home, []).extend(refs)
    from .harvester import audit_source_refs_reported

    # One lifecycle-ledger read per watch, not one full read per finding.  A
    # busy Audit may retain hundreds of report-required findings; repeatedly
    # rescanning the same receipt ledger made terminal reconciliation
    # quadratic and could hold the task transition lock for user-visible
    # status requests.
    return any(
        not audit_source_refs_reported(owner_home, tuple(dict.fromkeys(refs)))
        for owner_home, refs in refs_by_owner.items()
    )


def _drain_finding_outbox(
    agent: object,
    task: object,
    ledger_path: Path,
    cursor_path: Path,
) -> dict[str, object]:
    with ExitStack() as stack:
        stack.enter_context(locked_json_path(ledger_path))
        stack.enter_context(locked_json_path(cursor_path))
        cursor_report = read_json_object_report(
            cursor_path,
            context="audit_finding_outbox.cursor",
        )
        if cursor_report.load_error is not None:
            return _outbox_pending("cursor_unreadable")
        offset = max(0, int(cursor_report.payload.get("offset_bytes") or 0))
        size = ledger_path.stat().st_size
        if offset > size:
            return _outbox_pending("cursor_beyond_ledger")
        return _scan_finding_outbox(
            agent,
            task,
            ledger_path,
            cursor_path,
            offset,
            size,
        )


def _scan_finding_outbox(
    agent: object,
    task: object,
    ledger_path: Path,
    cursor_path: Path,
    offset: int,
    size: int,
) -> dict[str, object]:
    published = 0
    with ledger_path.open("rb") as handle:
        handle.seek(offset)
        while raw_line := handle.readline():
            row = _decoded_ledger_row(raw_line)
            if row is None:
                error = (
                    "finding_ledger_partial_line"
                    if not raw_line.endswith(b"\n")
                    else "finding_ledger_invalid_row"
                )
                return _outbox_pending(error, published=published)
            if row.get("audit_finding") is True:
                event = _publish_finding_outbox_row(agent, task, row, published, offset)
                if event is not None:
                    return event
                published += 1
            offset = handle.tell()
            _write_finding_outbox_cursor(cursor_path, task, row, offset, size)
    return {
        "ok": True,
        "published": published,
        "state": "caught_up",
        "offset_bytes": offset,
    }


def _decoded_ledger_row(raw_line: bytes) -> dict[str, Any] | None:
    if not raw_line.endswith(b"\n"):
        return None
    try:
        row = json.loads(raw_line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return row if isinstance(row, dict) else None


def _strict_finding_ledger_rows(path: Path) -> tuple[list[dict[str, Any]], bool]:
    if not path.exists():
        return [], True
    rows: list[dict[str, Any]] = []
    try:
        with path.open("rb") as handle:
            for raw_line in handle:
                row = _decoded_ledger_row(raw_line)
                if row is None:
                    return rows, False
                rows.append(row)
    except OSError:
        return rows, False
    return rows, True


def _finding_source_refs(row: dict[str, Any]) -> list[str]:
    raw = row.get("source_refs") or row.get("evidence_refs") or []
    return list(
        dict.fromkeys(
            str(item or "").strip()
            for item in (raw if isinstance(raw, (list, tuple)) else [])
            if str(item or "").strip()
        )
    )


def _finding_delivery_refs(row: dict[str, Any]) -> list[str]:
    """Return the finding-owned Audit refs that authorize one owner delivery.

    ``source_refs`` deliberately accepts supplementary paths, URLs and event
    identifiers so the model can explain a finding.  Those references are not
    delivery identities.  New finding rows already persist the mechanically
    inspected verdict rows; use those exact refs for delivery receipts and
    replay suppression.  The fallback only supports older ledgers and still
    requires an owner-local Audit ref for the finding's own watch.
    """

    watch_id = str(row.get("watch_id") or "").strip()
    if not watch_id:
        return []
    from .harvester import parse_audit_source_ref

    verdicts = row.get("evidence_verdicts")
    if isinstance(verdicts, list):
        refs: list[str] = []
        for verdict in verdicts:
            if not isinstance(verdict, dict):
                continue
            source_ref = str(verdict.get("source_ref") or "").strip()
            parsed = parse_audit_source_ref(source_ref)
            if parsed is None or parsed[0] != watch_id:
                continue
            ack_id = str(verdict.get("ack_id") or "").strip()
            if ack_id and ack_id != parsed[1]:
                continue
            refs.append(source_ref)
        # A typed verdict list is authoritative.  Do not silently fall back to
        # broader supporting evidence if it is present but malformed.
        return list(dict.fromkeys(refs))

    return list(
        dict.fromkeys(
            source_ref
            for source_ref in _finding_source_refs(row)
            if (
                (parsed := parse_audit_source_ref(source_ref)) is not None
                and parsed[0] == watch_id
            )
        )
    )


def _pending_finding_delivery_keys(
    store: object,
    audit_id: str,
) -> set[tuple[str, int]] | None:
    try:
        signals = store.pending_wake_signals(limit=0)
    except Exception:
        return None
    keys: set[tuple[str, int]] = set()
    for signal in signals:
        metadata = getattr(signal, "metadata", None)
        metadata = metadata if isinstance(metadata, dict) else {}
        if (
            str(getattr(signal, "root_task_id", "") or "").strip() != audit_id
            or str(getattr(signal, "reason", "") or "").strip().lower()
            != "audit_finding"
            or str(metadata.get("schema_version") or "") != "audit-finding-event.v1"
        ):
            continue
        finding_id = str(metadata.get("finding_id") or "").strip()
        if finding_id:
            keys.add((finding_id, max(1, int(metadata.get("revision") or 1))))
    return keys


def _requeue_unreported_finding_deliveries(
    agent: object,
    task: object,
    ledger_path: Path,
) -> dict[str, object]:
    """Repair a missing wake only when the durable sent receipt is absent."""

    rows, readable = _strict_finding_ledger_rows(ledger_path)
    if not readable:
        return _outbox_pending("finding_ledger_unreadable")
    attrs = dict(getattr(task, "attributes", {}) or {})
    audit_id = str(attrs.get(CONVERSATION_REQUEST_ID_ATTR) or "").strip()
    owner_home = Path(str(attrs.get(AUDIT_SOURCE_OWNER_HOME_ATTR) or ""))
    store = getattr(agent, "conversation_store", None)
    if not audit_id or store is None:
        return _outbox_pending("owner_delivery_state_unavailable")
    pending = _pending_finding_delivery_keys(store, audit_id)
    if pending is None:
        return _outbox_pending("owner_delivery_queue_unreadable")
    requeued = 0
    from .harvester import audit_source_refs_reported

    for row in rows:
        if row.get("audit_finding") is not True or not bool(
            row.get("requires_llm_report")
        ):
            continue
        mismatch = _audit_finding_task_mismatch(task, row)
        if mismatch:
            return _outbox_pending(mismatch, published=requeued)
        finding_id = str(row.get("id") or "").strip()
        revision = max(1, int(row.get("revision") or 1))
        refs = _finding_delivery_refs(row)
        if not finding_id or not refs:
            return _outbox_pending("finding_delivery_identity_unavailable", published=requeued)
        if audit_source_refs_reported(owner_home, refs):
            continue
        if (finding_id, revision) in pending:
            continue
        event = _publish_audit_finding_event(agent, task, row)
        if not bool(event.get("ok")):
            return {**event, "requeued": requeued}
        pending.add((finding_id, revision))
        requeued += 1
    return {"ok": True, "state": "caught_up", "requeued": requeued}


def _publish_finding_outbox_row(
    agent: object,
    task: object,
    row: dict[str, Any],
    published: int,
    offset: int,
) -> dict[str, object] | None:
    mismatch = _audit_finding_task_mismatch(task, row)
    if mismatch:
        return _outbox_pending(mismatch, published=published)
    event = _publish_audit_finding_event(agent, task, row)
    if bool(event.get("ok")):
        return None
    return {**event, "published": published, "offset_bytes": offset}


def _write_finding_outbox_cursor(
    cursor_path: Path,
    task: object,
    row: dict[str, Any],
    offset: int,
    size: int,
) -> None:
    write_json_file_atomic_unlocked(
        cursor_path,
        {
            "schema_version": _FINDING_OUTBOX_SCHEMA,
            "run_id": str(getattr(task, "id", "") or ""),
            "offset_bytes": offset,
            "ledger_size_bytes": size,
            "last_finding_id": str(row.get("id") or ""),
            "last_revision": int(row.get("revision") or 0),
            "updated_at": time.time(),
        },
    )


@dataclass(frozen=True)
class _InlineProjectionContext:
    state: WatchState
    audit_id: str
    source_id: str
    watch_id: str
    owner_id: str
    finding_ledger_path: Path
    verdict_path: Path
    cursor_path: Path


@dataclass
class _InlineProjectionProgress:
    offset: int
    size: int
    projected_total: int
    projected: int = 0
    reused: int = 0
    last_finding_id: str = ""


def _project_inline_audit_findings(
    agent: object,
    task: object,
) -> dict[str, object]:
    """Recover verdict-owned findings into the existing per-run ledger."""
    context, error = _inline_projection_context(agent, task)
    if error is not None:
        return error
    assert context is not None
    if not context.verdict_path.exists():
        return {"ok": True, "state": "empty", "projected": 0, "reused": 0}
    try:
        with locked_json_path(context.cursor_path):
            progress, error = _inline_projection_progress(context)
            if error is not None:
                return error
            assert progress is not None
            error = _scan_inline_verdicts(context, progress)
            if error is not None:
                return error
            _write_inline_projection_cursor(context, progress)
    except (OSError, ValueError) as exc:
        return _inline_projection_error(f"inline_projection_io_failed:{type(exc).__name__}")
    return _inline_projection_success(progress)


def _inline_projection_context(
    agent: object,
    task: object,
) -> tuple[_InlineProjectionContext | None, dict[str, object] | None]:
    attrs = getattr(task, "attributes", {}) or {}
    owner_home = Path(str(attrs.get(AUDIT_SOURCE_OWNER_HOME_ATTR) or ""))
    watch_id = str(attrs.get(AUDIT_SOURCE_WATCH_ID_ATTR) or "").strip()
    audit_id = str(attrs.get(CONVERSATION_REQUEST_ID_ATTR) or "").strip()
    source_id = str(attrs.get(AUDIT_SOURCE_ID_ATTR) or "").strip()
    state = load_state(owner_home, watch_id) if watch_id else None
    if state is None or state.audit_root_task_id != audit_id or state.source_id != source_id:
        return None, _inline_projection_error("inline_projection_source_binding_unavailable")
    finding_ledger_text = str(getattr(task, "agent_run_findings_jsonl", "") or "").strip()
    if not finding_ledger_text:
        return None, _inline_projection_error("inline_projection_finding_ledger_unavailable")
    verdict_path = state_dir(owner_home) / f"{watch_id}.verdicts.ndjson"
    cursor_path = state_dir(owner_home) / f"{watch_id}.inline-findings.cursor.json"
    owner_id = str(getattr(getattr(agent, "home_paths", None), "owner_id", "") or state.owner_id)
    return (
        _InlineProjectionContext(
            state=state,
            audit_id=audit_id,
            source_id=source_id,
            watch_id=watch_id,
            owner_id=owner_id,
            finding_ledger_path=Path(finding_ledger_text),
            verdict_path=verdict_path,
            cursor_path=cursor_path,
        ),
        None,
    )


def _inline_projection_progress(
    context: _InlineProjectionContext,
) -> tuple[_InlineProjectionProgress | None, dict[str, object] | None]:
    report = read_json_object_report(
        context.cursor_path,
        context="audit_inline_finding_projection.cursor",
    )
    if report.load_error is not None:
        return None, _inline_projection_error("inline_projection_cursor_unreadable")
    offset = max(0, int(report.payload.get("offset_bytes") or 0))
    size = context.verdict_path.stat().st_size
    if offset > size:
        return None, _inline_projection_error("inline_projection_cursor_beyond_ledger")
    return (
        _InlineProjectionProgress(
            offset=offset,
            size=size,
            projected_total=max(0, int(report.payload.get("projected_total") or 0)),
        ),
        None,
    )


def _scan_inline_verdicts(
    context: _InlineProjectionContext,
    progress: _InlineProjectionProgress,
) -> dict[str, object] | None:
    with context.verdict_path.open("rb") as handle:
        handle.seek(progress.offset)
        while raw_line := handle.readline():
            row = _decoded_ledger_row(raw_line)
            if row is None:
                error = (
                    "inline_projection_partial_verdict_row"
                    if not raw_line.endswith(b"\n")
                    else "inline_projection_invalid_verdict_row"
                )
                return _inline_projection_error(error, progress)
            row_error = _project_inline_verdict_row(context, progress, row)
            if row_error is not None:
                return row_error
            progress.offset = handle.tell()
    return None


def _project_inline_verdict_row(
    context: _InlineProjectionContext,
    progress: _InlineProjectionProgress,
    row: dict[str, Any],
) -> dict[str, object] | None:
    if not isinstance(row.get("finding"), dict):
        return None
    expected = {
        "owner_id": context.owner_id,
        "audit_id": context.audit_id,
        "source_id": context.source_id,
        "watch_id": context.watch_id,
    }
    if any(str(row.get(key) or "") != value for key, value in expected.items()):
        return _inline_projection_error("inline_projection_verdict_scope_mismatch", progress)
    stage = str(row.get("stage") or "initial")
    valid_initial = stage == "initial" and row.get("acknowledged") is True
    valid_review = stage == "review" and row.get("acknowledged") is False
    if not (valid_initial or valid_review):
        return _inline_projection_error("inline_projection_unacknowledged_verdict", progress)
    if valid_review and not _review_has_initial_verdict(context.state, row):
        return _inline_projection_error("inline_projection_review_without_initial", progress)
    from ..agent_core.runtime.record_finding_tool import append_inline_audit_finding

    recorded, _revision, record = append_inline_audit_finding(
        context.finding_ledger_path,
        state=context.state,
        verdict_row=row,
    )
    progress.last_finding_id = str(record.get("id") or "")
    progress.projected += int(recorded)
    progress.reused += int(not recorded)
    progress.projected_total += 1
    return None


def _review_has_initial_verdict(state: WatchState, row: dict[str, Any]) -> bool:
    from .harvester import inspect_audit_record

    inspected = inspect_audit_record(state, str(row.get("ack_id") or ""))
    status = inspected.get("processing_status")
    return (
        inspected.get("ok") is True
        and isinstance(status, dict)
        and status.get("acknowledged") is True
    )


def _write_inline_projection_cursor(
    context: _InlineProjectionContext,
    progress: _InlineProjectionProgress,
) -> None:
    write_json_file_atomic_unlocked(
        context.cursor_path,
        {
            "schema_version": _INLINE_FINDING_PROJECTION_SCHEMA,
            "audit_id": context.audit_id,
            "source_id": context.source_id,
            "watch_id": context.watch_id,
            "offset_bytes": progress.offset,
            "ledger_size_bytes": progress.size,
            "last_finding_id": progress.last_finding_id,
            "projected_total": progress.projected_total,
            "updated_at": time.time(),
        },
    )


def _inline_projection_error(
    error: str,
    progress: _InlineProjectionProgress | None = None,
) -> dict[str, object]:
    return {
        "ok": False,
        "state": "pending",
        "error": error,
        **({"projected": progress.projected, "reused": progress.reused} if progress else {}),
    }


def _inline_projection_success(
    progress: _InlineProjectionProgress,
) -> dict[str, object]:
    return {
        "ok": True,
        "state": "caught_up",
        "projected": progress.projected,
        "reused": progress.reused,
        "projected_total": progress.projected_total,
        "offset_bytes": progress.offset,
    }


def _audit_finding_task_mismatch(
    task: object,
    row: dict[str, Any],
) -> str:
    attrs = getattr(task, "attributes", {}) or {}
    expected = {
        "audit_id": str(attrs.get(CONVERSATION_REQUEST_ID_ATTR) or ""),
        "source_id": str(attrs.get(AUDIT_SOURCE_ID_ATTR) or ""),
        "watch_id": str(attrs.get(AUDIT_SOURCE_WATCH_ID_ATTR) or ""),
    }
    for key, value in expected.items():
        if str(row.get(key) or "") != value:
            return f"finding_{key}_mismatch"
    return ""


def _publish_audit_finding_event(
    agent: object,
    task: object,
    row: dict[str, Any],
) -> dict[str, object]:
    store = getattr(agent, "conversation_store", None)
    if store is None or not callable(getattr(store, "append_observation_with_wake", None)):
        return _outbox_pending("conversation_event_store_unavailable")
    attrs = getattr(task, "attributes", {}) or {}
    audit_id = str(attrs.get(CONVERSATION_REQUEST_ID_ATTR) or "").strip()
    thread_id = str(attrs.get("conversation_thread_id") or "").strip()
    if not thread_id and callable(getattr(store, "thread_for_task", None)):
        try:
            thread = store.thread_for_task(audit_id)
        except Exception as exc:
            return _outbox_pending(f"thread_lookup_failed:{type(exc).__name__}")
        thread_id = str(getattr(thread, "thread_id", "") or "").strip()
    if not thread_id:
        return _outbox_pending("conversation_thread_unavailable")
    finding_id = str(row.get("id") or "").strip()
    revision = max(1, int(row.get("revision") or 1))
    urgency = str(row.get("urgency") or "normal").strip().lower()
    if urgency not in {"normal", "urgent"}:
        urgency = "normal"
    evidence_refs = [
        str(item).strip()
        for item in (row.get("source_refs") or row.get("evidence_refs") or [])
        if str(item or "").strip()
    ]
    delivery_evidence_refs = _finding_delivery_refs(row)
    if bool(row.get("requires_llm_report")) and not delivery_evidence_refs:
        return _outbox_pending("finding_delivery_identity_unavailable")
    metadata = {
        "schema_version": "audit-finding-event.v1",
        # A finding wake is an incremental report request.  It is not a
        # completed-window or whole-Audit summary, even when several wakes are
        # coalesced into one owner-facing turn.
        "report_scope": "incremental",
        "owner_id": str(row.get("owner_id") or ""),
        "audit_id": audit_id,
        "audit_run_epoch": max(0, int(row.get("audit_run_epoch") or 0)),
        "ingest_run_epoch": max(0, int(row.get("ingest_run_epoch") or 0)),
        "source_id": str(row.get("source_id") or ""),
        "watch_id": str(row.get("watch_id") or ""),
        "finding_id": finding_id,
        "revision": revision,
        "stage": str(row.get("stage") or ""),
        "report_status": str(row.get("report_status") or ""),
        "needs_evidence": bool(row.get("needs_evidence")),
        "verdict": row.get("verdict"),
        "score": row.get("score"),
        "requires_llm_report": bool(row.get("requires_llm_report")),
        "delivery_evidence_refs": delivery_evidence_refs,
        "evidence_records": [
            dict(item)
            for item in (row.get("evidence_records") or [])
            if isinstance(item, dict)
        ],
    }
    requires_report = bool(row.get("requires_llm_report"))
    observation_payload = {
        "thread_id": thread_id,
        "event_type": "audit_finding",
        "summary": str(row.get("claim") or ""),
        "urgency": urgency,
        "source_agent_id": str(getattr(task, "id", "") or ""),
        "parent_agent_id": audit_id,
        "root_task_id": audit_id,
        "evidence_refs": evidence_refs,
        # Findings that are retained only for audit/review stay in the event
        # ledger.  Only a typed report request may wake the owner-facing model.
        "requires_main_agent": requires_report,
        "requires_llm_report": requires_report,
        "metadata": metadata,
    }
    try:
        if requires_report:
            observation, signal = store.append_observation_with_wake(
                observation_payload,
                {
                    "thread_id": thread_id,
                    "urgency": urgency,
                    "reason": "audit_finding",
                    "source_agent_id": str(getattr(task, "id", "") or ""),
                    "parent_agent_id": audit_id,
                    "root_task_id": audit_id,
                    "summary": str(row.get("claim") or ""),
                    "evidence_refs": evidence_refs,
                    "dedupe_key": (f"audit-finding:{audit_id}:{finding_id}:{revision}"),
                    "metadata": metadata,
                },
            )
        else:
            observation = store.append_observation(observation_payload)
            signal = None
    except Exception as exc:
        return _outbox_pending(f"event_publish_failed:{type(exc).__name__}")
    return {
        "ok": True,
        "state": "published",
        "finding_id": finding_id,
        "revision": revision,
        "observation_id": str(getattr(observation, "observation_id", "") or ""),
        "wake_signal_id": str(getattr(signal, "wake_signal_id", "") or ""),
        "report_requested": requires_report,
    }


def _outbox_pending(
    reason: str,
    *,
    published: int = 0,
) -> dict[str, object]:
    return {
        "ok": False,
        "state": "pending",
        "reason": str(reason or ""),
        "published": max(0, int(published)),
    }


# LLM: A source worker's completion condition is derived from the persisted
# watch window and unacknowledged ledger, never from its final prose.
# 函数用途: 判断来源子代理是否仍有持续采集或待判记录，供 runner 收口和恢复复用。
def source_worker_task_incomplete(task: object, *, now: float | None = None) -> bool:
    state = source_worker_lifecycle_state(task, now=now)
    return state not in {"complete", "closed", "not_source_worker"}


# LLM: A named Audit source worker is governed by its durable watch window and
# ACK ledger, not by the short-lived foreground request link that originally
# created it. Missing/corrupt authority remains a hold, never an implicit allow.
# 函数用途: 给统一会话生命周期闸返回来源岗位的 active/complete/unavailable 结构状态。
def source_worker_lifecycle_state(
    task: object,
    *,
    now: float | None = None,
) -> str:
    attrs = getattr(task, "attributes", {}) or {}
    if not structured_audit_source_worker_attributes(attrs):
        return "not_source_worker"
    assert isinstance(attrs, dict)
    raw_owner_home = str(attrs.get(AUDIT_SOURCE_OWNER_HOME_ATTR) or "").strip()
    watch_id = str(attrs.get(AUDIT_SOURCE_WATCH_ID_ATTR) or "")
    if not raw_owner_home or not watch_id:
        return "unavailable"
    owner_home = Path(raw_owner_home)
    state = registry.get_or_load(owner_home, watch_id)
    if state is None:
        return "unavailable"
    # The Gateway, a CLI worker and an exact named-clear command may run in
    # different processes.  Refresh the persisted scalar authority before
    # deciding whether a cached watch is active, naturally complete or closed.
    with state.lock:
        refresh_scalars_from_disk(state)
        if state.closed:
            return "closed"
        if not _source_watch_incomplete(state, now=now):
            return "complete"
        # The harvester is model-independent and remains active for the whole
        # collection window.  A source worker needs a model turn only when the
        # durable ACK ledger proves there is actual work.  Keeping the logical
        # worker PENDING while this state is ``waiting`` preserves its home,
        # memory and recovery identity without spending provider calls on
        # empty polls.
        return "active" if _unjudged_backlog(state) > 0 else "waiting"


# LLM: Worker completion is one shared calculation for direct creation,
# runner timeout recovery and restart reconciliation. It uses only the durable
# window, close flag and acknowledged ledger, never generated completion text.
# 函数用途: 判断一条 watch 是否仍需来源工作者，防止窗口结束且清账后被重启误复活。
def _source_watch_incomplete(
    state: WatchState,
    *,
    now: float | None = None,
) -> bool:
    if state.closed:
        return False
    observed_at = time.time() if now is None else float(now)
    window = max(0, int(state.watch_window_seconds or 0))
    if window <= 0 or observed_at - float(state.opened_at or 0.0) < window:
        return True
    if float(state.window_finalized_at or 0.0) <= 0:
        return True
    return _unjudged_backlog(state) > 0


# LLM: Clear removes only the ephemeral consumer lease; raw input, verdicts and
# finding references remain durable for later audit.
# 函数用途: Audit clear/close 时撤销来源工作者租约，防止旧 attempt 继续签收。
def clear_source_worker_lease(owner_home: Path, watch_id: str) -> None:
    path = state_dir(Path(owner_home)) / f"{watch_id}.worker-lease.json"
    try:
        with locked_json_path(path):
            path.unlink(missing_ok=True)
    except OSError:
        return


# LLM: Every source-worker takeover records one typed reason on the canonical
# task and revokes its ephemeral watch lease. No prompt text or PID name is
# used to decide whether an old attempt may keep consuming.
# 函数用途: 统一记录来源工作者的轮换/卡死/崩溃恢复事实，并撤销旧消费租约。
def record_source_worker_recovery(
    task: object,
    *,
    reason: str,
    now: float | None = None,
) -> bool:
    attrs = dict(getattr(task, "attributes", {}) or {})
    if not structured_audit_supervised_worker_attributes(attrs):
        return False
    allowed_reasons = {
        "runner_process_died",
        "runner_session_stalled",
        "runner_session_ended",
        "runner_timeout",
        "slice_rotation",
        "provider_timeout",
        "provider_transient",
    }
    if reason not in allowed_reasons:
        raise ValueError(f"unknown Audit source recovery reason: {reason}")
    observed_at = time.time() if now is None else float(now)
    recovery = dict(attrs.get("audit_source_recovery") or {})
    recovery["schema_version"] = _RECOVERY_SCHEMA
    recovery["total_recoveries"] = int(recovery.get("total_recoveries") or 0) + 1
    counter_key = f"{reason}_count"
    recovery[counter_key] = int(recovery.get(counter_key) or 0) + 1
    recovery["last_reason"] = reason
    recovery["last_recovered_at"] = observed_at
    active_attempt = str(getattr(task, "runner_active_attempt_id", "") or "").strip()
    abandoned = list(getattr(task, "runner_abandoned_attempt_ids", None) or [])
    recovery["last_abandoned_attempt_id"] = active_attempt or str(
        abandoned[-1] if abandoned else ""
    )
    if reason in {"slice_rotation", "provider_timeout", "provider_transient"}:
        recovery["consecutive_stalls"] = 0
    else:
        recovery["consecutive_stalls"] = int(recovery.get("consecutive_stalls") or 0) + 1
    if reason in {
        "provider_timeout",
        "runner_timeout",
        "slice_rotation",
    } and structured_audit_source_worker_attributes(attrs):
        from .harvester import record_audit_batch_failure

        state = registry.get_or_load(
            Path(str(attrs.get(AUDIT_SOURCE_OWNER_HOME_ATTR) or "")),
            str(attrs.get(AUDIT_SOURCE_WATCH_ID_ATTR) or ""),
        )
        recovery["batch_backoff_persisted"] = bool(
            state is not None and record_audit_batch_failure(state, reason=reason)
        )
        recovery["batch_backoff_observed_at"] = observed_at
    attrs["audit_source_recovery"] = recovery
    task.attributes = attrs
    if structured_audit_source_worker_attributes(attrs):
        clear_source_worker_lease(
            Path(str(attrs.get(AUDIT_SOURCE_OWNER_HOME_ATTR) or "")),
            str(attrs.get(AUDIT_SOURCE_WATCH_ID_ATTR) or ""),
        )
    return True


# LLM: A successful bounded turn proves the logical source worker can still
# advance; reset only the consecutive stall streak while retaining lifetime
# recovery counters for operations and audit.
# 函数用途: 来源工作者正常完成一轮后清零连续卡死次数，但保留累计恢复历史。
def record_source_worker_progress(task: object, *, now: float | None = None) -> bool:
    attrs = dict(getattr(task, "attributes", {}) or {})
    if not structured_audit_source_worker_attributes(attrs):
        return False
    observed_at = time.time() if now is None else float(now)
    recovery = dict(attrs.get("audit_source_recovery") or {})
    recovery["schema_version"] = _RECOVERY_SCHEMA
    recovery["consecutive_stalls"] = 0
    recovery["last_progress_at"] = observed_at
    recovery["consecutive_provider_failures"] = 0
    recovery["provider_retry_not_before"] = 0.0
    recovery["provider_retry_delay_seconds"] = 0.0
    attrs["audit_source_recovery"] = recovery
    task.attributes = attrs
    return True


def _validate_current_worker_task(
    agent: object,
    *,
    run_id: str,
    attempt_id: str,
    attrs: dict[str, Any],
) -> dict[str, object] | None:
    manager = getattr(agent, "subagents", None)
    if manager is None or not callable(getattr(manager, "load", None)):
        return _denied(
            "AUDIT_SOURCE_TASK_STATE_UNAVAILABLE",
            "来源子代理权威任务状态不可用。",
        )
    try:
        task = manager.load(run_id)
    except Exception:
        return _denied(
            "AUDIT_SOURCE_TASK_STATE_UNAVAILABLE",
            "当前来源子代理任务记录不可读取。",
        )
    task_attrs = getattr(task, "attributes", {}) or {}
    keys = (
        CONVERSATION_REQUEST_ID_ATTR,
        AUDIT_RUN_EPOCH_ATTR,
        AUDIT_SOURCE_ID_ATTR,
        AUDIT_SOURCE_WATCH_ID_ATTR,
        AUDIT_SOURCE_WORKER_KEY_ATTR,
        AUDIT_SOURCE_OWNER_HOME_ATTR,
        AUDIT_SOURCE_PROFILE_REF_ATTR,
        AUDIT_SOURCE_CONFIG_VERSION_ATTR,
        "work_scope_key",
    )
    if not isinstance(task_attrs, dict) or any(
        str(task_attrs.get(key) or "").strip() != str(attrs.get(key) or "").strip() for key in keys
    ):
        return _denied(
            "AUDIT_SOURCE_TASK_BINDING_MISMATCH",
            "当前 runner 与持久任务中的来源绑定不一致。",
        )
    if task_attrs.get(AUDIT_SOURCE_DOCUMENT_REFS_ATTR) != attrs.get(
        AUDIT_SOURCE_DOCUMENT_REFS_ATTR
    ):
        return _denied(
            "AUDIT_SOURCE_TASK_BINDING_MISMATCH",
            "当前 runner 与持久任务中的来源文档绑定不一致。",
        )
    if str(getattr(task, "status", "") or "").upper() != TaskStatus.RUNNING.value:
        return _denied(
            "AUDIT_SOURCE_TASK_NOT_RUNNING",
            "来源子代理任务当前不是 RUNNING。",
        )
    if str(getattr(task, "runner_active_attempt_id", "") or "").strip() != attempt_id:
        return _denied(
            "AUDIT_SOURCE_ATTEMPT_STALE",
            "当前来源子代理 attempt 已失效。",
        )
    return None


def _acquire_or_renew_lease(
    agent: object,
    state: WatchState,
    *,
    attrs: dict[str, Any],
    worker_key: str,
    run_id: str,
    attempt_id: str,
    now: float,
) -> dict[str, object]:
    path = _lease_path(state)
    ttl = _lease_seconds(agent)
    try:
        with locked_json_path(path):
            # The first task check happens before the lease lock so callers
            # fail fast.  Repeat it while holding the lock to close the
            # takeover race: an old attempt may be fenced after that first
            # check but before it writes the lease.  Without this second check
            # the stale attempt could recreate its lease after recovery and
            # unnecessarily block the replacement until TTL expiry.
            task_error = _validate_current_worker_task(
                agent,
                run_id=run_id,
                attempt_id=attempt_id,
                attrs=attrs,
            )
            if task_error is not None:
                return task_error
            report = read_json_object_report(path, context="audit_source_worker.lease")
            if report.load_error is not None:
                return _denied(
                    "AUDIT_SOURCE_LEASE_UNAVAILABLE",
                    "来源工作者租约损坏，已按 fail-closed 拒绝消费。",
                )
            previous = report.payload
            fresh = _float(previous.get("expires_at")) > now
            same_attempt = (
                str(previous.get("worker_key") or "") == worker_key
                and str(previous.get("run_id") or "") == run_id
                and str(previous.get("attempt_id") or "") == attempt_id
                and _normalized_run_epoch(previous.get("audit_run_epoch"))
                == _normalized_run_epoch(state.audit_run_epoch)
            )
            if fresh and not same_attempt:
                return _denied(
                    "AUDIT_SOURCE_LEASE_HELD",
                    "这条来源已有另一个有效 runner attempt 持有消费租约。",
                )
            epoch = max(1, int(previous.get("epoch") or 0) + (0 if same_attempt else 1))
            payload = {
                "schema_version": _LEASE_SCHEMA,
                "audit_id": state.audit_root_task_id,
                "audit_run_epoch": _normalized_run_epoch(
                    state.audit_run_epoch
                ),
                "source_id": state.source_id,
                "watch_id": state.watch_id,
                "worker_key": worker_key,
                "run_id": run_id,
                "attempt_id": attempt_id,
                "epoch": epoch,
                "heartbeat_at": now,
                "expires_at": now + ttl,
            }
            write_json_file_atomic_unlocked(path, payload)
    except OSError:
        return _denied(
            "AUDIT_SOURCE_LEASE_UNAVAILABLE",
            "来源工作者租约无法持久化，已按 fail-closed 拒绝消费。",
        )
    return {
        "ok": True,
        "error_code": "",
        "audit_id": state.audit_root_task_id,
        "audit_run_epoch": _normalized_run_epoch(state.audit_run_epoch),
        "source_id": state.source_id,
        "watch_id": state.watch_id,
        "worker_key": worker_key,
        "run_id": run_id,
        "attempt_id": attempt_id,
        "lease_epoch": epoch,
        "lease_expires_at": now + ttl,
    }


def _source_worker_create_params(
    agent: object,
    state: WatchState,
    worker_key: str,
) -> dict[str, object]:
    attrs = _source_worker_attributes(agent, state, worker_key)
    task_workspace = _audit_task_workspace(agent, state)
    if task_workspace:
        attrs["run_workspace"] = {
            "task_root": task_workspace,
            "work_dir": str(Path(task_workspace) / "work"),
            "output_dir": str(Path(task_workspace) / "output"),
        }
    thread_id = _thread_id_for_task(agent, state.audit_root_task_id)
    if thread_id:
        attrs["conversation_thread_id"] = thread_id
    remaining = _watch_window_remaining(state)
    params: dict[str, object] = {
        "goal": _source_worker_goal(state),
        "agent_name": f"audit-source-{state.watch_id}",
        "role": "worker",
        "parent_id": state.audit_root_task_id,
        "root_id": state.audit_root_task_id,
        "allowed_tools": list(audit_worker_tool_scope(attrs)),
        "_exact_allowed_tools": True,
        "context_manifest": {
            "required_read_paths": _source_worker_required_read_paths(state),
        },
        # The Audit objective may mention every sibling source document.
        # Exact source refs above are authoritative; goal-derived path hints
        # would broaden only what the model sees, not what this worker owns.
        "_include_goal_file_hints": False,
        "attributes": attrs,
        "long_running": True,
    }
    if remaining > 0:
        params["service_window_seconds"] = remaining
    return params


def _source_worker_attributes(
    agent: object,
    state: WatchState,
    worker_key: str,
) -> dict[str, object]:
    return {
        AUDIT_ATTR: True,
        # Keep exact chronological prepare text in the named Audit task.  A
        # source worker receives only the current operational projection plus
        # its own pinned profile/document refs, never every sibling prepare.
        AUDIT_OBJECTIVE_ATTR: audit_runtime_requirement_text(state.audit_objective),
        AUDIT_RUN_PROMPT_ATTR: str(state.audit_run_prompt or ""),
        AUDIT_SOURCE_WORKER_ATTR: True,
        AUDIT_SOURCE_ID_ATTR: state.source_id,
        AUDIT_SOURCE_WATCH_ID_ATTR: state.watch_id,
        AUDIT_SOURCE_WORKER_KEY_ATTR: worker_key,
        AUDIT_SOURCE_OWNER_HOME_ATTR: str(state.owner_home),
        AUDIT_SOURCE_PROFILE_REF_ATTR: state.source_profile_ref,
        AUDIT_SOURCE_DOCUMENT_REFS_ATTR: list(state.document_refs),
        AUDIT_SOURCE_CONFIG_VERSION_ATTR: state.source_config_version,
        AUDIT_RUN_EPOCH_ATTR: max(0, int(state.audit_run_epoch or 0)),
        CONVERSATION_REQUEST_ID_ATTR: state.audit_root_task_id,
        "conversation_task_id": state.audit_root_task_id,
        "work_scope_key": audit_source_work_scope_key(
            worker_key,
            max(0, int(state.audit_run_epoch or 0)),
        ),
        "long_running": True,
        # 来源工作者是长期逻辑岗位，但单次 runner 只占一个有界工作片。
        # 卡在模型/工具调用时由统一 runner timeout 废弃 attempt，再沿同一账本续派。
        "dynamic_timeout_seconds": audit_worker_slice_seconds(agent),
    }


# LLM: If an Audit child opens its first source, bind that existing run instead
# of creating a second sibling. The decision uses only typed lineage and task state.
# 函数用途: 把“先派五个子代理再各自开源”收敛成恰好五个来源 worker，避免套娃和双重模型消耗。
def _adopt_current_audit_subagent(
    agent: object,
    state: WatchState,
    worker_key: str,
) -> dict[str, object] | None:
    from ..agent_core.runner.context import (
        current_subagent_run_id,
        current_task_attributes,
    )

    run_id = current_subagent_run_id(agent)
    runtime_attrs = current_task_attributes(agent)
    if not run_id or not isinstance(runtime_attrs, dict):
        return None
    if structured_audit_source_worker_attributes(runtime_attrs):
        return None
    if not structured_audit_source_binding_attributes(runtime_attrs):
        return None
    if str(runtime_attrs.get(CONVERSATION_REQUEST_ID_ATTR) or "") != state.audit_root_task_id:
        return None
    manager = getattr(agent, "subagents", None)
    if manager is None or not callable(getattr(manager, "load", None)):
        return None
    try:
        task = manager.load(run_id)
    except Exception:
        return None
    if (
        str(getattr(task, "root_id", "") or "") != state.audit_root_task_id
        or str(getattr(task, "status", "") or "").upper() != TaskStatus.RUNNING.value
        or list(getattr(task, "child_ids", None) or [])
    ):
        return None
    task_attrs = dict(getattr(task, "attributes", {}) or {})
    if structured_audit_source_worker_attributes(task_attrs):
        return None
    if not structured_audit_source_binding_attributes(task_attrs):
        return None
    binding = _source_worker_attributes(agent, state, worker_key)
    binding["audit_source_adopted_at"] = time.time()
    active_attempt_id = str(getattr(task, "runner_active_attempt_id", "") or "").strip()
    if active_attempt_id:
        binding[AUDIT_SOURCE_CONTEXT_REFRESH_ATTEMPT_ATTR] = active_attempt_id
    task_attrs.update(binding)
    task_attrs.pop(AUDIT_SOURCE_BINDING_PENDING_ATTR, None)
    task_attrs.pop(AUDIT_SOURCE_OPEN_ATTR, None)
    task_attrs.pop("output_files", None)
    task_attrs.pop("output_refs", None)
    task_attrs.pop("system_default_output_ref", None)
    task.attributes = task_attrs
    task.allowed_tools = list(audit_worker_tool_scope(task_attrs))
    task.allowed_skills = []
    task.allowed_write_roots = []
    task.role = "worker"
    task.agent_name = f"audit-source-{state.watch_id}"
    task.acceptance_checks = []
    # Before adoption this child may have inherited parent read previews and
    # sibling-source material. The focused goal already carries the Audit
    # objective, so those generic packs are both redundant and out of scope.
    task.context_packs = []
    required_reads = _source_worker_required_read_paths(state)
    context_manifest = getattr(task, "context_manifest", None)
    if context_manifest is not None:
        context_manifest.required_read_paths = required_reads
        context_manifest.hint_read_paths = []
    task.updated_at = time.time()
    manager.save(task)

    # Keep the current model turn on its immutable pre-binding snapshot.  The
    # durable task above is now the bound worker, but this turn was prompted and
    # schema-scoped only to open one source.  Expanding ``runtime_attrs`` here
    # used to let the same stale turn pull and judge records before a refreshed
    # source-worker prompt (including the current run prompt) was built.  The
    # binding gate therefore remains active until this bounded slice ends; the
    # canonical continuation reloads the persisted worker attributes.
    return {
        "ok": True,
        "state": "active",
        "run_id": run_id,
        "task_status": TaskStatus.RUNNING.value,
        "worker_key": worker_key,
        "created": False,
        "adopted": True,
        "context_refresh_required": True,
    }


# LLM: Recovery resolves the parent Audit workspace from the canonical
# conversation task link; it never derives a path from a task name or prompt.
# 中文说明：后台接替必须从会话任务绑定读取原 Audit 的权威目录，不能根据任务名、
# 请求正文或临时运行目录猜测路径。
def _audit_task_workspace(agent: object, state: WatchState) -> str:
    store = getattr(agent, "conversation_store", None)
    if store is None:
        return ""
    thread_for_task = getattr(store, "thread_for_task", None)
    task_links = getattr(store, "task_links", None)
    if not callable(thread_for_task) or not callable(task_links):
        return ""
    try:
        thread = thread_for_task(state.audit_root_task_id)
        if thread is None:
            return ""
        link = next(
            (
                item
                for item in task_links(str(getattr(thread, "thread_id", "") or ""))
                if str(getattr(item, "task_id", "") or "") == state.audit_root_task_id
            ),
            None,
        )
        raw = str(getattr(link, "task_path", "") or "").strip()
        if not raw:
            return ""
        from ..conversation.workspace_paths import validated_durable_work_path

        root = validated_durable_work_path(
            state.owner_home,
            raw,
            "audit",
            require_directory=True,
        )
        return str(root)
    except (OSError, RuntimeError, ValueError):
        return ""


# LLM: Persisted workers from older releases may point at a service workspace.
# Move only idle lifecycle records; a live runner keeps its immutable workspace
# until the normal lease/timeout supervisor makes it idle.
# 中文说明：升级恢复只迁移未运行的旧来源工作者；运行中的进程先按统一租约和超时
# 机制收回，避免边执行边改权威目录导致两处状态互相覆盖。
def _reconcile_source_worker_workspace(
    agent: object,
    state: WatchState,
    task: object,
) -> str:
    task_root = _audit_task_workspace(agent, state)
    if not task_root or _source_worker_uses_task_root(task, task_root):
        return "current"
    status = str(getattr(task, "status", "") or "").strip().upper()
    if status == TaskStatus.RUNNING.value:
        return "waiting_for_idle"
    if status not in {
        TaskStatus.PLANNING.value,
        TaskStatus.PENDING.value,
        TaskStatus.BLOCKED.value,
        TaskStatus.DONE.value,
    }:
        return "not_migratable"
    attrs = dict(getattr(task, "attributes", {}) or {})
    root = Path(task_root)
    attrs["run_workspace"] = {
        "task_root": str(root),
        "work_dir": str(root / "work"),
        "output_dir": str(root / "output"),
    }
    task.attributes = attrs
    task.updated_at = time.time()
    agent.subagents.save(task)
    return "migrated"


def _source_worker_uses_task_root(task: object, task_root: str) -> bool:
    attrs = getattr(task, "attributes", {}) or {}
    run_workspace = attrs.get("run_workspace") if isinstance(attrs, dict) else None
    raw = (
        str(run_workspace.get("task_root") or "").strip() if isinstance(run_workspace, dict) else ""
    )
    if not raw:
        raw = str(getattr(task, "task_workspace_dir", "") or "").strip()
    if not raw:
        return False
    try:
        return Path(raw).expanduser().resolve(strict=False) == Path(task_root).expanduser().resolve(
            strict=False
        )
    except (OSError, RuntimeError):
        return False


# LLM: Only local file references become filesystem grants; URI/artifact refs
# keep using their canonical tools and a version fragment never becomes part of
# an on-disk filename.
# 函数用途: 从当前来源的固定引用中提取可交给统一 read boundary 的本地文件路径。
def _source_worker_required_read_paths(state: WatchState) -> list[str]:
    paths: list[str] = []
    for raw in [state.source_profile_ref, *state.document_refs]:
        text = str(raw or "").strip()
        if not text or "://" in text:
            continue
        path = text.split("#", 1)[0].strip()
        if path and path not in paths:
            paths.append(path)
    return paths


def _source_worker_goal(state: WatchState) -> str:
    source_goal = str(state.source_task_goal or "").strip()
    refs = [
        ref for ref in [state.source_profile_ref, *state.document_refs] if str(ref or "").strip()
    ]
    ref_note = (
        "\n本来源可按需读取的说明/文档引用：" + json.dumps(refs, ensure_ascii=False) if refs else ""
    )
    return (
        "作为当前 Audit 中这一个数据源的专属来源工作者持续工作。"
        f"结构化 source_id={state.source_id}、watch_id={state.watch_id} 已由系统绑定；"
        "只通过 watch_stream 消费它，"
        "对每个收到的完整记录提交一次首次 verdict，并保留 ack_id/source_ref 对应关系。"
        "按本次用户目标自主研判；首次判断中值得升级的记录，把 finding 随同该条 verdict "
        "一次提交，使判断与发现共同落账；后续补证、复核或组合多条记录形成的新结论，才使用 "
        "record_finding 更新可追溯 finding。"
        "如果后续发现某条已签收 verdict 本身判错，先用 watch_stream verdict "
        "review=true 对准确原记录追加更正；需要升级时可与 finding 同行落账。"
        "不要试图用 record_finding 改变旧 verdict。"
        "不要直接替主代理面向用户汇报，也不要打开、关闭或消费其他来源。"
        "持久 verdict 和 finding 就是本岗位交付，不创建额外报告文件，"
        "也不为写报告申请新工具。"
        "在采集窗口关闭且待判积压清零前保持可续跑。"
        + ref_note
        + (f"\n本来源任务说明：{source_goal}" if source_goal else "")
    )


# LLM: Source-specific task prose is read only from the current typed child
# run.  The Audit root prompt and source URL are never used to synthesize it.
# 函数用途: 首次 open 时把协调代理交给这个叶子子代理的原始目标钉进 watch，供崩溃补岗复用。
def current_audit_source_task_goal(agent: object) -> str:
    from ..agent_core.runner.context import (
        current_subagent_run_id,
        current_task_attributes,
    )
    from ..common.audit_activation import (
        structured_audit_source_binding_attributes,
        structured_audit_source_worker_attributes,
    )

    attrs = current_task_attributes(agent)
    if not (
        structured_audit_source_binding_attributes(attrs)
        or structured_audit_source_worker_attributes(attrs)
    ):
        return ""
    run_id = current_subagent_run_id(agent)
    manager = getattr(agent, "subagents", None)
    if not run_id or manager is None or not callable(getattr(manager, "load", None)):
        return ""
    try:
        task = manager.load(run_id)
    except Exception:
        return ""
    return str(getattr(task, "goal", "") or "").strip()


def _cancel_closed_source_worker(
    agent: object,
    state: WatchState,
    *,
    reason: str = "audit_parent_inactive",
) -> bool:
    worker_key = audit_source_worker_key(
        state.audit_root_task_id,
        state.watch_id,
    )
    tasks = [
        task
        for task in _matching_worker_tasks(agent, worker_key)
        if task_status_in(getattr(task, "status", ""), _ACTIVE_WORKER_STATUSES)
    ]
    if not tasks:
        return False
    from ..agent_core.orchestration.tools.cancel import (
        CancelSubagentTaskRequest,
        cancel_subagent_task,
    )

    cancelled = False
    for task in tasks:
        try:
            cancel_subagent_task(
                agent,
                CancelSubagentTaskRequest(
                    task=task,
                    reason=reason,
                    source="audit_source_worker_reconcile",
                ),
            )
        except Exception:
            continue
        cancelled = True
    return cancelled


def _matching_worker_task(
    agent: object | None,
    worker_key: str,
    *,
    run_epoch: object | None = None,
) -> object | None:
    task, _retired = _canonical_worker_task(
        agent,
        worker_key,
        retire_duplicates=False,
        run_epoch=run_epoch,
    )
    return task


def _matching_worker_tasks(
    agent: object | None,
    worker_key: str,
    *,
    run_epoch: object | None = None,
) -> list[object]:
    manager = getattr(agent, "subagents", None) if agent is not None else None
    if manager is None or not callable(getattr(manager, "list_runs", None)):
        return []
    try:
        tasks = manager.list_runs()
    except Exception:
        return []
    matching = []
    selected_epoch = (
        None if run_epoch is None else _normalized_run_epoch(run_epoch)
    )
    for task in tasks:
        attrs = getattr(task, "attributes", {}) or {}
        if (
            isinstance(attrs, dict)
            and structured_audit_source_worker_attributes(attrs)
            and str(attrs.get(AUDIT_SOURCE_WORKER_KEY_ATTR) or "") == worker_key
            and (
                selected_epoch is None
                or _normalized_run_epoch(attrs.get(AUDIT_RUN_EPOCH_ATTR))
                == selected_epoch
            )
        ):
            matching.append(task)
    if not matching:
        return []
    return matching


def _canonical_worker_task(
    agent: object | None,
    worker_key: str,
    *,
    retire_duplicates: bool,
    run_epoch: object | None = None,
) -> tuple[object | None, int]:
    matching = _matching_worker_tasks(
        agent,
        worker_key,
        run_epoch=run_epoch,
    )
    if not matching:
        return None, 0
    canonical = min(matching, key=_worker_selection_key)
    if not retire_duplicates or agent is None:
        return canonical, 0
    from ..agent_core.orchestration.tools.cancel import (
        CancelSubagentTaskRequest,
        cancel_subagent_task,
    )

    retired = 0
    for task in matching:
        if str(getattr(task, "id", "") or "") == str(getattr(canonical, "id", "") or ""):
            continue
        if not task_status_in(getattr(task, "status", ""), _ACTIVE_WORKER_STATUSES):
            continue
        try:
            cancel_subagent_task(
                agent,
                CancelSubagentTaskRequest(
                    task=task,
                    reason="duplicate_audit_source_worker",
                    source="audit_source_worker_reconcile",
                ),
            )
        except Exception:
            continue
        retired += 1
    return canonical, retired


def _worker_selection_key(task: object) -> tuple[object, ...]:
    from ..subagents.runner_session_liveness import has_fresh_runner_session

    status = str(getattr(task, "status", "") or "").upper()
    active_rank = 0 if status in _ACTIVE_WORKER_STATUSES else 1
    try:
        fresh_rank = 0 if has_fresh_runner_session(task) else 1
    except (AttributeError, OSError, TypeError, ValueError):
        fresh_rank = 1
    status_rank = {
        TaskStatus.RUNNING.value: 0,
        TaskStatus.PENDING.value: 1,
        TaskStatus.PLANNING.value: 2,
        TaskStatus.BLOCKED.value: 3,
        TaskStatus.PAUSED.value: 4,
        TaskStatus.DONE.value: 5,
    }.get(status, 6)
    progress_at = max(
        float(getattr(task, "last_progress_at", 0.0) or 0.0),
        float(getattr(task, "updated_at", 0.0) or 0.0),
    )
    created_at = float(getattr(task, "created_at", 0.0) or 0.0)
    return (
        active_rank,
        fresh_rank,
        status_rank,
        -progress_at,
        created_at,
        str(getattr(task, "id", "") or ""),
    )


def _requeue_completed_worker(agent: object, task: object) -> None:
    task.status = TaskStatus.PENDING.value
    task.verification_status = VerificationStatus.UNVERIFIED.value
    task.failure_type = "incomplete_deliverables"
    task.blockers = []
    task.ended_at = 0.0
    task.updated_at = time.time()
    agent.subagents.save(task)


# LLM: A named Audit source is a durable logical position, not one model turn.
# Generic batch-closeout failures resume immediately. Typed provider supply
# failures retain the same run/workspace but wait on a persisted exponential
# backoff. Permission, configuration and tool failures remain BLOCKED.
# 函数用途：决定 BLOCKED 来源工作者是立即续派、等待供应恢复，还是保持真实阻塞。
def _blocked_source_worker_recovery(
    agent: object,
    task: object,
    state: WatchState,
    *,
    now: float | None = None,
) -> dict[str, object]:
    if not _source_watch_incomplete(state):
        return {"action": "none"}
    failure_type = str(getattr(task, "failure_type", "") or "").strip()
    if failure_type in {
        FailureType.INCOMPLETE_DELIVERABLES.value,
        FailureType.STATUS_BLOCKED.value,
        FailureType.STRUCTURED_OUTPUT_PARSE_ERROR.value,
    }:
        return {
            "action": "requeue",
            "reason": failure_type,
            "pending_failure_type": FailureType.INCOMPLETE_DELIVERABLES.value,
        }
    if failure_type not in PROVIDER_SUPPLY_FAILURE_TYPES:
        return {"action": "none"}
    observed_at = time.time() if now is None else float(now)
    retry = _record_provider_supply_backoff(
        agent,
        task,
        failure_type=failure_type,
        now=observed_at,
    )
    retry_not_before = float(retry.get("retry_not_before") or 0.0)
    if observed_at < retry_not_before:
        return {
            "action": "wait",
            "reason": failure_type,
            **retry,
        }
    return {
        "action": "requeue",
        "reason": failure_type,
        "pending_failure_type": "",
        **retry,
    }


# 函数用途：对同一个 provider 失败 attempt 只记一次持久退避，重启后继续按原截止时间恢复。
def _record_provider_supply_backoff(
    agent: object,
    task: object,
    *,
    failure_type: str,
    now: float,
) -> dict[str, object]:
    attrs = dict(getattr(task, "attributes", {}) or {})
    recovery = dict(attrs.get("audit_source_recovery") or {})
    attempt_key = ":".join(
        (
            failure_type,
            str(max(0, int(getattr(task, "runner_attempts", 0) or 0))),
            f"{float(getattr(task, 'runner_last_attempt_at', 0.0) or 0.0):.6f}",
        )
    )
    if str(recovery.get("provider_failure_attempt_key") or "") != attempt_key:
        reason = (
            "provider_timeout"
            if failure_type == FailureType.PROVIDER_TIMEOUT.value
            else "provider_transient"
        )
        record_source_worker_recovery(task, reason=reason, now=now)
        attrs = dict(getattr(task, "attributes", {}) or {})
        recovery = dict(attrs.get("audit_source_recovery") or {})
        streak = int(recovery.get("consecutive_provider_failures") or 0) + 1
        guard_policy = getattr(agent, "runtime_guard_policy", None)
        base_seconds = runtime_guard_int(
            "provider_supply_backoff_base_seconds",
            30,
            policy=guard_policy,
        )
        max_seconds = runtime_guard_int(
            "provider_supply_backoff_max_seconds",
            900,
            policy=guard_policy,
        )
        delay = jittered_backoff(
            streak,
            base_delay=max(1.0, float(base_seconds)),
            max_delay=max(float(base_seconds), float(max_seconds)),
            jitter_ratio=0.1,
        )
        recovery.update(
            {
                "provider_failure_attempt_key": attempt_key,
                "provider_last_failure_type": failure_type,
                "provider_last_failure_at": now,
                "consecutive_provider_failures": streak,
                "provider_retry_delay_seconds": delay,
                "provider_retry_not_before": now + delay,
            }
        )
        attrs["audit_source_recovery"] = recovery
        task.attributes = attrs
        task.updated_at = now
        agent.subagents.save(task)
    return {
        "retry_delay_seconds": float(recovery.get("provider_retry_delay_seconds") or 0.0),
        "retry_not_before": float(recovery.get("provider_retry_not_before") or 0.0),
        "consecutive_failures": int(recovery.get("consecutive_provider_failures") or 0),
    }


def _requeue_blocked_source_worker(
    agent: object,
    task: object,
    *,
    pending_failure_type: str = FailureType.INCOMPLETE_DELIVERABLES.value,
) -> None:
    from ..subagents.process_control import reclaim_background_start

    for request in getattr(task, "capability_requests", []) or []:
        if capability_request_counts_as_open(getattr(request, "status", "OPEN")):
            request.status = "CLOSED"
    task.status = TaskStatus.PENDING.value
    task.verification_status = VerificationStatus.UNVERIFIED.value
    task.failure_type = pending_failure_type
    task.blockers = []
    task.runner_last_error = ""
    task.ended_at = 0.0
    task.updated_at = time.time()
    reclaim_background_start(task)
    agent.subagents.save(task)


def resume_quota_blocked_source_workers(agent: object, audit_id: str) -> int:
    """Explicitly requeue quota-blocked workers for one exact active Audit.

    This is intentionally not called by periodic supervision.  The operator
    first restores quota or switches the configured model, then invokes the
    typed Audit resume command.  The existing run IDs, workspaces, cursors and
    unjudged spool stay unchanged.
    """
    selected_audit_id = str(audit_id or "").strip()
    manager = getattr(agent, "subagents", None)
    if not selected_audit_id or manager is None:
        return 0
    report = manager.list_runs_report()
    if list(getattr(report, "load_errors", []) or []):
        raise RuntimeError("subagent state unavailable")
    resumed = 0
    for task in list(getattr(report, "runs", []) or []):
        attrs = dict(getattr(task, "attributes", {}) or {})
        if (
            not structured_audit_supervised_worker_attributes(attrs)
            or str(attrs.get(CONVERSATION_REQUEST_ID_ATTR) or "").strip() != selected_audit_id
            or str(getattr(task, "status", "") or "").upper() != TaskStatus.BLOCKED.value
            or str(getattr(task, "failure_type", "") or "").strip()
            != FailureType.PROVIDER_QUOTA_EXHAUSTED.value
        ):
            continue
        recovery = dict(attrs.get("audit_source_recovery") or {})
        recovery["schema_version"] = _RECOVERY_SCHEMA
        recovery["operator_resume_count"] = int(recovery.get("operator_resume_count") or 0) + 1
        recovery["last_operator_resume_at"] = time.time()
        attrs["audit_source_recovery"] = recovery
        task.attributes = attrs
        _requeue_blocked_source_worker(
            agent,
            task,
            pending_failure_type=FailureType.INCOMPLETE_DELIVERABLES.value,
        )
        resumed += 1
    return resumed


def _worker_state(task: object | None, lease_fresh: bool) -> str:
    if task is None:
        return "missing"
    status = str(getattr(task, "status", "") or "").upper()
    if status == TaskStatus.RUNNING.value and lease_fresh:
        return "active"
    if status in _ACTIVE_WORKER_STATUSES:
        return "waiting_for_worker"
    return status.lower() or "unknown"


def _source_worker_recovery(task: object | None) -> dict[str, object]:
    if task is None:
        return {}
    attrs = getattr(task, "attributes", {}) or {}
    if not isinstance(attrs, dict):
        return {}
    recovery = attrs.get("audit_source_recovery")
    return dict(recovery) if isinstance(recovery, dict) else {}


def _lease_path(state: WatchState) -> Path:
    return state_dir(state.owner_home) / f"{state.watch_id}.worker-lease.json"


def _read_lease(path: Path) -> dict[str, Any]:
    report = read_json_object_report(path, context="audit_source_worker.lease_status")
    return report.payload if report.load_error is None else {}


def _lease_seconds(agent: object) -> int:
    raw = getattr(getattr(agent, "config", None), "lease_stale_without_heartbeat_seconds", 300)
    try:
        return max(30, int(raw or 300))
    except (TypeError, ValueError):
        return 300


def _watch_window_remaining(state: WatchState) -> int:
    window = max(0, int(state.watch_window_seconds or 0))
    if window <= 0:
        return 0
    return max(1, int(state.opened_at + window - time.time()))


def _unjudged_backlog(state: WatchState) -> int:
    try:
        from .harvester import consumed_and_acked_on_disk

        written = int(state.totals.get("spool_candidates") or 0)
        _consumed, acked = consumed_and_acked_on_disk(
            state.owner_home,
            state.watch_id,
        )
        return max(0, written - acked)
    except Exception:
        return 1


def _thread_id_for_task(agent: object, task_id: str) -> str:
    store = getattr(agent, "conversation_store", None)
    if store is None or not callable(getattr(store, "thread_for_task", None)):
        return ""
    try:
        thread = store.thread_for_task(task_id)
    except Exception:
        return ""
    return str(getattr(thread, "thread_id", "") or getattr(thread, "id", "") or "").strip()


def _agent_owner_home(agent: object) -> Path | None:
    raw = getattr(getattr(agent, "home_paths", None), "owner_home_dir", "")
    if not isinstance(raw, str | Path) or not str(raw).strip():
        return None
    return Path(raw)


def _tool_payload(result: object) -> dict[str, Any]:
    try:
        payload = json.loads(str(getattr(result, "output", "") or ""))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _float(value: object) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _denied(error_code: str, error: str) -> dict[str, object]:
    return {"ok": False, "error_code": error_code, "error": error}


__all__ = [
    "audit_parent_reconcile_state",
    "audit_task_has_unreported_findings",
    "authorize_source_worker_action",
    "current_audit_source_task_goal",
    "source_worker_lease_fence",
    "clear_source_worker_lease",
    "ensure_audit_source_worker",
    "provision_published_audit_source_workers",
    "reconcile_audit_finding_outbox",
    "reconcile_audit_capacity_alert",
    "record_source_worker_progress",
    "record_source_worker_recovery",
    "reconcile_audit_source_workers",
    "retire_published_audit_sources",
    "settle_audit_source_worker",
    "source_worker_facts",
    "resume_quota_blocked_source_workers",
    "source_worker_lifecycle_state",
    "source_worker_task_incomplete",
    "wake_audit_source_worker",
]
