"""Bind ordinary delegated work to one durable Audit finding.

This is a relationship adapter over the existing subagent lifecycle.  It does
not choose whether to investigate, prescribe investigation steps, or create a
second Audit workflow.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from ...conversation.authority import CONVERSATION_REQUEST_ID_ATTR
from ...subagents.models import TaskStatus, task_status_in

AUDIT_FINDING_RELATION_ATTR = "audit_finding_relation"
AUDIT_FINDING_RELATION_SCHEMA = "audit-finding-investigation.v1"
_FINDING_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_ACTIVE_TASK_STATUSES = frozenset(
    {
        TaskStatus.PLANNING.value,
        TaskStatus.PENDING.value,
        TaskStatus.RUNNING.value,
        TaskStatus.BLOCKED.value,
        TaskStatus.PAUSED.value,
    }
)


# LLM: A model may decide to delegate an investigation, but the program links
# it only to a finding event already persisted in this exact owner/thread and
# to an active named Audit.  Goal prose cannot manufacture that authority.
# 函数用途: 把模型选择的 finding_id 校验成真实会话事实，并写入子代理的结构化血缘。
def prepare_audit_finding_relation(
    agent: object,
    params: dict[str, object],
    *,
    goal: str,
) -> tuple[dict[str, object], str]:
    attrs = _attributes(params)
    if AUDIT_FINDING_RELATION_ATTR in attrs:
        return params, "audit_finding_relation 是系统保留字段，不能由调用者直接提供。"
    finding_id = str(params.get("related_finding_id") or "").strip()
    if not finding_id:
        return params, ""
    if _FINDING_ID_PATTERN.fullmatch(finding_id) is None:
        return params, "related_finding_id 格式无效。"
    thread_id = _current_thread_id(agent)
    if not thread_id:
        return params, "related_finding_id 需要当前持久会话上下文。"
    relation, error = _resolve_relation(agent, thread_id, finding_id)
    if error:
        return params, error
    audit_id = str(relation["audit_id"])
    if _conflicting_lineage(params, audit_id):
        return params, "related_finding_id 与显式 parent_id/root_id 不属于同一 Audit。"
    updated = dict(params)
    updated_attrs = attrs
    updated_attrs[AUDIT_FINDING_RELATION_ATTR] = relation
    updated_attrs[CONVERSATION_REQUEST_ID_ATTR] = audit_id
    updated_attrs["conversation_thread_id"] = thread_id
    updated_attrs["conversation_task_id"] = audit_id
    updated_attrs["work_scope_key"] = _investigation_scope_key(
        relation,
        goal=goal,
        role=str(params.get("role") or "worker"),
    )
    updated["attributes"] = updated_attrs
    updated["parent_id"] = audit_id
    updated["root_id"] = audit_id
    updated["context_packs"] = _context_packs(params, relation)
    return updated, ""


# LLM: This validator is shared by create-time conflict policy, payload
# projection, and cancellation fencing, so all lifecycle stages recognize the
# same typed relation instead of trusting role names or prompt text.
# 函数用途: 判断任务属性中是否存在完整且自洽的 Audit finding 关系。
def structured_audit_finding_relation(attrs: object) -> dict[str, object]:
    if not isinstance(attrs, dict):
        return {}
    relation = attrs.get(AUDIT_FINDING_RELATION_ATTR)
    if not isinstance(relation, dict):
        return {}
    required = ("thread_id", "owner_id", "audit_id", "finding_id", "observation_id")
    if relation.get("schema_version") != AUDIT_FINDING_RELATION_SCHEMA:
        return {}
    if any(not str(relation.get(key) or "").strip() for key in required):
        return {}
    if _FINDING_ID_PATTERN.fullmatch(str(relation.get("finding_id") or "")) is None:
        return {}
    return dict(relation)


# LLM: Named clear may race a just-created investigation.  Re-check the exact
# active Audit after dispatch registration and cancel the canonical run if its
# durable parent was already cleared; no orphan may escape a stale snapshot.
# 函数用途: 创建调查后再次核对所属 Audit，竞态中已关闭的任务立即走统一取消链。
def fence_inactive_audit_investigations(
    agent: object,
    tasks: list[object],
) -> list[dict[str, object]]:
    cancelled: list[dict[str, object]] = []
    for task in tasks:
        relation = structured_audit_finding_relation(
            getattr(task, "attributes", None)
        )
        if not relation or _relation_is_active(agent, relation):
            continue
        run_id = str(getattr(task, "id", "") or "").strip()
        if run_id and task_status_in(getattr(task, "status", ""), _ACTIVE_TASK_STATUSES):
            _cancel_task(agent, task)
        cancelled.append(
            {
                "investigation_run_id": run_id,
                "finding_id": str(relation.get("finding_id") or ""),
                "audit_id": str(relation.get("audit_id") or ""),
                "reason": "audit_not_active_after_create",
            }
        )
    return cancelled


def finding_investigation_payload(
    tasks: list[object],
    schedule_lifecycle: object,
) -> list[dict[str, object]]:
    lifecycle = schedule_lifecycle if isinstance(schedule_lifecycle, dict) else {}
    accepted = _text_set(lifecycle.get("start_accepted_run_ids"))
    running = _text_set(lifecycle.get("running_run_ids"))
    failed = _text_set(lifecycle.get("failed_run_ids"))
    rows: list[dict[str, object]] = []
    for task in tasks:
        relation = structured_audit_finding_relation(
            getattr(task, "attributes", None)
        )
        if not relation:
            continue
        run_id = str(getattr(task, "id", "") or "").strip()
        task_status = str(getattr(task, "status", "") or "").strip().upper()
        rows.append(
            {
                "finding_id": str(relation.get("finding_id") or ""),
                "finding_revision": int(relation.get("revision") or 0),
                "audit_id": str(relation.get("audit_id") or ""),
                "investigation_run_id": run_id,
                "state": _investigation_state(
                    run_id,
                    task_status=task_status,
                    accepted=accepted,
                    running=running,
                    failed=failed,
                ),
                "task_status": task_status,
            }
        )
    return rows


def _resolve_relation(
    agent: object,
    thread_id: str,
    finding_id: str,
) -> tuple[dict[str, object], str]:
    store = getattr(agent, "conversation_store", None)
    if store is None:
        return {}, "会话事件账不可用，不能验证 related_finding_id。"
    try:
        observations, errors = store.observations.recent_report(
            thread_id,
            limit=0,
        )
    except Exception:
        return {}, "会话事件账不可读取，不能验证 related_finding_id。"
    if errors:
        return {}, "会话事件账存在读取错误，已按 fail-closed 拒绝创建调查。"
    matches = [
        event
        for event in observations
        if _matching_finding_event(event, finding_id)
    ]
    if not matches:
        return {}, "当前会话中不存在这个已持久化的 Audit finding。"
    matches.sort(
        key=lambda event: (
            int(getattr(event, "metadata", {}).get("revision") or 0),
            float(getattr(event, "observed_at", 0.0) or 0.0),
        ),
        reverse=True,
    )
    event = matches[0]
    metadata = dict(getattr(event, "metadata", {}) or {})
    owner_id = str(getattr(getattr(agent, "home_paths", None), "owner_id", "") or "")
    if not owner_id or str(metadata.get("owner_id") or "") != owner_id:
        return {}, "这个 Audit finding 不属于当前 owner。"
    audit_id = str(metadata.get("audit_id") or "").strip()
    if not _active_audit_link(store, thread_id, audit_id):
        return {}, "这个 Audit finding 所属的命名 Audit 已不在运行。"
    return {
        "schema_version": AUDIT_FINDING_RELATION_SCHEMA,
        "thread_id": thread_id,
        "owner_id": owner_id,
        "audit_id": audit_id,
        "finding_id": finding_id,
        "revision": max(1, int(metadata.get("revision") or 1)),
        "observation_id": str(getattr(event, "observation_id", "") or ""),
        "source_refs": [
            str(item)
            for item in getattr(event, "evidence_refs", ())
            if str(item or "").strip()
        ],
    }, ""


def _matching_finding_event(event: object, finding_id: str) -> bool:
    metadata = getattr(event, "metadata", None)
    return bool(
        str(getattr(event, "event_type", "") or "") == "audit_finding"
        and isinstance(metadata, dict)
        and metadata.get("schema_version") == "audit-finding-event.v1"
        and str(metadata.get("finding_id") or "") == finding_id
        and str(metadata.get("audit_id") or "").strip()
    )


def _active_audit_link(store: object, thread_id: str, audit_id: str) -> bool:
    try:
        links, errors = store.tasks.active_report(thread_id)
    except Exception:
        return False
    if errors:
        return False
    return any(
        str(getattr(link, "task_id", "") or "") == audit_id
        and str(getattr(link, "work_kind", "") or "").lower() == "audit"
        and str(getattr(link, "status", "") or "").lower() == "active"
        for link in links
    )


def _relation_is_active(agent: object, relation: dict[str, object]) -> bool:
    store = getattr(agent, "conversation_store", None)
    return bool(
        store is not None
        and _active_audit_link(
            store,
            str(relation.get("thread_id") or ""),
            str(relation.get("audit_id") or ""),
        )
    )


def _cancel_task(agent: object, task: object) -> None:
    try:
        from ...subagents.cancellation import CancelSubagentTaskRequest, cancel_subagent_task

        cancel_subagent_task(
            agent,
            CancelSubagentTaskRequest(
                task=task,
                reason="audit_cleared_during_investigation_create",
                source="audit_finding_relation",
            ),
        )
    except Exception:
        return


def _current_thread_id(agent: object) -> str:
    current = getattr(agent, "_current_run_params", None)
    attrs = getattr(current, "task_attributes", None) if current is not None else None
    thread_id = (
        str(attrs.get("conversation_thread_id") or "").strip()
        if isinstance(attrs, dict)
        else ""
    )
    if thread_id:
        return thread_id
    task_id = str(getattr(current, "task_id", "") or "").strip()
    store = getattr(agent, "conversation_store", None)
    if not task_id or store is None:
        return ""
    try:
        thread = store.tasks.thread_for(task_id)
    except Exception:
        return ""
    return str(getattr(thread, "thread_id", "") or "").strip()


def _attributes(params: dict[str, object]) -> dict[str, object]:
    value = params.get("attributes")
    return dict(value) if isinstance(value, dict) else {}


def _conflicting_lineage(params: dict[str, object], audit_id: str) -> bool:
    return any(
        value and value != audit_id
        for value in (
            str(params.get("parent_id") or "").strip(),
            str(params.get("root_id") or "").strip(),
        )
    )


def _context_packs(
    params: dict[str, object],
    relation: dict[str, object],
) -> list[dict[str, object]]:
    raw = params.get("context_packs")
    packs = (
        [dict(item) for item in raw if isinstance(item, dict)]
        if isinstance(raw, list)
        else ([dict(raw)] if isinstance(raw, dict) else [])
    )
    packs = [
        item
        for item in packs
        if str(item.get("kind") or "") != "audit_finding_relation"
    ]
    packs.append(
        {
            "kind": "audit_finding_relation",
            "role": "machine_facts",
            "summary": "This delegated run is linked to one persisted Audit finding.",
            "finding_id": relation["finding_id"],
            "finding_revision": relation["revision"],
            "audit_id": relation["audit_id"],
            "source_refs": list(relation.get("source_refs") or []),
        }
    )
    return packs


def _investigation_scope_key(
    relation: dict[str, object],
    *,
    goal: str,
    role: str,
) -> str:
    material = "\0".join(
        [
            str(relation.get("owner_id") or ""),
            str(relation.get("audit_id") or ""),
            str(relation.get("finding_id") or ""),
            " ".join(str(goal or "").split()),
            str(role or "worker").strip().lower(),
        ]
    )
    return "audit-investigation:" + hashlib.sha256(material.encode()).hexdigest()[:24]


def _text_set(value: object) -> set[str]:
    return {
        str(item).strip()
        for item in (value if isinstance(value, list | tuple | set) else [])
        if str(item or "").strip()
    }


def _investigation_state(
    run_id: str,
    *,
    task_status: str,
    accepted: set[str],
    running: set[str],
    failed: set[str],
) -> str:
    if run_id in running or task_status == TaskStatus.RUNNING.value:
        return "running"
    if task_status in {"CANCELLED", "FAILED", "TIMEOUT", "CHANNEL_ERROR"}:
        return task_status.lower()
    if run_id in failed:
        return "dispatch_failed"
    if run_id in accepted:
        return "accepted"
    if task_status in {TaskStatus.PLANNING.value, TaskStatus.PENDING.value}:
        return "pending"
    return task_status.lower() or "recorded"


__all__ = [
    "AUDIT_FINDING_RELATION_ATTR",
    "fence_inactive_audit_investigations",
    "finding_investigation_payload",
    "prepare_audit_finding_relation",
    "structured_audit_finding_relation",
]
