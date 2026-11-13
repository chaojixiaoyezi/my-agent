"""共用原租约和运行库的只读执行证明，供后台会话及任务观察使用。"""
from __future__ import annotations

import time

from .background_team_lifecycle import background_identity
from .session_tasks import SESSION_TASK_TERMINAL_STATUSES


def thread_execution_view(store, runtime, thread, *, claim_scope_id=""):
    claim, error = store.claims.load_report(thread.thread_id, claim_scope_id=claim_scope_id)
    if error:
        raise ValueError("CLAIM_UNREADABLE")
    if claim.get("status") != "running":
        return None
    validate_execution_claim(thread, claim)
    if runtime is None:
        raise ValueError("EXECUTION_AUTHORITY_UNAVAILABLE")
    authority = claim["runtime_authority"]
    ref = runtime.parent_execution_ref(owner_id=thread.owner_id, run_id=authority["run_id"],
                                       attempt_id=authority["attempt_id"])
    context = runtime.business_context_for_execution(owner_id=thread.owner_id, run_id=ref.run_id,
                                                     attempt_id=ref.observed_attempt_id)
    validate_execution_context(store, thread, authority, ref, context, runtime=runtime)
    identity = background_identity(thread)
    return {"run_id": ref.run_id, "attempt_id": ref.observed_attempt_id,
            "task_id": context.task_id if context else authority["task_id"],
            "thread_id": thread.thread_id, "space_id": context.space_id if context else identity.space_id,
            "status": "running", "effect_outcome": "running", "started_at": None,
            "ended_at": None, "error": None, "retry_refs": []}


def validate_execution_claim(thread, claim):
    authority = claim.get("runtime_authority")
    required = ("invocation_run_id", "task_id", "run_id", "agent_run_id", "attempt_id")
    if (claim.get("thread_id") != thread.thread_id or not isinstance(authority, dict)
            or authority.get("schema_version") != "conversation_claim_execution.v1"
            or authority.get("claim_id") != claim.get("claim_id")
            or float(claim.get("expires_at") or 0) <= time.time()
            or any(not isinstance(authority.get(key), str) or not authority[key] for key in required)):
        raise ValueError("EXECUTION_AUTHORITY_UNAVAILABLE")

# LLM: 有business context则核canonical SessionTask；原生消息无该context时核原runtime task的owner/thread，不制造派工记录。
# 函数用途: 核对当前执行与业务身份/任务的冻结绑定。
def validate_execution_context(store, thread, authority, ref, context, *, runtime):
    identity = background_identity(thread)
    run, attempt = runtime.get_agent_run(ref.agent_run_id), runtime.get_attempt(ref.observed_attempt_id)
    task_run = runtime.get_task_run(ref.task_run_id)
    if (ref.agent_run_id != authority["agent_run_id"] or ref.observed_attempt_id != authority["attempt_id"]
            or (context is not None and context.owner_id != thread.owner_id)
            or run is None or attempt is None or task_run is None
            # 父 run 在 create_attempt 后可仍为 created；准确 current attempt 的 running 才是执行事实。
            or run["status"] not in {"created", "running"} or attempt["status"] != "running"
            or run["current_attempt_id"] != ref.observed_attempt_id
            or task_run["task_id"] != authority["task_id"]
            or (context is not None and identity is not None and context.space_id != identity.space_id)):
        raise ValueError("EXECUTION_AUTHORITY_UNAVAILABLE")
    if context is None:
        # 原生会话消息也发布同一执行租约，但没有 SessionTask；只读原运行任务的 owner/thread 绑定。
        task = runtime.get_task(authority["task_id"])
        if (identity is None or task is None or task["owner_id"] != thread.owner_id
                or task["thread_id"] != thread.thread_id):
            raise ValueError("EXECUTION_AUTHORITY_UNAVAILABLE")
        return
    task = store.session_tasks.require(context.task_id)
    if (task.target_thread_id != thread.thread_id or task.status in SESSION_TASK_TERMINAL_STATUSES
            or task.task_run_id != ref.task_run_id or task.conversation_request_id != authority["invocation_run_id"]
            or task.metadata.get("business_context") != context.to_dict()):
        raise ValueError("EXECUTION_AUTHORITY_UNAVAILABLE")
