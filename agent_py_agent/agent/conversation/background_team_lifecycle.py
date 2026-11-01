"""持久后台身份的接单/退休策略；执行权、子树停止和交付仍使用原账本。"""
from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import replace

from ..gateway_parts.io import locked_file_transition
from .business_contracts import BackgroundAgentIdentity
from .session_tasks import SESSION_TASK_TERMINAL_STATUSES

BACKGROUND_IDENTITY_KEY = "background_agent"


@contextmanager
def background_identity_transition(store, thread_id):
    # 短控制事务只串行接单与身份变更，不领执行租约、不调用模型、不增加第二执行车道。
    path = store.storage.thread_path(thread_id).with_suffix(".admission")
    with locked_file_transition(path):
        yield


def background_identity(thread):
    raw = (getattr(thread, "metadata", None) or {}).get(BACKGROUND_IDENTITY_KEY)
    if raw is None:
        return None
    identity = BackgroundAgentIdentity.from_dict(raw)
    if identity.agent_id != thread.thread_id:
        raise ValueError("BACKGROUND_IDENTITY_MISMATCH")
    return identity


def background_identity_allows_work(thread, *, existing: bool = False) -> bool:
    identity = background_identity(thread)
    return identity is None or identity.identity_state == "available" or (
        existing and identity.identity_state == "retiring"
        and thread.metadata.get("background_retirement", {}).get("disposition") == "drain")


def identity_outstanding(host, thread) -> tuple[list[str], list]:
    """原存储的当前义务；partial/坏账/未知副作用都阻止retired，不拿PID/TTL猜已结束。"""
    store = host.conversation_store
    refs, tasks = [], []
    page = store.session_tasks.list_page(limit=100, target_thread_id=thread.thread_id)
    if page["coverage"] != "complete" or page["next_cursor"] or page["load_errors"]:
        refs.append("session_tasks:coverage_unknown")
    for task in page["items"]:
        refs.extend(task.metadata.get("cancellation_control", {}).get("outstanding_refs", []))
        if task.metadata.get("task_learning_enqueue", {}).get("status") == "pending":
            refs.append(str(task.metadata.get("trusted_task_completion", {}).get("completion_ref") or task.task_id))
        if task.status not in SESSION_TASK_TERMINAL_STATUSES:
            refs.append(task.task_id)
            tasks.append(task)
    links, errors = store.tasks.active_report(thread.thread_id)
    refs.extend(link.task_id for link in links)
    if errors:
        refs.append("task_links:unreadable")
    claim, error = store.claims.load_report(thread.thread_id)
    if error:
        refs.append("claim:unreadable")
    elif claim.get("status") in {"running", "unknown"}:
        refs.append(str(claim.get("claim_id") or "claim:unknown"))
    wakes, errors = store.wakes.pending_report(limit=0)
    refs.extend(wake.wake_signal_id for wake in wakes if wake.thread_id == thread.thread_id)
    if errors:
        refs.append("wake:unreadable")
    policies, errors = store.progress.list_report(enabled_only=True)
    refs.extend(policy.policy_id for policy in policies if policy.thread_id == thread.thread_id)
    if errors:
        refs.append("subscriptions:unreadable")
    inputs, errors = store.guidance.pending_report("thread", thread.thread_id, limit=100)
    refs.extend(entry.guidance_id for entry in inputs)
    if errors:
        refs.append("guidance:unreadable")
    repository = getattr(host, "scheduler_repository", None)
    if repository is not None:
        jobs, errors = repository.list_jobs()
        refs.extend(str(job["job_id"]) for job in jobs
                    if thread.thread_id in {job.get("thread_id"), job.get("source_thread_id")}
                    and job.get("status") not in {"paused", "deleted", "disabled"})
        if errors:
            refs.append("subscriptions:unreadable")
    manager = getattr(host, "subagents", None)
    if manager is not None:
        from ..subagents.model_capabilities import capability_request_requires_parent_resolution
        from ..subagents.models import task_status_in

        report = manager.list_runs_report()
        if report.load_errors:
            refs.append("subagents:unreadable")
        for task in report.runs:
            attrs = task.attributes or {}
            if attrs.get("conversation_thread_id") != thread.thread_id:
                continue
            if not task_status_in(task.status, {"DONE", "FAILED", "CANCELLED", "STOPPED"}):
                refs.append(task.id)
            pending = [r for r in task.capability_requests if capability_request_requires_parent_resolution(r.status)]
            refs.extend(str(r.id) for r in pending)
    # 附加来源只允许宿主提供读取适配器；返回unknown不能被当作空列表。
    probe = getattr(host, "background_identity_obligations", None)
    if callable(probe):
        observed = probe(thread.thread_id)
        if not isinstance(observed, dict) or observed.get("coverage") != "complete":
            refs.append("host_obligations:unknown")
        else:
            refs.extend(str(ref) for ref in observed.get("outstanding_refs", ()))
    return list(dict.fromkeys(refs)), tasks


def reconcile_background_identity(host, thread_id: str):
    """只处理已请求的after_task/退休；不主动调用模型，不关闭未知交付/子树。"""
    with background_identity_transition(host.conversation_store, thread_id):
        return _reconcile_background_identity_locked(host, thread_id)


def _reconcile_background_identity_locked(host, thread_id):
    store = host.conversation_store
    thread = store.threads.require(thread_id)
    identity = background_identity(thread)
    if identity is None or identity.identity_state in {"suspended", "retired"}:
        return thread
    refs, _ = identity_outstanding(host, thread)
    if refs:
        return thread
    target = ("retired" if identity.identity_state == "retiring" or identity.after_task == "retire"
              else "suspended" if identity.after_task == "suspend" else "available")
    if target == identity.identity_state:
        return thread

    def apply(latest):
        current = background_identity(latest)
        if current != identity:
            return latest
        updated = replace(current, identity_state=target, identity_revision=current.identity_revision + 1)
        return replace(latest, metadata={**latest.metadata, BACKGROUND_IDENTITY_KEY: updated.to_dict()}, updated_at=time.time())

    return store.threads.update_atomic(thread_id, apply)
