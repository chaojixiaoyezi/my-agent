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
    NamedConversationWorkStatus,
    conversation_request_interrupt_name,
    render_conversation_task_status,
    render_verbose_control,
)
from ...agent.conversation.models import new_id


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


# LLM: CLI dispatch preserves the shared command contract while swapping only its runtime backend;
# an optional manual-Compact target is transport metadata and never parsed from command prose.
# 函数用途：按本地或 Gateway 模式执行控制命令，并可携带精确 Compact 停止目标。
def execute_chat_control(
    execution: ChatControlExecution,
    command: ConversationControlCommand,
    *,
    message_id: str = "",
    target_control_message_id: str = "",
) -> ConversationControlResult:
    if not command.valid:
        return ConversationControlResult(command.kind, False, command.usage)
    if execution.use_gateway:
        return _execute_gateway_control(
            execution,
            command,
            message_id=message_id,
            target_control_message_id=target_control_message_id,
        )
    return _execute_local_control(execution, command)


# LLM: Gateway CLI controls use the thin client's sole HTTP writer when available, preserving
# scoped owner identity and an optional exact Compact target in typed metadata across retries.
# 函数用途：向本机 Gateway 提交即时控制，保证多用户身份和精确停止目标不在传输层漂移。
def _execute_gateway_control(
    execution: ChatControlExecution,
    command: ConversationControlCommand,
    *,
    message_id: str = "",
    target_control_message_id: str = "",
) -> ConversationControlResult:
    port = int(getattr(getattr(execution.agent, "config", None), "gateway_port", 0) or 0)
    if port <= 0:
        return ConversationControlResult(command.kind, False, "Gateway 控制入口未启用。")
    expected_turn_id = (
        str(execution.state.request_id or "").strip()
        if command.kind in {"steer", "stop"}
        else ""
    )
    client_message_id = str(message_id or new_id("control")).strip()
    payload = {
        "command": _command_text(command),
        "user_id": "local-agent",
        "channel": "chat",
        "conversation_id": execution.state.session_id or "default",
        "metadata": {
            "message_id": client_message_id,
            "expected_turn_id": expected_turn_id,
        },
    }
    selected_control_target = str(target_control_message_id or "").strip()
    if selected_control_target:
        payload["metadata"]["target_control_message_id"] = selected_control_target
    timeout = _gateway_control_timeout(execution, command)
    body: dict[str, object] = {}
    last_error: BaseException | None = None
    post_json = getattr(execution.agent, "post_gateway_json", None)
    if callable(post_json):
        for _attempt in range(2):
            status, decoded = post_json("/control", payload, timeout=timeout)
            if int(status or 0) > 0:
                body = dict(decoded) if isinstance(decoded, dict) else {}
                body["_http_status"] = int(status)
                break
            last_error = OSError("gateway client transport returned no status")
    else:
        for _attempt in range(2):
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
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    decoded = json.loads(response.read().decode("utf-8", "replace"))
                    body = decoded if isinstance(decoded, dict) else {}
                    break
            except urllib.error.HTTPError as exc:
                body = _http_error_body(exc)
                return _gateway_control_result_from_body(command.kind, body)
            except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
                last_error = exc
    if not body:
        return ConversationControlResult(
            command.kind,
            False,
            f"Gateway 控制入口暂时不可用：{type(last_error).__name__ if last_error else 'UnknownError'}。",
            control_state="transport_unknown",
        )
    return _gateway_control_result_from_body(command.kind, body)


# LLM: Polling a stable control operation is read-only and authenticates both owner and exact
# conversation. An operation id locates a receipt but never substitutes for either scope fact.
# 函数用途: 按 Gateway 返回的 operation_id、当前 TUI 身份和精确会话查询最终状态。
def request_gateway_control_status(
    execution: ChatControlExecution,
    operation_id: str,
) -> ConversationControlResult:
    stable_id = str(operation_id or "").strip()
    port = int(getattr(getattr(execution.agent, "config", None), "gateway_port", 0) or 0)
    if not stable_id or port <= 0:
        return ConversationControlResult(
            "unsupported",
            False,
            "控制操作编号无效。",
            operation_id=stable_id,
            control_state="transport_unknown",
        )
    get_json = getattr(execution.agent, "get_gateway_json", None)
    if callable(get_json):
        status, decoded = get_json(
            f"/control-status/{stable_id}",
            timeout=2.0,
            conversation_id=execution.state.session_id or "default",
        )
        if int(status or 0) <= 0:
            return ConversationControlResult(
                "unsupported",
                False,
                "控制操作状态暂时无法读取。",
                operation_id=stable_id,
                control_state="transport_unknown",
            )
        body = dict(decoded) if isinstance(decoded, dict) else {}
        body["_http_status"] = int(status)
    else:
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/control-status/{stable_id}",
            headers={
                "X-User-Id": "local-agent",
                "X-Channel": "chat",
                "X-Conversation-Id": execution.state.session_id or "default",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=2.0) as response:
                decoded = json.loads(response.read().decode("utf-8", "replace"))
                body = decoded if isinstance(decoded, dict) else {}
        except urllib.error.HTTPError as exc:
            body = _http_error_body(exc)
        except (urllib.error.URLError, OSError, json.JSONDecodeError):
            return ConversationControlResult(
                "unsupported",
                False,
                "控制操作状态暂时无法读取。",
                operation_id=stable_id,
                control_state="transport_unknown",
            )
    return _gateway_control_result_from_body(
        str(body.get("kind") or "unsupported"),
        body,
    )


# LLM: Control HTTP responses preserve operation identity, delivery state, and error_code
# separately from localized prose; callers must never infer interruption or retry safety from text.
# 函数用途: 把控制 HTTP 字典恢复成含结构化错误码的共享 ConversationControlResult。
def _gateway_control_result_from_body(
    fallback_kind: str,
    body: dict[str, object],
) -> ConversationControlResult:
    delivery_status = str(body.get("delivery_status") or "")
    if delivery_status not in {"", "accepted", "rejected", "unknown"}:
        delivery_status = ""
    control_state = str(body.get("control_state") or "")
    error_code = str(body.get("error_code") or "")
    try:
        http_status = int(body.get("_http_status") or 0)
    except (TypeError, ValueError):
        http_status = 0
    if error_code == "IDEMPOTENCY_CONFLICT":
        control_state = "conflict"
    elif http_status in {400, 401, 403}:
        control_state = "rejected"
    elif http_status >= 429:
        control_state = "transport_unknown"
    return ConversationControlResult(
        str(body.get("kind") or fallback_kind or "unsupported"),
        bool(body.get("ok")),
        str(body.get("message") or body.get("error") or "控制命令没有返回结果。"),
        request_id=str(body.get("request_id") or ""),
        status=_conversation_task_status_from_body(body.get("task_status")),
        delivery_status=delivery_status,
        guidance_dedupe_key=str(body.get("guidance_dedupe_key") or ""),
        operation_id=str(body.get("operation_id") or ""),
        control_state=control_state,
        error_code=error_code,
    )


# LLM: HTTP task status is untrusted adapter input. Restore only the frozen scalar fields and
# bounded durable-work rows; malformed values cannot become a compact boundary or runtime state.
# 函数用途: 把 Gateway 控制结果里的结构化任务状态安全恢复成共享 dataclass，供 TUI 消费真实 Compact 代数。
def _conversation_task_status_from_body(value: object) -> ConversationTaskStatus | None:
    if not isinstance(value, dict):
        return None
    state = str(value.get("state") or "idle").strip().lower()
    if state not in {"idle", "queued", "running", "stopping"}:
        state = "idle"
    compact_generation = _optional_nonnegative_int(value.get("compact_generation"))
    durable_work: list[NamedConversationWorkStatus] = []
    raw_work = value.get("durable_work")
    if isinstance(raw_work, list | tuple):
        for row in raw_work[:64]:
            if not isinstance(row, dict):
                continue
            kind = str(row.get("kind") or "").strip().lower()
            name = str(row.get("name") or "").strip()
            if kind not in {"audit", "goal"} or not name:
                continue
            durable_work.append(
                NamedConversationWorkStatus(
                    kind=kind,
                    name=name[:64],
                    status=str(row.get("status") or "")[:32],
                    elapsed_seconds=_nonnegative_float(row.get("elapsed_seconds")),
                )
            )
    return ConversationTaskStatus(
        state=state,
        task=str(value.get("task") or ""),
        elapsed_seconds=_nonnegative_float(value.get("elapsed_seconds")),
        queued_count=_nonnegative_int(value.get("queued_count")),
        recent_progress=str(value.get("recent_progress") or ""),
        subagent_total=_nonnegative_int(value.get("subagent_total")),
        subagent_running=_nonnegative_int(value.get("subagent_running")),
        subagent_done=_nonnegative_int(value.get("subagent_done")),
        subagent_failed=_nonnegative_int(value.get("subagent_failed")),
        model_name=str(value.get("model_name") or ""),
        compact_generation=compact_generation,
        verbose_level=str(value.get("verbose_level") or ""),
        durable_work=tuple(durable_work),
    )


# LLM: Numeric control projections reject booleans and malformed strings instead of silently
# turning them into status authority.
# 函数用途: 将控制结果中的整数计数安全收口为非负值。
def _nonnegative_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


# LLM: Optional compact generation distinguishes an absent field from canonical generation zero.
# 函数用途: 安全读取可选的 Compact 代数，非法输入返回空。
def _optional_nonnegative_int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


# LLM: Elapsed time is display-only and cannot accept negative or non-numeric transport values.
# 函数用途: 将控制结果中的耗时安全收口为非负秒数。
def _nonnegative_float(value: object) -> float:
    if isinstance(value, bool):
        return 0.0
    try:
        return max(0.0, float(value or 0.0))
    except (TypeError, ValueError):
        return 0.0


# LLM: Structured HTTP errors such as idempotency conflicts are terminal protocol facts; reading
# their JSON body prevents the client from retrying them as transport failures.
# 函数用途: 从 urllib HTTPError 中读取控制接口的结构化错误对象。
def _http_error_body(exc: urllib.error.HTTPError) -> dict[str, object]:
    try:
        payload = json.loads(exc.read().decode("utf-8", "replace"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    result = payload if isinstance(payload, dict) else {}
    result["_http_status"] = int(exc.code)
    return result


# LLM: Local control targets only the exact run currently mounted in this chat window.
# 函数用途：在不启动 Gateway 的终端聊天里查询、纠偏或停止当前执行轮。
def _execute_local_control(
    execution: ChatControlExecution,
    command: ConversationControlCommand,
) -> ConversationControlResult:
    state = execution.state
    request_id = str(state.request_id or "").strip()
    if command.kind in {"context", "compact"}:
        return ConversationControlResult(
            command.kind,
            False,
            f"/{command.kind} 需要使用 canonical Gateway 会话；请用 chat --gateway。",
        )
    if command.kind == "effort":
        viewing = command.operation in {"view", "help"}
        prefix = (
            "用法：/effort [low|medium|high|max|auto]\n"
            if command.operation == "help"
            else ""
        )
        return ConversationControlResult(
            "effort",
            viewing,
            prefix + "当前模型接口由供应商管理推理强度，未声明可调 effort 档位；未改变任何模型参数。",
        )
    if command.kind == "status":
        status = _local_status(execution)
        return ConversationControlResult(
            "status",
            True,
            render_conversation_task_status(status),
            request_id=request_id,
            status=status,
        )
    if command.kind in {"goal", "audit"}:
        return ConversationControlResult(
            command.kind,
            False,
            "命名持续任务需要 Gateway 的持久会话和后台续跑；"
            "请使用默认 my-agent 或 chat --gateway。",
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
    if command.kind == "context":
        return "/context"
    if command.kind == "compact":
        return f"/compact {command.value}".rstrip()
    if command.kind == "effort":
        return f"/effort {command.value}".rstrip()
    if command.kind == "steer":
        return f"/btw {command.value}"
    if command.kind == "goal":
        operation = command.operation or "view"
        if operation == "view":
            return "/goal"
        if operation == "create":
            if command.duration_seconds is not None and command.name:
                return (
                    f"/goal {_duration_token(command.duration_seconds)} "
                    f"{command.name} {command.value}"
                )
            return f"/goal {command.value}"
        if operation == "edit":
            return f"/goal edit {command.value}"
        if command.name:
            return f"/goal {command.name} {operation}"
        return f"/goal {operation}"
    if command.kind == "audit":
        if command.operation == "start":
            if command.duration_seconds is None or not command.name or not command.value:
                return "/audit"
            return (
                f"/audit {_duration_token(command.duration_seconds)} "
                f"{command.name} {command.value}"
            )
        if command.operation == "help":
            return "/audit help"
        return f"/audit {command.name} {command.operation}".rstrip()
    if command.kind == "verbose":
        return f"/verbose {command.value}".rstrip()
    if command.kind == "unsupported":
        return f"/{command.operation or 'unsupported'}"
    return f"/{command.kind}"


# LLM: Manual compact may make one real summary-model call, so its client timeout must cover the
# provider request timeout; all cheap controls retain the bounded service-command timeout.
# 函数用途: 为普通控制和手动压缩选择不同的 HTTP 等待上限。
def _gateway_control_timeout(
    execution: ChatControlExecution,
    command: ConversationControlCommand,
) -> float:
    config = getattr(execution.agent, "config", None)
    service_timeout = max(
        10.0,
        float(getattr(config, "gateway_service_command_timeout_seconds", 30) or 30),
    )
    if command.kind != "compact":
        return service_timeout
    provider_timeout = max(
        0.0,
        float(getattr(config, "request_timeout", 0) or 0),
    )
    return max(service_timeout, provider_timeout + 30.0)


def _duration_token(seconds: int) -> str:
    if seconds % 86400 == 0:
        return f"{seconds // 86400}d"
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    return f"{max(1, seconds // 60)}m"


__all__ = [
    "ChatControlExecution",
    "ChatControlState",
    "execute_chat_control",
]
