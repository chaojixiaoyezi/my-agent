"""Owner-scoped view, guidance, approval, and stop operations for delegated agents."""

# LLM: This module is the transport-neutral user control plane for TUI, Web, and
# IM surfaces. It resolves one authenticated conversation root, authorizes one
# exact descendant run, and delegates mutations to canonical lifecycle services.
# 模块用途: 让用户查看、插话或停止自己当前主任务树中的任意子代理，不在前端直接修改任务账本。

from __future__ import annotations

import threading
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass

from ..agent_core.orchestration.tools.cancel import (
    CancelSubagentTaskRequest,
    cancel_subagent_tree,
)
from ..runtime_db.operations import RuntimeConflictError, RuntimeExecutionBusyError
from ..runtime_errors import DataCorruptionError
from ..subagents.authorization_gate import OperationRequest, authorize_operation
from ..subagents.models import SUBAGENT_ENDED_STATUSES, task_status_in
from .agent_activity import conversation_agent_view
from .agent_tool_approval import resolve_subagent_tool_approval
from .store import ConversationStore

MAX_AGENT_GUIDANCE_CHARS = 1 << 20

_AGENT_STOP_LOCK = threading.Lock()
_AGENT_STOP_ACTIVE: set[tuple[str, str]] = set()
_AGENT_GUIDANCE_LOCKS_GUARD = threading.Lock()
_AGENT_GUIDANCE_LOCKS: dict[tuple[str, str], tuple[threading.Lock, int]] = {}


# LLM: Structured errors preserve an HTTP status and stable code without
# exposing internal paths, prompts, or raw exceptions to thin clients.
# 类用途: 把子代理控制拒绝转换成统一的 HTTP 错误响应。
@dataclass
class AgentControlError(RuntimeError):
    status: int
    error_code: str
    message: str

    # LLM: String conversion returns only the public message; callers must read
    # status/error_code fields for machine decisions.
    # 函数用途: 给日志或兜底响应提供不含内部对象的错误文字。
    def __str__(self) -> str:
        return self.message


# LLM: This internal decision binds one user message either to the exact active
# attempt or to one durable pending successor on the same AgentRun. It carries
# only structured runtime facts; display labels and model prose never select a
# turn or authorize resumption.
# 类用途: 表示子代理插话应进入哪个执行轮，以及是否需要把同一个代理重新拉起。
@dataclass(frozen=True)
class _AgentGuidanceTurn:
    expected_turn_id: str = ""
    agent_run_id: str = ""
    resume_required: bool = False
    queue_successor: bool = False


# LLM: One immutable submission keeps the authenticated scope, canonical store,
# refreshed task and client id together across first delivery and HTTP replay.
# Attempt identity remains an explicit argument because recovery may rebind it.
# 类用途: 汇总一次子代理插话的固定身份，避免首次投递和重放函数携带过多易错参数。
@dataclass(frozen=True)
class _AgentGuidanceSubmission:
    scope: object
    store: ConversationStore
    thread: object
    task: object
    content: str
    stable_id: str
    dedupe_key: str


# LLM: The single Gateway serializes only competing submissions for the same
# owner/run. Reference counts remove idle locks, so unrelated users never share
# a slow admission edge and long-lived task trees do not leak one lock per run.
# 函数用途: 给同一用户的同一子代理加一把短锁，避免并发消息重复启动且不阻塞其他用户。
@contextmanager
def _agent_guidance_admission(owner_id: str, run_id: str):
    key = (str(owner_id or "").strip(), str(run_id or "").strip())
    with _AGENT_GUIDANCE_LOCKS_GUARD:
        lock, references = _AGENT_GUIDANCE_LOCKS.get(
            key,
            (threading.Lock(), 0),
        )
        _AGENT_GUIDANCE_LOCKS[key] = (lock, references + 1)
    lock.acquire()
    try:
        yield
    finally:
        lock.release()
        with _AGENT_GUIDANCE_LOCKS_GUARD:
            current_lock, references = _AGENT_GUIDANCE_LOCKS.get(
                key,
                (lock, 1),
            )
            if current_lock is lock and references <= 1:
                _AGENT_GUIDANCE_LOCKS.pop(key, None)
            elif current_lock is lock:
                _AGENT_GUIDANCE_LOCKS[key] = (lock, references - 1)


# LLM: View is read-only after exact owner/root authorization. Cursor controls
# only the optional display-event page and has no lifecycle meaning.
# 函数用途: 读取一个可见子代理的详情页数据。
def read_agent_view(
    agent: object,
    *,
    scope: object,
    run_id: str,
    after: int = 0,
) -> dict[str, object]:
    store, _thread, task = _authorized_agent_target(
        agent,
        scope=scope,
        run_id=run_id,
        operation="view",
    )
    return conversation_agent_view(
        agent,
        store,
        str(getattr(task, "id", "") or ""),
        after=max(0, int(after or 0)),
    )


# LLM: Child input uses one caller-supplied stable message id and one exact
# AgentRun. A live runner receives it in-place; an idle/waiting nonterminal
# agent gets one pending successor attempt on that same run and is resumed.
# Acceptance still means mailbox + launch admission only; provider consumption
# is a later durable state. Terminal children remain read-only.
# 函数用途: 把普通用户消息精确排进当前或下一子代理回合，等待中的代理会立即被同 run 唤醒。
def send_agent_guidance(
    agent: object,
    *,
    scope: object,
    run_id: str,
    message: str,
    message_id: str,
) -> dict[str, object]:
    store, thread, task = _authorized_agent_target(
        agent,
        scope=scope,
        run_id=run_id,
        operation="send_guidance",
    )
    content = str(message or "").strip()
    stable_id = str(message_id or "").strip()
    if not content or not stable_id:
        raise AgentControlError(400, "AGENT_GUIDANCE_INVALID", "消息和 message_id 不能为空。")
    if len(content) > MAX_AGENT_GUIDANCE_CHARS:
        raise AgentControlError(413, "AGENT_GUIDANCE_TOO_LARGE", "补充消息超过长度上限，未发送。")
    owner_id = str(
        getattr(getattr(agent, "home_paths", None), "owner_id", "") or ""
    ).strip()
    with _agent_guidance_admission(
        owner_id,
        str(getattr(task, "id", "") or ""),
    ):
        return _send_agent_guidance_admitted(
            agent,
            scope=scope,
            store=store,
            thread=thread,
            task=task,
            content=content,
            stable_id=stable_id,
        )


# LLM: Gateway admission is serialized only through durable attempt reservation,
# idempotent mailbox append, and nonblocking launch acceptance. Never move a
# provider call or child execution into this critical section.
# 函数用途: 在短锁内完成一次子代理插话的排队和启动接纳，避免并发重复拉起。
def _send_agent_guidance_admitted(
    agent: object,
    *,
    scope: object,
    store: ConversationStore,
    thread: object,
    task: object,
    content: str,
    stable_id: str,
) -> dict[str, object]:
    task = _reload_guidance_task(agent, task)
    submission = _AgentGuidanceSubmission(
        scope=scope,
        store=store,
        thread=thread,
        task=task,
        content=content,
        stable_id=stable_id,
        dedupe_key=f"user-agent-guidance:{stable_id}",
    )
    prior = _read_guidance_receipt(submission)
    if prior is not None:
        return _replay_agent_guidance(agent, submission, prior)
    return _submit_new_agent_guidance(agent, submission)


# LLM: Admission always refreshes the task after taking the per-run lock; an
# authorization snapshot from before the lock cannot prove current lifecycle.
# 函数用途: 重新读取子代理权威状态并确认它仍有可投递的会话身份。
def _reload_guidance_task(agent: object, task: object) -> object:
    try:
        task = agent.subagents.load(str(getattr(task, "id", "") or ""))
    except Exception as exc:
        raise AgentControlError(
            503,
            "AGENT_ATTEMPT_UNAVAILABLE",
            "暂时无法确认子代理当前状态，消息未发送。",
        ) from exc
    child_thread_id = str(getattr(task, "agent_thread_id", "") or "").strip()
    if not child_thread_id:
        raise AgentControlError(
            503,
            "AGENT_THREAD_UNAVAILABLE",
            "暂时无法确认子代理会话，消息未发送。",
        )
    return task


# LLM: Receipt lookup is fail-closed because an unreadable existing stable id
# makes first-delivery versus replay unknowable. No attempt may be reserved.
# 函数用途: 读取稳定消息 ID 的已有回执；存储不可读时明确返回未知而不重复投递。
def _read_guidance_receipt(submission: _AgentGuidanceSubmission) -> object | None:
    try:
        return submission.store.guidance_once_receipt(submission.dedupe_key)
    except (DataCorruptionError, OSError) as exc:
        raise AgentControlError(
            503,
            "AGENT_GUIDANCE_STORE_UNAVAILABLE",
            "子代理消息回执暂时不可读取，投递结果未知。",
        ) from exc


# LLM: First delivery resolves one exact current or successor attempt, persists
# the message once, then starts only the already-reserved same AgentRun.
# 函数用途: 完成首次插话的回合预留、可靠写入与必要唤醒。
def _submit_new_agent_guidance(
    agent: object,
    submission: _AgentGuidanceSubmission,
) -> dict[str, object]:
    task = submission.task
    if _task_is_terminal(task):
        raise AgentControlError(409, "AGENT_ALREADY_TERMINAL", "这个子代理已经结束；当前详情页只读。")
    turn, expected_turn_id = _reserve_guidance_turn(agent, task)
    entry = _append_guidance_entry(submission, expected_turn_id)
    resume = _resume_new_guidance_turn(agent, task, turn)
    return {
        "ok": True,
        "delivery": "queued",
        "status": "pending",
        "operation_id": submission.stable_id,
        "guidance_id": entry.guidance_id,
        "expected_turn_id": expected_turn_id,
        "run_id": str(getattr(task, "id", "") or ""),
        "resume": resume,
    }


# LLM: Attempt reservation reads only RuntimeDB lifecycle and may create one
# pending successor. The provider and runner launch remain outside this helper.
# 函数用途: 决定插话进入当前轮还是同一代理的新 pending 轮，并返回精确 attempt id。
def _reserve_guidance_turn(
    agent: object,
    task: object,
) -> tuple[_AgentGuidanceTurn, str]:
    try:
        turn = _agent_guidance_turn(agent, task)
    except Exception as exc:
        raise AgentControlError(
            503,
            "AGENT_ATTEMPT_UNAVAILABLE",
            "暂时无法确认子代理当前执行回合，消息未发送。",
        ) from exc
    if not turn.expected_turn_id:
        raise AgentControlError(
            409,
            "AGENT_NOT_RUNNING",
            "子代理正在切换执行回合，消息未发送；请稍后重试。",
        )
    expected_turn_id = turn.expected_turn_id
    if turn.queue_successor:
        try:
            queued = agent.subagents.runtime_db.queue_pending_attempt(
                turn.agent_run_id,
                source="user_agent_guidance",
            )
            expected_turn_id = str(queued["attempt_id"] or "").strip()
        except (KeyError, RuntimeConflictError, RuntimeExecutionBusyError) as exc:
            raise AgentControlError(
                503,
                "AGENT_ATTEMPT_UNAVAILABLE",
                "子代理正在切换执行回合，消息尚未排队；系统会按同一消息重试。",
            ) from exc
    if not expected_turn_id:
        raise AgentControlError(
            503,
            "AGENT_ATTEMPT_UNAVAILABLE",
            "暂时无法预留子代理下一执行回合，消息未发送。",
        )
    return turn, expected_turn_id


# LLM: One builder and one store append path enforce the same stable-id payload
# contract for first delivery and replay. Conflicts never become another row.
# 函数用途: 按稳定消息 ID 写入子代理消息箱，并把冲突和磁盘错误转成统一控制错误。
def _append_guidance_entry(
    submission: _AgentGuidanceSubmission,
    expected_turn_id: str,
) -> object:
    try:
        return submission.store.append_guidance_once(
            _agent_guidance_request(
                scope=submission.scope,
                thread=submission.thread,
                task=submission.task,
                content=submission.content,
                stable_id=submission.stable_id,
                expected_turn_id=expected_turn_id,
            ),
            dedupe_key=submission.dedupe_key,
        )
    except DataCorruptionError as exc:
        raise AgentControlError(
            409,
            "AGENT_GUIDANCE_ID_CONFLICT",
            "这条消息的身份与先前内容冲突，未重复发送。",
        ) from exc
    except OSError as exc:
        raise AgentControlError(
            503,
            "AGENT_GUIDANCE_STORE_UNAVAILABLE",
            "子代理消息箱暂时不可用，投递结果未知。",
        ) from exc


# LLM: Launch follows a durable mailbox append and only for a turn explicitly
# marked resume_required. Failure keeps the receipt pending for stable replay.
# 函数用途: 在消息保存后启动等待中的同一子代理；活跃轮不重复启动。
def _resume_new_guidance_turn(
    agent: object,
    task: object,
    turn: _AgentGuidanceTurn,
) -> dict[str, object]:
    if not turn.resume_required:
        return {"status": "active_turn", "run_ids": []}
    try:
        return _resume_agent_for_guidance(agent, task)
    except AgentControlError:
        raise
    except Exception as exc:
        raise AgentControlError(
            503,
            "AGENT_RESUME_UNAVAILABLE",
            "子代理消息已经保存，但处理轮暂未启动；系统会按同一消息重试。",
        ) from exc


# LLM: Host-owned attempt routing is the only field that may differ after a
# crash/retry rebind. All user/scope/thread fields stay in this canonical
# request builder so append_guidance_once can detect stable-id conflicts.
# 函数用途: 生成一条子代理插话的标准幂等请求，供首次提交和网络重放共用。
def _agent_guidance_request(
    *,
    scope: object,
    thread: object,
    task: object,
    content: str,
    stable_id: str,
    expected_turn_id: str,
) -> dict[str, object]:
    return {
        "target_type": "agent_run",
        "target_id": str(getattr(task, "id", "") or ""),
        "message": content,
        "sender": f"user:{getattr(scope, 'user_id', '') or 'owner'}",
        "priority": "normal",
        "delivery": "next_turn",
        "metadata": {
            "kind": "active_turn_user_input",
            "record_in_transcript": True,
            "thread_id": str(getattr(task, "agent_thread_id", "") or ""),
            "channel": str(getattr(scope, "channel", "") or "chat"),
            "conversation_id": str(getattr(scope, "conversation_id", "") or ""),
            "channel_message_id": stable_id,
            "conversation_thread_id": str(getattr(thread, "thread_id", "") or ""),
            "agent_run_id": str(getattr(task, "id", "") or ""),
            "expected_turn_id": expected_turn_id,
            "source": "user_agent_control",
        },
    }


# LLM: A stable client id is authoritative across HTTP timeout and attempt
# transitions. Consumed/submitted/reserved receipts are acknowledged exactly as
# recorded and never start another turn. Pending receipts may resume/rebind only
# through proven same-AgentRun attempt ancestry.
# 函数用途: 对网络重试返回原消息的真实回执，避免“已收到却恢复输入框”或重复唤醒。
def _replay_agent_guidance(
    agent: object,
    submission: _AgentGuidanceSubmission,
    prior: object,
) -> dict[str, object]:
    prior_entry = getattr(prior, "entry", None)
    prior_metadata = (
        getattr(prior_entry, "metadata", {})
        if isinstance(getattr(prior_entry, "metadata", {}), dict)
        else {}
    )
    expected_turn_id = str(prior_metadata.get("expected_turn_id") or "").strip()
    if not prior_entry or not expected_turn_id:
        raise AgentControlError(
            503,
            "AGENT_GUIDANCE_STORE_UNAVAILABLE",
            "子代理消息回执缺少执行轮身份，投递结果未知。",
        )
    entry = _append_guidance_entry(submission, expected_turn_id)
    receipt_status = str(getattr(prior, "status", "") or "").strip().lower()
    if receipt_status == "rejected":
        raise AgentControlError(
            409,
            "AGENT_GUIDANCE_REJECTED",
            "这条子代理消息已被执行轮明确拒绝，未重复发送。",
        )
    resume: dict[str, object] = {
        "status": f"replay_{receipt_status or 'recorded'}",
        "run_ids": [],
    }
    if receipt_status == "pending":
        if _task_is_terminal(submission.task):
            raise AgentControlError(
                409,
                "AGENT_ALREADY_TERMINAL",
                "这个子代理已经结束；当前详情页只读。",
            )
        try:
            expected_turn_id, resume = _resume_pending_guidance_replay(
                agent,
                store=submission.store,
                task=submission.task,
                expected_turn_id=expected_turn_id,
            )
            refreshed = submission.store.guidance_once_receipt(submission.dedupe_key)
        except AgentControlError:
            raise
        except Exception as exc:
            raise AgentControlError(
                503,
                "AGENT_RESUME_UNAVAILABLE",
                "子代理消息已经保存，但处理轮暂未启动；系统会按同一消息重试。",
            ) from exc
        if refreshed is not None:
            entry = refreshed.entry
    return {
        "ok": True,
        "delivery": "queued",
        "status": receipt_status or "pending",
        "operation_id": submission.stable_id,
        "guidance_id": str(getattr(entry, "guidance_id", "") or ""),
        "expected_turn_id": expected_turn_id,
        "run_id": str(getattr(submission.task, "id", "") or ""),
        "resume": resume,
        "replayed": True,
    }


# LLM: Only a pending receipt can cross this recovery edge. It either resumes
# its exact current pending turn, or is atomically rebound from a proven dead
# attempt to the current/new pending attempt on the same AgentRun.
# 函数用途: 首次启动失败或回执丢失后，安全续起仍未提交模型的同一条子代理消息。
def _resume_pending_guidance_replay(
    agent: object,
    *,
    store: ConversationStore,
    task: object,
    expected_turn_id: str,
) -> tuple[str, dict[str, object]]:
    turn = _agent_guidance_turn(agent, task)
    if not turn.expected_turn_id:
        raise AgentControlError(
            503,
            "AGENT_ATTEMPT_UNAVAILABLE",
            "子代理消息已经保存，但当前执行轮仍在切换；系统会按同一消息重试。",
        )
    target_turn_id = turn.expected_turn_id
    if turn.queue_successor:
        queued = agent.subagents.runtime_db.queue_pending_attempt(
            turn.agent_run_id,
            source="user_agent_guidance_replay",
        )
        target_turn_id = str(queued["attempt_id"] or "").strip()
    if target_turn_id != expected_turn_id:
        if not _guidance_attempt_is_rebindable(
            agent.subagents.runtime_db,
            turn.agent_run_id,
            expected_turn_id,
        ):
            raise AgentControlError(
                503,
                "AGENT_ATTEMPT_UNAVAILABLE",
                "子代理消息的旧执行轮尚不能安全接续；系统会按同一消息重试。",
            )
        summary = store.rebind_unsubmitted_guidance_for_recovered_turn(
            "agent_run",
            str(getattr(task, "id", "") or ""),
            dead_turn_ids=[expected_turn_id],
            recovered_turn_id=target_turn_id,
        )
        if int(summary.get("errors") or 0) > 0 or int(summary.get("rebound") or 0) != 1:
            raise AgentControlError(
                503,
                "AGENT_GUIDANCE_STORE_UNAVAILABLE",
                "子代理消息暂时无法接续到新执行轮；系统会按同一消息重试。",
            )
    if turn.resume_required:
        return target_turn_id, _resume_agent_for_guidance(agent, task)
    return target_turn_id, {"status": "active_turn", "run_ids": []}


# LLM: Rebind authority comes only from RuntimeDB ancestry and a terminal
# attempt status. Receipt text, task projection, or timing cannot prove death.
# 函数用途: 确认旧插话轮确属同一代理且已经安全结束，才允许把未提交消息接到新轮。
def _guidance_attempt_is_rebindable(
    repo: object,
    agent_run_id: str,
    attempt_id: str,
) -> bool:
    reader = getattr(repo, "get_attempt", None)
    if not callable(reader):
        return False
    row = reader(str(attempt_id or "").strip())
    return bool(
        row is not None
        and str(row["agent_run_id"] or "").strip() == str(agent_run_id or "").strip()
        and str(row["status"] or "").strip().lower()
        in {"done", "failed", "cancelled", "recovered"}
    )


# LLM: Active attempts keep the historical projection fence. Only canonical
# PLANNING/PENDING task states may reserve/resume an idle attempt; this mirrors
# 会话运行时 send_input starting a turn on an existing agent thread without making
# terminal or blocked runs writable.
# 函数用途: 根据 runtime.db 和任务状态决定插话是进入当前轮，还是给同一代理排下一轮。
def _agent_guidance_turn(agent: object, task: object) -> _AgentGuidanceTurn:
    manager = getattr(agent, "subagents", None)
    repo = getattr(manager, "runtime_db", None)
    projected_attempt_id = str(
        getattr(task, "runner_active_attempt_id", "") or ""
    ).strip()
    if repo is None:
        return _AgentGuidanceTurn(expected_turn_id=projected_attempt_id)
    run_id = str(getattr(task, "id", "") or "").strip()
    agent_run = repo.agent_run_for_run_id(run_id)
    if agent_run is None:
        return _AgentGuidanceTurn()
    agent_run_id = str(agent_run["agent_run_id"] or "").strip()
    current = repo.current_attempt(agent_run_id)
    if current is None:
        return _AgentGuidanceTurn()
    attempt_status = str(current["status"] or "").strip().lower()
    attempt_id = str(current["attempt_id"] or "").strip()
    if not attempt_id:
        return _AgentGuidanceTurn()
    if attempt_status == "running":
        if projected_attempt_id != attempt_id:
            return _AgentGuidanceTurn()
        return _AgentGuidanceTurn(
            expected_turn_id=attempt_id,
            agent_run_id=agent_run_id,
        )
    idle_task = task_status_in(
        str(getattr(task, "status", "") or ""),
        {"PLANNING", "PENDING"},
    )
    if not idle_task or projected_attempt_id:
        return _AgentGuidanceTurn()
    if attempt_status == "pending":
        return _AgentGuidanceTurn(
            expected_turn_id=attempt_id,
            agent_run_id=agent_run_id,
            resume_required=True,
        )
    if attempt_status in {"done", "failed", "cancelled", "recovered"}:
        return _AgentGuidanceTurn(
            expected_turn_id=attempt_id,
            agent_run_id=agent_run_id,
            resume_required=True,
            queue_successor=True,
        )
    return _AgentGuidanceTurn()


# LLM: Resume admission clears only a typed direct-child wait, then reuses the
# canonical exact-run background dispatcher. Existing runner/launch liveness is
# accepted idempotently; failures remain retriable because the guidance receipt
# and pending attempt were already committed with the caller's stable id.
# 函数用途: 让空闲或等待孩子的指定子代理立刻处理刚排队的用户消息。
def _resume_agent_for_guidance(agent: object, task: object) -> dict[str, object]:
    manager = getattr(agent, "subagents", None)
    run_id = str(getattr(task, "id", "") or "").strip()
    if manager is None or not run_id:
        raise AgentControlError(
            503,
            "AGENT_RESUME_UNAVAILABLE",
            "子代理消息已经保存，但当前无法启动处理；系统会按同一消息重试。",
        )
    from ..subagents.direct_parent_lifecycle import (
        release_parent_wait_for_user_guidance,
    )

    released_child_ids = release_parent_wait_for_user_guidance(manager, run_id)
    refreshed = manager.load(run_id)
    from ..agent_core.runner.dispatch import candidate_policy, runner_launch_in_progress
    from ..subagents.runner_session_liveness import has_fresh_runner_session

    if runner_launch_in_progress(refreshed, candidate_policy()) or has_fresh_runner_session(
        refreshed
    ):
        return {
            "status": "already_starting",
            "run_ids": [run_id],
            "released_child_wait_run_ids": list(released_child_ids),
        }
    from ..agent_core.orchestration.background.dispatch import auto_start_tasks

    result = auto_start_tasks(agent, [refreshed], {})
    status = str(result.get("status") or "").strip()
    if status != "started":
        raise AgentControlError(
            503,
            "AGENT_RESUME_UNAVAILABLE",
            "子代理消息已经保存，但处理轮暂未启动；系统会按同一消息重试。",
        )
    return {
        "status": status,
        "run_ids": list(result.get("run_ids") or []),
        "launch_id": str(result.get("launch_id") or ""),
        "released_child_wait_run_ids": list(released_child_ids),
    }


# LLM: Stop reuses the canonical cancellation primitive after the same subtree
# authorization. A terminal target is reported idempotently without touching it.
# 函数用途: 按用户 Esc 请求停止当前正在查看的子代理。
def stop_agent(
    agent: object,
    *,
    scope: object,
    run_id: str,
    operation_id: str,
) -> dict[str, object]:
    _store, _thread, task = _authorized_agent_target(
        agent,
        scope=scope,
        run_id=run_id,
        operation="cancel",
    )
    stable_id = str(operation_id or "").strip()
    if not stable_id:
        raise AgentControlError(400, "AGENT_STOP_ID_REQUIRED", "停止操作缺少 operation_id。")
    if _task_is_terminal(task):
        return {
            "ok": True,
            "operation_id": stable_id,
            "run_id": str(getattr(task, "id", "") or ""),
            "status": "already_terminal",
        }
    try:
        result = cancel_subagent_tree(
            agent,
            CancelSubagentTaskRequest(
                task=task,
                reason="用户从代理详情页按 Esc 停止",
                kill_process=True,
                source="user_agent_control",
            ),
        )
    except OSError as exc:
        raise AgentControlError(
            503,
            "AGENT_STOP_UNAVAILABLE",
            "停止请求暂时无法确认；系统没有自动重复执行。",
        ) from exc
    return {
        "ok": True,
        "operation_id": stable_id,
        "run_id": str(getattr(task, "id", "") or ""),
        "status": "cancelled",
        "result": result,
    }


# LLM: Interactive clients acknowledge an authorized stop after one background worker has been
# admitted, not after every canonical projection/index has flushed. The exact owner/run pair is
# the in-process dedupe key; the worker still uses the same synchronous canonical cancellation
# primitive as model tools, and a later view reads only durable lifecycle state.
# 函数用途: 让 TUI/Web 快速接收停止请求，在后台完成可能较慢的终态、索引和会话收口。
def enqueue_agent_stop(
    agent: object,
    *,
    scope: object,
    run_id: str,
    operation_id: str,
) -> dict[str, object]:
    _store, _thread, task = _authorized_agent_target(
        agent,
        scope=scope,
        run_id=run_id,
        operation="cancel",
    )
    stable_id = str(operation_id or "").strip()
    if not stable_id:
        raise AgentControlError(400, "AGENT_STOP_ID_REQUIRED", "停止操作缺少 operation_id。")
    stable_run_id = str(getattr(task, "id", "") or "").strip()
    if _task_is_terminal(task):
        return {
            "ok": True,
            "operation_id": stable_id,
            "run_id": stable_run_id,
            "status": "already_terminal",
        }
    owner_id = str(
        getattr(getattr(agent, "home_paths", None), "owner_id", "") or ""
    ).strip()
    active_key = (owner_id, stable_run_id)
    with _AGENT_STOP_LOCK:
        already_active = active_key in _AGENT_STOP_ACTIVE
        if not already_active:
            _AGENT_STOP_ACTIVE.add(active_key)

    if not already_active:

        # LLM: The worker owns the exact task snapshot authorized above. Any exception leaves the
        # durable run nonterminal and clears only the ephemeral dedupe key, so a later explicit
        # user action may retry; the client never performs an automatic duplicate cancellation.
        # 函数用途: 后台执行一次真实取消，并在结束后释放同一 run 的并发停止占位。
        def cancel() -> None:
            try:
                cancel_subagent_tree(
                    agent,
                    CancelSubagentTaskRequest(
                        task=task,
                        reason="用户从代理详情页按 Esc 停止",
                        kill_process=True,
                        source="user_agent_control",
                    ),
                )
            finally:
                with _AGENT_STOP_LOCK:
                    _AGENT_STOP_ACTIVE.discard(active_key)

        threading.Thread(
            target=cancel,
            name=f"agent-stop:{stable_run_id}",
            daemon=True,
        ).start()

    return {
        "ok": True,
        "operation_id": stable_id,
        "run_id": stable_run_id,
        "status": "accepted",
        "already_active": already_active,
    }


# LLM: A child approval decision reuses the exact conversation subtree gate and
# canonical pending record. The client cannot approve by row index, tool label,
# or a binding that differs from the child-published ToolApprovalRequest.
# 函数用途: 将 TUI/Web 对某个子代理具体工具调用的批准或拒绝写回等待中的原调用。
def resolve_agent_permission(
    agent: object,
    *,
    scope: object,
    run_id: str,
    request: Mapping[str, object],
    decision: Mapping[str, object],
) -> dict[str, object]:
    _store, _thread, task = _authorized_agent_target(
        agent,
        scope=scope,
        run_id=run_id,
        operation="resolve_tool_approval",
    )
    if _task_is_terminal(task):
        raise AgentControlError(
            409,
            "AGENT_ALREADY_TERMINAL",
            "这个子代理已经结束；迟到的工具决定不会生效。",
        )
    try:
        return resolve_subagent_tool_approval(
            agent,
            run_id=str(getattr(task, "id", "") or ""),
            request_value=request,
            decision_value=decision,
        )
    except FileNotFoundError as exc:
        raise AgentControlError(
            409,
            "AGENT_APPROVAL_STALE",
            "这条工具审批已经结束或不再等待。",
        ) from exc
    except (TypeError, ValueError) as exc:
        raise AgentControlError(
            409,
            "AGENT_APPROVAL_MISMATCH",
            "工具审批身份不匹配，决定未写入。",
        ) from exc
    except OSError as exc:
        raise AgentControlError(
            503,
            "AGENT_APPROVAL_UNAVAILABLE",
            "工具审批暂时无法写回；系统没有自动重复批准。",
        ) from exc


# LLM: Scope resolution first applies historical read authorization to prove
# exact owner/root ancestry. A live mutation then applies its stronger current
# attempt/binding gate; terminal targets stay readable so callers can return a
# deterministic read-only or already-terminal result instead of a false denial.
# 函数用途: 解析用户当前会话并确认目标 run 真属于这棵主任务树，运行中写操作再加一层权威运行门。
def _authorized_agent_target(
    agent: object,
    *,
    scope: object,
    run_id: str,
    operation: str,
) -> tuple[ConversationStore, object, object]:
    store = getattr(agent, "conversation_store", None)
    if not isinstance(store, ConversationStore):
        raise AgentControlError(503, "AGENT_STORE_UNAVAILABLE", "代理状态存储不可用。")
    conversation_id = str(getattr(scope, "conversation_id", "") or "").strip()
    user_id = str(getattr(scope, "user_id", "") or "").strip()
    channel = str(getattr(scope, "channel", "") or "").strip()
    if not conversation_id or not user_id or not channel:
        raise AgentControlError(400, "AGENT_SCOPE_INVALID", "当前会话身份不完整。")
    try:
        thread, load_error = store.resolve_thread_report(
            channel=channel,
            channel_conversation_id=conversation_id,
            channel_user_id=user_id,
        )
    except Exception as exc:
        raise AgentControlError(503, "AGENT_THREAD_UNAVAILABLE", "当前会话暂时不可读取。") from exc
    if thread is None or load_error is not None:
        raise AgentControlError(404, "AGENT_THREAD_NOT_FOUND", "当前会话还没有可查看的代理任务。")
    root_id = str(getattr(thread, "workspace_task_id", "") or "").strip()
    try:
        link, link_error = store.load_task_link_report(root_id)
    except Exception as exc:
        raise AgentControlError(503, "AGENT_ROOT_UNAVAILABLE", "当前主任务状态暂时不可读取。") from exc
    if (
        not root_id
        or link is None
        or link_error is not None
        or str(getattr(link, "thread_id", "") or "").strip()
        != str(getattr(thread, "thread_id", "") or "").strip()
    ):
        raise AgentControlError(404, "AGENT_ROOT_NOT_FOUND", "当前会话没有可操作的主任务树。")
    manager = getattr(agent, "subagents", None)
    if manager is None:
        raise AgentControlError(503, "AGENT_MANAGER_UNAVAILABLE", "子代理管理器不可用。")
    try:
        task = manager.load(str(run_id or "").strip())
    except (FileNotFoundError, ValueError) as exc:
        raise AgentControlError(404, "AGENT_NOT_FOUND", "目标子代理不存在。") from exc
    if str(getattr(task, "root_id", "") or "").strip() != root_id:
        raise AgentControlError(403, "AGENT_OUTSIDE_CONVERSATION", "目标子代理不属于当前会话。")
    owner_id = str(
        getattr(getattr(agent, "home_paths", None), "owner_id", "") or ""
    ).strip()
    try:
        task = authorize_operation(
            manager,
            OperationRequest(
                operation="view",
                run_id=str(getattr(task, "id", "") or ""),
                requester_owner=owner_id,
                requester_run_id=root_id,
            ),
            target=task,
        )
    except PermissionError as exc:
        raise AgentControlError(403, "AGENT_CONTROL_DENIED", "无权操作这个子代理。") from exc
    if operation != "view" and not _task_is_terminal(task):
        try:
            task = authorize_operation(
                manager,
                OperationRequest(
                    operation=operation,
                    run_id=str(getattr(task, "id", "") or ""),
                    requester_owner=owner_id,
                    requester_run_id=root_id,
                ),
                target=task,
            )
        except PermissionError as exc:
            raise AgentControlError(
                403,
                "AGENT_CONTROL_DENIED",
                "无权操作这个子代理。",
            ) from exc
    return store, thread, task


# LLM: Terminal checks use the canonical status enum only; result prose and UI
# labels can never make an agent writable or stoppable.
# 函数用途: 判断详情页是否已经进入只读终态。
def _task_is_terminal(task: object) -> bool:
    return task_status_in(
        str(getattr(task, "status", "") or "").strip(),
        SUBAGENT_ENDED_STATUSES,
    )


__all__ = [
    "AgentControlError",
    "enqueue_agent_stop",
    "read_agent_view",
    "resolve_agent_permission",
    "send_agent_guidance",
    "stop_agent",
]
