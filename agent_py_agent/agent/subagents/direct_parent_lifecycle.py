from __future__ import annotations

"""LLM: Own the recursive parent-child wait contract for every subagent depth.

模块用途: 让子代理、孙代理都用同一套“创建后暂停、孩子发事件后唤醒直属父级”规则。
"""

import time
from dataclasses import dataclass
from typing import Any

from .model_capabilities import capability_request_counts_as_open
from .models import (
    SUBAGENT_ENDED_STATUSES,
    SUBAGENT_FAILURE_STATUSES,
    TaskStatus,
    task_has_ended_status,
    task_status_in,
)
from .runner_completion_wake import completion_handoff_payload

DIRECT_CHILD_WAIT_ATTR = "direct_child_wait"
_WAIT_SCHEMA_VERSION = "direct-child-wait.v1"
_CONTEXT_CHILD_LIMIT = 12
_CONTEXT_REQUEST_LIMIT = 3


# LLM: This decision is derived only from canonical task ids/statuses and the
# durable wait marker; callers must not infer resume from child prose.
# 类用途: 表示一次孩子事件是否应该叫醒直属父级。
@dataclass(frozen=True)
class DirectParentResumeDecision:
    parent_run_id: str = ""
    marker_found: bool = False
    should_resume: bool = False
    reason: str = ""
    active_run_ids: tuple[str, ...] = ()
    terminal_run_ids: tuple[str, ...] = ()
    attention_run_ids: tuple[str, ...] = ()
    missing_run_ids: tuple[str, ...] = ()


# LLM: Persist only exact direct-child ids that are still non-terminal. The
# marker suppresses generic orphan redispatch until a child lifecycle edge.
# 函数用途: 父代理创建或继续等孩子时，把等待关系耐久化。
def mark_parent_waiting_for_direct_children(
    manager: Any,
    parent_run_id: str,
    child_run_ids: list[str] | tuple[str, ...] | None = None,
) -> tuple[str, ...]:
    parent_id = str(parent_run_id or "").strip()
    if manager is None or not parent_id:
        return ()
    try:
        parent = manager.load(parent_id)
    except (FileNotFoundError, TypeError, ValueError):
        return ()
    if task_has_ended_status(parent):
        return ()
    attrs = dict(getattr(parent, "attributes", {}) or {})
    previous = _wait_record(parent)
    candidate_ids = _unique_ids(
        [
            *list(previous.get("run_ids") or []),
            *list(child_run_ids or []),
            *([] if child_run_ids is not None else list(getattr(parent, "child_ids", []) or [])),
        ]
    )
    active_ids: list[str] = []
    for child_id in candidate_ids:
        child = _load_exact_direct_child(manager, parent_id, child_id)
        if child is not None and not task_status_in(
            getattr(child, "status", ""), SUBAGENT_ENDED_STATUSES
        ):
            active_ids.append(child_id)
    if not active_ids:
        if attrs.pop(DIRECT_CHILD_WAIT_ATTR, None) is not None:
            parent.attributes = attrs
            manager.save(parent)
        return ()
    now = time.time()
    attrs[DIRECT_CHILD_WAIT_ATTR] = {
        "schema_version": _WAIT_SCHEMA_VERSION,
        "state": "waiting",
        "run_ids": active_ids,
        "created_at": float(previous.get("created_at") or now),
        "updated_at": now,
    }
    parent.attributes = attrs
    manager.save(parent)
    return tuple(active_ids)


# LLM: Dispatch may read only this typed marker; it must not inspect goals,
# summaries, or phrases such as "waiting for children".
# 函数用途: 判断一个 PENDING 父代理是否正在合法等孩子，避免被孤儿恢复器误启。
def parent_wait_blocks_dispatch(task: object) -> bool:
    marker = _wait_record(task)
    return marker.get("state") == "waiting" and bool(_unique_ids(marker.get("run_ids")))


# LLM: A real user message is an explicit control event that may wake this exact
# logical parent before its children finish. Clear only the typed wait marker in
# one canonical mutation; child identities and their durable work keep running,
# and the next parent slice may mark the remaining active children again.
# 函数用途: 用户从代理详情页插话时解除“等待直属孩子”，让同一个父代理立即处理消息。
def release_parent_wait_for_user_guidance(
    manager: Any,
    parent_run_id: str,
) -> tuple[str, ...]:
    parent_id = str(parent_run_id or "").strip()
    if manager is None or not parent_id:
        return ()
    released_run_ids: list[str] = []

    # LLM: The reducer runs under the manager's canonical state guard. It may
    # mutate only the wait marker and must never stop, complete, or rewrite a
    # child from this parent-side control edge.
    # 函数用途: 在锁内复读最新父任务并精确删掉等待标记。
    def release(parent: object) -> None:
        if task_has_ended_status(parent):
            return
        marker = _wait_record(parent)
        run_ids = _unique_ids(marker.get("run_ids"))
        if marker.get("state") != "waiting" or not run_ids:
            return
        attrs = dict(getattr(parent, "attributes", {}) or {})
        attrs.pop(DIRECT_CHILD_WAIT_ATTR, None)
        parent.attributes = attrs
        released_run_ids.extend(run_ids)

    try:
        manager.mutate(parent_id, release)
    except (FileNotFoundError, TypeError, ValueError):
        return ()
    return tuple(released_run_ids)


# LLM: One child terminal event may resume only its exact direct parent. A
# normal DONE child is batched until all marked siblings end; failures and
# missing canonical rows demand immediate parent attention.
# 函数用途: 收到孩子结果后核对等待清单，决定是继续等同批兄弟还是叫醒父级。
def reconcile_parent_wait_for_child(
    manager: Any,
    child_run_id: str,
) -> DirectParentResumeDecision:
    child_id = str(child_run_id or "").strip()
    if not child_id:
        return DirectParentResumeDecision(reason="missing_child_id")
    try:
        child = manager.load(child_id)
    except (FileNotFoundError, TypeError, ValueError):
        return DirectParentResumeDecision(reason="child_not_found")
    parent_id = str(getattr(child, "parent_id", "") or "").strip()
    if not parent_id:
        return DirectParentResumeDecision(reason="root_parent")
    return _reconcile_parent_wait(manager, parent_id)


# LLM: Crash recovery reconciles stale wait markers before ordinary orphan
# revival so a completed child cannot leave its parent parked forever.
# 函数用途: 周期巡检所有等孩子的父代理，补偿进程重启期间丢失的事件。
def reconcile_all_parent_waits(manager: Any) -> dict[str, object]:
    checked = 0
    released: list[str] = []
    try:
        tasks = manager.list_runs()
    except Exception:
        return {"checked": 0, "released": 0, "released_run_ids": []}
    for task in tasks:
        if not parent_wait_blocks_dispatch(task):
            continue
        checked += 1
        decision = _reconcile_parent_wait(manager, str(getattr(task, "id", "") or ""))
        if decision.should_resume:
            released.append(decision.parent_run_id)
    return {
        "checked": checked,
        "released": len(released),
        "released_run_ids": released,
    }


# LLM: The resumed parent receives bounded host facts for direct children only;
# full model responses and hidden reasoning remain in referenced archives.
# 函数用途: 把直属孩子的状态、结果引用和权限申请放进父级新回合上下文。
def direct_children_context_payload(manager: Any, parent_run_id: str) -> dict[str, object]:
    parent_id = str(parent_run_id or "").strip()
    if not parent_id:
        return {}
    try:
        parent = manager.load(parent_id)
    except (FileNotFoundError, TypeError, ValueError):
        return {}
    children = []
    for child_id in _unique_ids(getattr(parent, "child_ids", []) or []):
        child = _load_exact_direct_child(manager, parent_id, child_id)
        if child is not None:
            children.append(child)
    children.sort(
        key=lambda item: float(getattr(item, "created_at", 0.0) or 0.0)
    )
    rows = [_direct_child_context_row(item) for item in children[-_CONTEXT_CHILD_LIMIT:]]
    active_ids = [
        str(getattr(item, "id", "") or "")
        for item in children
        if not task_status_in(getattr(item, "status", ""), SUBAGENT_ENDED_STATUSES)
    ]
    attention_ids = [
        str(getattr(item, "id", "") or "")
        for item in children
        if task_status_in(getattr(item, "status", ""), SUBAGENT_FAILURE_STATUSES)
    ]
    counts: dict[str, int] = {}
    for item in children:
        status = str(getattr(item, "status", "") or "UNKNOWN").strip().upper()
        counts[status] = counts.get(status, 0) + 1
    return {
        "schema_version": "direct-children-context.v2",
        "parent_run_id": parent_id,
        "total": len(children),
        "counts": counts,
        "all_terminal": bool(children) and not active_ids,
        "active_run_ids": active_ids,
        "attention_run_ids": attention_ids,
        "items": rows,
    }


# LLM: Reconciliation clears the marker only when the parent is eligible for
# one event-driven slice; its next slice re-marks any children still active.
# 函数用途: 核对一个父级的精确等待记录并在需要反应时释放它。
def _reconcile_parent_wait(
    manager: Any,
    parent_run_id: str,
) -> DirectParentResumeDecision:
    parent_id = str(parent_run_id or "").strip()
    try:
        parent = manager.load(parent_id)
    except (FileNotFoundError, TypeError, ValueError):
        return DirectParentResumeDecision(parent_run_id=parent_id, reason="parent_not_found")
    marker = _wait_record(parent)
    run_ids = _unique_ids(marker.get("run_ids"))
    if not run_ids:
        return DirectParentResumeDecision(parent_run_id=parent_id, reason="no_wait_marker")
    active: list[str] = []
    terminal: list[str] = []
    attention: list[str] = []
    missing: list[str] = []
    for child_id in run_ids:
        child = _load_exact_direct_child(manager, parent_id, child_id)
        if child is None:
            missing.append(child_id)
            attention.append(child_id)
            continue
        status = str(getattr(child, "status", "") or "").strip()
        if task_status_in(status, SUBAGENT_FAILURE_STATUSES):
            terminal.append(child_id)
            attention.append(child_id)
        elif task_status_in(status, SUBAGENT_ENDED_STATUSES):
            terminal.append(child_id)
        else:
            active.append(child_id)
    parent_terminal = task_has_ended_status(parent)
    should_resume = not parent_terminal and (bool(attention) or not active)
    reason = (
        "parent_terminal"
        if parent_terminal
        else "child_attention_required"
        if attention
        else "all_direct_children_terminal"
        if not active
        else "siblings_still_active"
    )
    if should_resume or parent_terminal:
        attrs = dict(getattr(parent, "attributes", {}) or {})
        attrs.pop(DIRECT_CHILD_WAIT_ATTR, None)
        parent.attributes = attrs
        manager.save(parent)
    return DirectParentResumeDecision(
        parent_run_id=parent_id,
        marker_found=True,
        should_resume=should_resume,
        reason=reason,
        active_run_ids=tuple(active),
        terminal_run_ids=tuple(terminal),
        attention_run_ids=tuple(attention),
        missing_run_ids=tuple(missing),
    )


# LLM: Context rows expose bounded durable fields and refs, never raw prompts or
# unrestricted response bodies.
# 函数用途: 把单个直属孩子整理成父级可读的小型事实包。
def _direct_child_context_row(task: object) -> dict[str, object]:
    requests = []
    for request in list(getattr(task, "capability_requests", []) or []):
        if not capability_request_counts_as_open(getattr(request, "status", "OPEN")):
            continue
        requests.append(
            {
                "request_id": str(getattr(request, "id", "") or ""),
                "capability_type": str(getattr(request, "capability_type", "") or ""),
                "needed_capability": str(getattr(request, "needed_capability", "") or "")[:300],
                "tools": list(getattr(request, "requested_tools", []) or [])[:8],
                "skills": list(getattr(request, "requested_skills", []) or [])[:8],
                "path_scope": list(getattr(request, "path_scope", []) or [])[:8],
            }
        )
        if len(requests) >= _CONTEXT_REQUEST_LIMIT:
            break
    handoff = completion_handoff_payload(task)
    return {
        "run_id": str(getattr(task, "id", "") or ""),
        "status": str(getattr(task, "status", "") or ""),
        "turn_end_reason": str(getattr(task, "turn_end_reason", "") or ""),
        "failure_type": str(getattr(task, "failure_type", "") or ""),
        "goal": str(getattr(task, "goal", "") or "")[:500],
        "latest_summary": str(
            getattr(task, "latest_summary", "") or getattr(task, "result", "") or ""
        )[:600],
        "artifact_refs": list(getattr(task, "artifact_refs", []) or [])[:8],
        "evidence_refs": list(getattr(task, "evidence_refs", []) or [])[:8],
        **handoff,
        "open_capability_requests": requests,
    }


# LLM: A direct child must match both opaque id and canonical parent_id.
# 函数用途: 读取一个直属孩子，拒绝跨树或跨层误绑。
def _load_exact_direct_child(manager: Any, parent_run_id: str, child_run_id: str) -> object | None:
    try:
        child = manager.load(str(child_run_id or "").strip())
    except (FileNotFoundError, TypeError, ValueError):
        return None
    return (
        child
        if str(getattr(child, "parent_id", "") or "").strip() == parent_run_id
        else None
    )


# LLM: Read only the current schema marker; malformed or legacy values fail
# closed as no wait authority.
# 函数用途: 从任务 attributes 中读取标准的直属孩子等待记录。
def _wait_record(task: object) -> dict[str, object]:
    attrs = getattr(task, "attributes", {}) or {}
    marker = attrs.get(DIRECT_CHILD_WAIT_ATTR) if isinstance(attrs, dict) else None
    if not isinstance(marker, dict) or marker.get("schema_version") != _WAIT_SCHEMA_VERSION:
        return {}
    return marker


# LLM: Normalize opaque ids without inventing aliases or interpreting prose.
# 函数用途: 对子代理 id 去空白、去重并保持原顺序。
def _unique_ids(values: object) -> list[str]:
    if not isinstance(values, list | tuple | set):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        selected = str(value or "").strip()
        if not selected or selected in seen:
            continue
        seen.add(selected)
        result.append(selected)
    return result


__all__ = [
    "DIRECT_CHILD_WAIT_ATTR",
    "DirectParentResumeDecision",
    "direct_children_context_payload",
    "mark_parent_waiting_for_direct_children",
    "parent_wait_blocks_dispatch",
    "release_parent_wait_for_user_guidance",
    "reconcile_all_parent_waits",
    "reconcile_parent_wait_for_child",
]
