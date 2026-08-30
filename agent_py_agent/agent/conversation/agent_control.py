"""Owner-scoped view, guidance, approval, and stop operations for delegated agents."""

# LLM: This module is the transport-neutral user control plane for TUI, Web, and
# IM surfaces. It resolves one authenticated conversation root, authorizes one
# exact descendant run, and delegates mutations to canonical lifecycle services.
# 模块用途: 让用户查看、插话或停止自己当前主任务树中的任意子代理，不在前端直接修改任务账本。

from __future__ import annotations

import threading
from collections.abc import Mapping
from dataclasses import dataclass

from ..agent_core.orchestration.tools.cancel import (
    CancelSubagentTaskRequest,
    cancel_subagent_tree,
)
from ..runtime_errors import DataCorruptionError
from ..subagents.authorization_gate import OperationRequest, authorize_operation
from ..subagents.models import SUBAGENT_ENDED_STATUSES, task_status_in
from .agent_activity import conversation_agent_view
from .agent_tool_approval import resolve_subagent_tool_approval
from .store import ConversationStore

MAX_AGENT_GUIDANCE_CHARS = 1 << 20

_AGENT_STOP_LOCK = threading.Lock()
_AGENT_STOP_ACTIVE: set[tuple[str, str]] = set()


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


# LLM: Child input uses one caller-supplied stable message id, binds it to the
# canonical current AgentAttempt, and writes one ConversationStore idempotency
# receipt. Acceptance means queued only; provider consumption is a later state.
# It never resumes a terminal child or creates a replacement run.
# 函数用途: 把普通用户消息精确排进当前子代理回合，并返回真实的排队状态。
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
    if _task_is_terminal(task):
        raise AgentControlError(409, "AGENT_ALREADY_TERMINAL", "这个子代理已经结束；当前详情页只读。")
    try:
        expected_turn_id = _active_agent_turn_id(agent, task)
    except Exception as exc:
        raise AgentControlError(
            503,
            "AGENT_ATTEMPT_UNAVAILABLE",
            "暂时无法确认子代理当前执行回合，消息未发送。",
        ) from exc
    if not expected_turn_id:
        raise AgentControlError(
            409,
            "AGENT_NOT_RUNNING",
            "子代理正在切换执行回合，消息未发送；请稍后重试。",
        )
    dedupe_key = f"user-agent-guidance:{stable_id}"
    try:
        entry = store.append_guidance_once(
            {
                "target_type": "agent_run",
                "target_id": str(getattr(task, "id", "") or ""),
                "message": content,
                "sender": f"user:{getattr(scope, 'user_id', '') or 'owner'}",
                "priority": "normal",
                "delivery": "next_turn",
                "metadata": {
                    "channel_message_id": stable_id,
                    "conversation_thread_id": str(getattr(thread, "thread_id", "") or ""),
                    "expected_turn_id": expected_turn_id,
                    "source": "user_agent_control",
                },
            },
            dedupe_key=dedupe_key,
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
    return {
        "ok": True,
        "delivery": "queued",
        "status": "pending",
        "operation_id": stable_id,
        "guidance_id": entry.guidance_id,
        "expected_turn_id": expected_turn_id,
        "run_id": str(getattr(task, "id", "") or ""),
    }


# LLM: Managed mode reads the exact current attempt from RuntimeDB and accepts
# only pending/running slices; the task projection may lag and is used only as
# an additional mismatch fence. Local-unmanaged runners can use their canonical
# task-local active pointer, but no prose/status label may invent a turn id.
# 函数用途: 找到这条插话唯一归属的子代理执行回合；没有活跃回合时返回空字符串让入口拒绝写账。
def _active_agent_turn_id(agent: object, task: object) -> str:
    manager = getattr(agent, "subagents", None)
    repo = getattr(manager, "runtime_db", None)
    projected_attempt_id = str(
        getattr(task, "runner_active_attempt_id", "") or ""
    ).strip()
    if repo is None:
        return projected_attempt_id
    run_id = str(getattr(task, "id", "") or "").strip()
    agent_run = repo.agent_run_for_run_id(run_id)
    if agent_run is None:
        return ""
    current = repo.current_attempt(str(agent_run["agent_run_id"] or "").strip())
    if current is None:
        return ""
    attempt_status = str(current["status"] or "").strip().lower()
    attempt_id = str(current["attempt_id"] or "").strip()
    if attempt_status not in {"pending", "running"} or not attempt_id:
        return ""
    # DB 已换代而任务投影尚未提交时属于恢复临界区；此时拒绝新消息比把它绑到一个
    # 前端还看不见、runner 也未完全就绪的 attempt 更安全。
    if not projected_attempt_id or projected_attempt_id != attempt_id:
        return ""
    return attempt_id


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
