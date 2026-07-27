from __future__ import annotations

"""CLI adapter for the shared ordinary-conversation control protocol.

给人看的解释：
终端聊天不再自己发明 `/btw` 和 `/status` 的含义。Gateway 模式把命令交给同一个
服务端控制入口；本地直跑模式也使用同一份命令和状态文本，只替换运行事实来源。
"""

import json
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from ...agent.concurrency.interrupt import interrupt_by_name
from ...agent.conversation.control_commands import (
    ConversationControlCommand,
    ConversationControlResult,
    ConversationTaskStatus,
    conversation_request_interrupt_name,
    render_conversation_task_status,
    render_verbose_control,
)


@dataclass(frozen=True)
class ChatControlState:
    running: bool
    queued_count: int
    prompt: str
    started_at: float
    session_id: str
    request_id: str = ""


@dataclass(frozen=True)
class ChatControlExecution:
    agent: object
    use_gateway: bool
    state: ChatControlState


# LLM: CLI dispatch preserves the shared command contract while swapping only its runtime backend.
# 函数用途：按当前 chat 是 Gateway 模式还是本地模式执行同一条控制命令。
def execute_chat_control(
    execution: ChatControlExecution,
    command: ConversationControlCommand,
) -> ConversationControlResult:
    if not command.valid:
        return ConversationControlResult(command.kind, False, command.usage)
    if execution.use_gateway:
        return _execute_gateway_control(execution, command)
    return _execute_local_control(execution, command)


# LLM: Gateway CLI controls use the same /control endpoint and conversation identity as IM adapters.
# 函数用途：向本机 Gateway 提交即时控制，不进入普通任务队列。
def _execute_gateway_control(
    execution: ChatControlExecution,
    command: ConversationControlCommand,
) -> ConversationControlResult:
    port = int(getattr(getattr(execution.agent, "config", None), "gateway_port", 0) or 0)
    if port <= 0:
        return ConversationControlResult(command.kind, False, "Gateway 控制入口未启用。")
    payload = {
        "command": _command_text(command),
        "user_id": "local-agent",
        "channel": "chat",
        "conversation_id": execution.state.session_id or "default",
    }
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/control",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-User-Id": "local-agent",
            "X-Channel": "chat",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            body = json.loads(response.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        return ConversationControlResult(
            command.kind,
            False,
            f"Gateway 控制入口暂时不可用：{type(exc).__name__}。",
        )
    return ConversationControlResult(
        command.kind,
        bool(body.get("ok")),
        str(body.get("message") or "控制命令没有返回结果。"),
        request_id=str(body.get("request_id") or ""),
    )


# LLM: Local control targets only the exact run currently mounted in this chat window.
# 函数用途：在不启动 Gateway 的终端聊天里查询、纠偏或停止当前执行轮。
def _execute_local_control(
    execution: ChatControlExecution,
    command: ConversationControlCommand,
) -> ConversationControlResult:
    state = execution.state
    request_id = str(state.request_id or "").strip()
    if command.kind == "status":
        status = _local_status(execution)
        return ConversationControlResult(
            "status",
            True,
            render_conversation_task_status(status),
            request_id=request_id,
            status=status,
        )
    if command.kind == "goal":
        return ConversationControlResult(
            "goal",
            False,
            "持续目标需要 Gateway 的持久会话和后台续跑；请使用默认 my-agent 或 chat --gateway。",
        )
    if command.kind == "verbose":
        return _execute_local_verbose(execution, command)
    if command.kind == "steer":
        if not state.running or not request_id:
            return ConversationControlResult(
                "steer",
                False,
                "当前没有运行中的内容，补充要求未保存。",
            )
        try:
            execution.agent.conversation_store.append_guidance(
                {
                    "target_type": "request",
                    "target_id": request_id,
                    "message": command.value,
                    "sender": "local-cli",
                    "priority": "high",
                    "delivery": "current_request",
                }
            )
        except Exception:
            return ConversationControlResult("steer", False, "当前任务的补充通道暂时不可用。")
        return ConversationControlResult(
            "steer",
            True,
            "已补充到当前任务；代理会在下一个安全点按新要求调整。",
            request_id=request_id,
        )
    interrupted = bool(request_id) and interrupt_by_name(
        conversation_request_interrupt_name(request_id)
    )
    if not interrupted:
        return ConversationControlResult("stop", False, "当前没有运行中的内容，无需停止。")
    _retire_local_guidance(execution.agent, request_id)
    _cancel_local_subagents(execution.agent, request_id)
    return ConversationControlResult(
        "stop",
        True,
        "已停止当前会话正在执行的内容。",
        request_id=request_id,
    )


# LLM: Local stop retires only the interrupted request's pending steer inputs.
# 函数用途：防止本地窗口停止后，旧 `/btw` 在后续新一轮里意外生效。
def _retire_local_guidance(agent: object, request_id: str) -> None:
    try:
        store = agent.conversation_store
        entries = store.pending_guidance("request", request_id, limit=0)
        store.mark_guidance_delivered([entry.guidance_id for entry in entries])
    except Exception:
        return


# LLM: Local verbose persists the same per-thread setting as Gateway without opening a model turn.
# 函数用途：在本地直跑窗口读取或修改过程显示档位，并直接返回系统回执。
def _execute_local_verbose(
    execution: ChatControlExecution,
    command: ConversationControlCommand,
) -> ConversationControlResult:
    try:
        store = execution.agent.conversation_store
        thread = store.get_or_create_thread(
            {
                "canonical_user_id": "local-agent",
                "channel": "chat",
                "channel_conversation_id": execution.state.session_id or "default",
                "channel_user_id": "local-agent",
                "title": "会话设置",
            }
        )
        if command.value:
            thread = store.update_verbose_level(thread.thread_id, command.value)
        return ConversationControlResult(
            "verbose",
            True,
            render_verbose_control(
                str(getattr(thread, "verbose_level", "off") or "off"),
                command,
            ),
        )
    except Exception:
        return ConversationControlResult(
            "verbose",
            False,
            "当前会话的详细过程设置暂时不可用，请稍后重试。",
        )


# LLM: Local /status renders only facts already held by the chat worker and configured model.
# 函数用途：生成本地直跑任务的即时状态快照。
def _local_status(execution: ChatControlExecution) -> ConversationTaskStatus:
    state = execution.state
    status = "running" if state.running else "queued" if state.queued_count else "idle"
    return ConversationTaskStatus(
        state=status,
        task=state.prompt if state.running else "",
        elapsed_seconds=(
            max(0.0, time.perf_counter() - state.started_at)
            if state.running and state.started_at
            else 0.0
        ),
        queued_count=state.queued_count,
        recent_progress="正在处理" if state.running else "",
        model_name=str(
            getattr(getattr(execution.agent, "config", None), "model_name", "") or ""
        ),
    )


# LLM: Subagent teardown stays asynchronous so a local stop acknowledgement is immediate.
# 函数用途：后台回收本地当前请求派生的子代理树。
def _cancel_local_subagents(agent: object, request_id: str) -> None:
    try:
        run_ids = agent.subagent_run_ids_for_request(request_id)
    except Exception:
        run_ids = []

    def cancel() -> None:
        try:
            agent.cancel_request_subagents(
                request_id,
                reason="conversation_user_stop",
                run_ids=run_ids,
            )
        except Exception:
            return

    threading.Thread(target=cancel, name=f"cancel-{request_id}", daemon=True).start()


# LLM: Re-serialization preserves the parsed command instead of trusting arbitrary caller text.
# 函数用途：把结构化命令还原成 Gateway 可以再次校验的规范文本。
def _command_text(command: ConversationControlCommand) -> str:
    if command.kind == "steer":
        return f"/btw {command.value}"
    if command.kind == "goal":
        operation = command.operation or "view"
        if operation == "view":
            return "/goal"
        if operation == "create":
            return f"/goal {command.value}"
        if operation == "edit":
            return f"/goal edit {command.value}"
        return f"/goal {operation}"
    if command.kind == "verbose":
        return f"/verbose {command.value}".rstrip()
    if command.kind == "unsupported":
        return f"/{command.operation or 'unsupported'}"
    return f"/{command.kind}"


__all__ = [
    "ChatControlExecution",
    "ChatControlState",
    "execute_chat_control",
]
