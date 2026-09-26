# LLM: HTTP handlers preserve authenticated owner/thread boundaries and typed protocol fields;
# process stream identity affects only display cursors, never model requests or authorization.
# 控制 workspace 沿持久输入保存，共用宿主权限门；HTTP 投影不自行判定目录权限。
# 模块用途: 接收 Gateway HTTP 请求并转给正式会话、控制与展示入口，统一返回结构化结果。
from __future__ import annotations

from ..common.json_io import jsonl_lines

"""Endpoint handlers used by the gateway HTTP server.

`POST /ask` is the canonical ingress for both ordinary channel messages and
typed slash commands. System commands must resolve before active-turn steering
or queue writes so their syntax never reaches transcript or model execution.
"""

import hashlib
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from ..auth.middleware import _handler_peer_ip, require_admin_handler, require_trusted_source
from ..conversation.agent_activity import (
    ConversationAgentActivity,
    conversation_agent_activity,
)
from ..conversation.agent_control import (
    AgentControlError,
    enqueue_agent_stop,
    read_agent_view,
    resolve_agent_permission,
    send_agent_guidance,
)
from ..conversation.background_transcript import read_background_transcript_events
from ..conversation.channels import (
    project_host_paths_for_channel,
    project_user_reply,
    redact_host_absolute_paths,
)
from ..conversation.control_commands import (
    ConversationControlCommand,
    ConversationControlResult,
    parse_conversation_control,
    parse_conversation_task_command,
)
from ..conversation.message_stream import NoticeDisplayCapabilities, read_background_response_page
from ..plugin_commands import plugin_namespace
from ..runtime_errors import DataCorruptionError, runtime_error_report
from .client_service import execute_gateway_client_memory, read_gateway_client_history
from .control_operation_service import (
    GatewayControlOperationConflict,
    GatewayControlOperationIdentityRequired,
    execute_gateway_control_operation,
    gateway_control_operation_status_payload,
    read_gateway_control_operation,
    reconcile_gateway_control_operation,
)
from .control_service import (
    GatewayControlScope,
    active_turn_guidance_dedupe_key,
    request_gateway_memory_curator_lifecycle,
    resolve_gateway_scope_agent,
    resolve_gateway_scope_owner,
    resolve_loaded_gateway_scope_agent,
    steer_active_conversation_if_running,
)
from .daemon_control import get_running_pid, write_targeted_gateway_stop_request
from .input_delivery_service import (
    bind_gateway_input_active_locked,
    gateway_input_guidance_binding,
    gateway_input_status_payload,
    gateway_input_transition,
    load_or_prepare_gateway_input_locked,
    queue_gateway_input_locked,
    read_gateway_input_receipt,
    reconcile_gateway_input_request,
)
from .io import gateway_request_counts, write_json_file_atomic
from .paths import gateway_chunk_path, gateway_chunk_path_candidates
from .plugin_command_service import plugin_http_response
from .request_client import GatewayAskExecutionOptions
from .response_renderer import read_gateway_terminal_envelope_report


@dataclass(frozen=True)
class _ResultAccessContext:

    request_id: str
    user_id: str
    permission: Any


@dataclass(frozen=True)
class _AskRequestContext:

    body: dict
    goal: str
    request_id: str
    user_id: str
    channel: str


def _request_identity(handler) -> tuple[str, Any]:
    mw = getattr(handler, "_auth_middleware", None)
    if mw is None:
        return "admin", None
    peer_ip = _handler_peer_ip(handler)
    user_id, _ = mw.extract_identity(dict(handler.headers), peer_ip)
    return user_id, mw.get_permission(dict(handler.headers), peer_ip)


def _request_channel(handler) -> tuple[str, str]:
    mw = getattr(handler, "_auth_middleware", None)
    if mw is None:
        return "admin", "chat"
    return mw.extract_identity(dict(handler.headers), _handler_peer_ip(handler))


def _can_read_payload(payload: dict, user_id: str, permission: Any) -> bool:
    if permission is None or permission.can_access_all_users:
        return True
    payload_user = payload.get("user_id", payload.get("metadata", {}).get("user_id", ""))
    return payload_user == user_id


# LLM: Control receipts are issued to an authenticated channel+user pair. Raw user ids can collide
# across providers, so ordinary callers must match both facts; only explicit all-user permission
# may cross this boundary.
# 函数用途: 校验当前 HTTP 身份是否可以读取指定控制操作回执。
def _can_read_control_operation(handler, receipt) -> bool:
    user_id, channel = _request_channel(handler)
    _permission_user_id, permission = _request_identity(handler)
    if permission is None or bool(getattr(permission, "can_access_all_users", False)):
        return True
    headers = getattr(handler, "headers", {})
    conversation_id = str(headers.get("X-Conversation-Id") or "").strip()
    chat_type = str(headers.get("X-Channel-Chat-Type") or "").strip().lower()
    chat_id = str(headers.get("X-Channel-Chat-Id") or "").strip()
    return (
        receipt.user_id == user_id
        and receipt.channel == channel
        and receipt.conversation_id == conversation_id
        and receipt.channel_chat_type == chat_type
        and receipt.channel_chat_id == chat_id
    )


def handle_status(handler, server) -> None:
    if server is None:
        handler._send_json(500, {"error": "server not initialized"})
        return
    state, state_load_error = _read_state_report(server.paths.state)
    counts = gateway_request_counts(server.paths, include_archives=False)
    response = {
        "status": state.get("status", "unknown"),
        "pid": state.get("pid"),
        "uptime": time.time() - state.get("started_at", time.time()),
        "requests": counts,
        "runtime_prefix": str(state.get("runtime_prefix") or ""),
    }
    if state_load_error:
        response["state_load_error"] = state_load_error
    if isinstance(state.get("server_error"), dict):
        response["server_error"] = state["server_error"]
    # 安全重启排空阶段（turn_wait/tool_drain/cancelled）只做展示投影，状态源在 restart_service。
    if isinstance(state.get("restart_drain"), dict):
        response["restart_drain"] = state["restart_drain"]
    handler._send_json(200, response)


def _read_state(state_path) -> dict:
    state, _ = _read_state_report(state_path)
    return state


def _read_state_report(state_path) -> tuple[dict, dict[str, Any] | None]:
    if not state_path.exists():
        return {}, None
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        return {}, _state_load_error(state_path, exc)
    if isinstance(payload, dict):
        return payload, None
    return {}, _state_load_error(
        state_path,
        ValueError(f"gateway state JSON root is {type(payload).__name__}, expected object"),
    )


def _state_load_error(state_path, exc: BaseException) -> dict[str, Any]:
    report = runtime_error_report(exc, context="gateway.http_state.read")
    report["path"] = str(state_path)
    return report


def handle_result(handler, server) -> None:
    request_id = handler.path[len("/result/"):]
    if server is None:
        handler._send_json(500, {"error": "server not initialized"})
        return
    if not request_id or request_id != request_id.replace("/", "").replace("\\", ""):
        handler._send_json(400, {"error": "invalid request id"})
        return
    user_id, permission = _request_identity(handler)
    access = _ResultAccessContext(request_id, user_id, permission)
    if _send_archived_terminal_result(
        handler,
        server.paths,
        access,
        repair_response_projection=True,
    ):
        return
    if _send_pending_state(handler, server.paths.processing, "processing", access):
        return
    if _send_pending_state(handler, server.paths.inbox, "queued", access):
        return
    # 终态可能在两次热队列检查之间提交，再读一次 canonical；独立 response
    # 或 done/failed 投影永远不能把请求提升为完成。
    if _send_archived_terminal_result(
        handler,
        server.paths,
        access,
        repair_response_projection=True,
    ):
        return
    handler._send_json(404, {"error": "not found", "request_id": request_id})


# LLM: Input delivery status is authorized from the stored authenticated prepared request, not
# caller-supplied owner fields. It exposes the stable ingress state without creating a model job.
# 函数用途: 返回一条普通消息是在活动回合等待、已消费、已排队还是终态未知。
def handle_input_status(handler, server) -> None:
    request_id = handler.path[len("/input-status/") :]
    if server is None:
        handler._send_json(500, {"error": "server not initialized"})
        return
    if not request_id or request_id != request_id.replace("/", "").replace("\\", ""):
        handler._send_json(400, {"error": "invalid request id"})
        return
    try:
        receipt = read_gateway_input_receipt(server.paths, request_id)
    except DataCorruptionError:
        handler._send_json(
            503,
            {"error": "input delivery status is unavailable", "request_id": request_id},
        )
        return
    if receipt is None:
        handler._send_json(404, {"error": "not found", "request_id": request_id})
        return
    user_id, permission = _request_identity(handler)
    if not _can_read_payload(receipt.prepared_request, user_id, permission):
        handler._send_json(403, {"error": "forbidden", "request_id": request_id})
        return
    handler._send_json(200, gateway_input_status_payload(receipt))


# LLM: Terminal archives contain the canonical full response. This lazy repair makes /result survive
# a crash after archive commit but before the response projection, without rerunning the agent.
# 函数用途: 从完成或失败归档恢复最终答复，并补写缺失的 response 文件。
def _send_archived_terminal_result(
    handler,
    paths,
    access: _ResultAccessContext,
    *,
    repair_response_projection: bool = False,
) -> bool:
    archive_path = paths.terminal / f"{access.request_id}.json"
    if not archive_path.exists():
        return False
    terminal_report = read_gateway_terminal_envelope_report(
        archive_path,
        request_id=access.request_id,
        context="gateway.http_terminal_archive.read",
    )
    payload = terminal_report.payload
    if terminal_report.load_error is not None:
        status = 500 if _all_user_access(access) else 403
        body = {"error": "terminal result unavailable", "request_id": access.request_id}
        if _all_user_access(access):
            body["result_load_error"] = terminal_report.load_error
        handler._send_json(status, body)
        return True
    if not _can_read_payload(payload, access.user_id, access.permission):
        handler._send_json(403, {"error": "forbidden", "request_id": access.request_id})
        return True
    terminal_response = payload.get("terminal_response")
    if not isinstance(terminal_response, dict) or not terminal_response:
        handler._send_json(
            500,
            {"error": "terminal response is missing", "request_id": access.request_id},
        )
        return True
    response_path = paths.responses / f"{access.request_id}.json"
    try:
        if repair_response_projection or not response_path.exists():
            write_json_file_atomic(response_path, terminal_response)
    except OSError:
        # The canonical archive is already durable; serving it remains safe
        # even if this best-effort projection cannot be repaired now.
        pass
    result = terminal_response if _all_user_access(access) else _public_result(terminal_response)
    handler._send_json(200, result)
    return True


# LLM: progress 读取沿用 result 的请求 owner 鉴权；chunk 正文和 response 都不能自证身份。
# 函数用途: 按行游标返回当前 request 已启用的 typed 工具进度。
def handle_progress(handler, server) -> None:
    parsed = urlsplit(handler.path)
    request_id = parsed.path[len("/progress/") :]
    if server is None:
        handler._send_json(500, {"error": "server not initialized"})
        return
    if not request_id or request_id != request_id.replace("/", "").replace("\\", ""):
        handler._send_json(400, {"error": "invalid request id"})
        return
    user_id, permission = _request_identity(handler)
    access = _ResultAccessContext(request_id, user_id, permission)
    if not _can_read_finished_request(server.paths, access):
        handler._send_json(403, {"error": "forbidden", "request_id": request_id})
        return
    raw_since = parse_qs(parsed.query).get("since", ["0"])[0]
    try:
        since = max(0, int(raw_since))
    except (TypeError, ValueError):
        handler._send_json(400, {"error": "since must be a non-negative integer"})
        return
    chunk_path = gateway_chunk_path(server.paths, request_id)
    actual_path = next((path for path in gateway_chunk_path_candidates(chunk_path) if path.exists()), None)
    if actual_path is None:
        handler._send_json(
            200,
            {"request_id": request_id, "events": [], "next": since},
        )
        return
    events, next_cursor = _read_public_progress_events(
        actual_path, since, channel=_request_channel(handler)[1]
    )
    handler._send_json(
        200,
        {"request_id": request_id, "events": events, "next": next_cursor},
    )


def _read_public_progress_events(
    path, since: int, *, channel: str = ""
) -> tuple[list[dict[str, object]], int]:
    try:
        # JSONL 记录边界只能是物理 LF：splitlines() 会在 U+0085/U+2028/U+2029 等合法正文字符处切开记录。
        lines = jsonl_lines(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError):
        return [], since
    events: list[dict[str, object]] = []
    for line in lines[since : since + 200]:
        try:
            row = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(row, dict):
            continue
        if row.get("kind") == "assistant_commentary":
            # Model commentary is safe at every verbose level only after the
            # same user-facing projection used by final channel delivery.
            # 本机私有通道保留宿主绝对路径（用户要能直接复制），外部通道仍收敛成 basename。
            text = project_host_paths_for_channel(
                project_user_reply(row.get("text", "")).content,
                channel,
            ).strip()
            if text:
                events.append({"kind": "assistant_commentary", "text": text})
            continue
        if row.get("kind") != "tool_progress":
            continue
        level = str(row.get("verbose_level") or "off")
        progress = row.get("progress")
        if level not in {"on", "full"} or not isinstance(progress, dict):
            continue
        events.append({"level": level, **progress})
    return events, min(len(lines), since + 200)


def _send_pending_state(handler, folder, status: str, access: _ResultAccessContext) -> bool:
    request_path = folder / f"{access.request_id}.json"
    if not request_path.exists():
        return False
    payload, request_load_error = _read_payload_report(request_path, context="gateway.http_pending.read")
    if request_load_error and not _all_user_access(access):
        # 请求记录损坏时无法证明 owner；普通用户必须 fail-closed，不能顺带拿到
        # load report 中的服务器路径。管理员仍保留完整诊断视图。
        handler._send_json(403, {"error": "forbidden", "request_id": access.request_id})
        return True
    if payload and not _can_read_payload(payload, access.user_id, access.permission):
        handler._send_json(403, {"error": "forbidden", "request_id": access.request_id})
        return True
    response = {"status": status, "request_id": access.request_id}
    if request_load_error:
        response["request_load_error"] = request_load_error
    handler._send_json(202, response)
    return True


def _all_user_access(access: _ResultAccessContext) -> bool:
    return access.permission is None or access.permission.can_access_all_users


_PUBLIC_RESULT_FIELDS = (
    "id",
    "request_id",
    "kind",
    "ok",
    "status",
    "created_at",
    "started_at",
    "ended_at",
    "duration_seconds",
    "response",
    "error_code",
    "user_error",
    "backend",
    "used_memories",
    "tool_rounds",
    "attempts",
    "current_context_token_estimate",
    "prompt_token_estimate",
    "runtime_injection_token_estimate",
    "turn_token_estimate",
    "cumulative_token_estimate",
    "model_accounted_input_tokens",
    "model_output_tokens",
    "model_total_tokens",
    "model_cached_input_tokens",
    "model_cache_creation_input_tokens",
    "model_provider_usage_call_count",
    "model_estimated_usage_call_count",
    # LLM: 公开结果保留 provider/estimated 原始分栏，客户端不得从兼容总数反推计费真值。
    "model_usage_breakdown",
    "memory_resume_context_injected",
    "memory_resume_context_matches",
    "memory_resume_context_token_estimate",
    "conversation_persist_degraded",
)


# LLM: USER 的 HTTP result 是对内部 response record 的白名单投影；新增运行字段
# 默认不公开，但无正文的模型 token/cache 计数可供当前 owner 成本观测。
# 函数用途: 保留客户端需要的状态/正文/计数，同时移除路径、lease、prompt 和内部错误细节。
def _public_result(result: dict) -> dict[str, object]:
    # 操作核验只转发已清洗的公开投影，不在 HTTP 层重建 call、路径或副作用引用。
    public = {key: result[key] for key in _PUBLIC_RESULT_FIELDS if key in result}
    delivery = result.get("channel_delivery")
    if isinstance(delivery, dict):
        public["channel_delivery"] = {
            key: delivery[key]
            for key in (
                "content",
                "artifact_names",
                "internal_signal",
                "projection_status",
                "operation_verification",
            )
            if key in delivery
        }
    if result.get("ok") is False and result.get("error_code"):
        public["error"] = str(public.get("user_error") or "任务处理失败，请稍后重试。")
    return public


def _can_read_finished_request(paths, access: _ResultAccessContext) -> bool:
    """Authorize a finished response from its request record, never its output body.

    Request identity stays authoritative while the record moves from the hot queue
    into done/failed. Missing, unreadable, or conflicting copies therefore deny a
    USER by default; trusted admin callers keep their existing all-user access.
    """
    if _all_user_access(access):
        return True
    found_request = False
    for folder in (paths.processing, paths.inbox, paths.terminal, paths.done, paths.failed):
        request_path = folder / f"{access.request_id}.json"
        if not request_path.exists():
            continue
        found_request = True
        payload, load_error = _read_payload_report(
            request_path,
            context="gateway.http_result_request.read",
        )
        if load_error or not _can_read_payload(payload, access.user_id, access.permission):
            return False
    return found_request


def _read_payload(path) -> dict:
    payload, _ = _read_payload_report(path, context="gateway.http_payload.read")
    return payload


def _read_payload_report(path, *, context: str) -> tuple[dict, dict[str, Any] | None]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        return {}, _payload_load_error(path, exc, context)
    if isinstance(payload, dict):
        return payload, None
    return {}, _payload_load_error(
        path,
        ValueError(f"gateway payload JSON root is {type(payload).__name__}, expected object"),
        context,
    )


def _payload_load_error(path, exc: BaseException, context: str) -> dict[str, Any]:
    report = runtime_error_report(exc, context=context)
    report["path"] = str(path)
    return report


# LLM: 鉴权之后按原 owner 读取插件目录并核对提交版本，不进入持久控制或消息队列；普通请求仍沿原作用域绑定。
# 函数用途: 接收消息与系统命令，避免参数错误成为活动插话或模型任务。
def handle_ask(handler, server, request_id_factory: Callable[[], str]) -> None:
    if require_trusted_source(handler):
        return  # 不可信来源(远程无 token)拒绝派工:已发 403(回环本机/单机放行,渠道用户经适配器可提交)
    try:
        body = handler._read_json()
    except json.JSONDecodeError as exc:
        handler._send_json(400, {"error": f"invalid JSON: {exc}"})
        return
    body = dict(body)
    body.pop("system_task", None)
    kind = body.get("kind", "ask")
    if kind == "session_lifecycle":
        _handle_session_lifecycle_signal(
            handler,
            server,
            body,
            user_id=_request_channel(handler)[0],
            channel=_request_channel(handler)[1],
        )
        return
    if kind != "ask":
        handler._send_json(400, {"error": f"unsupported kind: {kind}"})
        return
    goal = str(body.get("goal", body.get("prompt", "")) or "")
    if not goal:
        handler._send_json(400, {"error": "goal is required"})
        return
    if server is None:
        handler._send_json(500, {"error": "server not initialized"})
        return
    user_id, channel = _request_channel(handler)
    if plugin_namespace(goal) is not None:
        plugin_result = plugin_http_response(handler, server, body, text=goal)
        handler._send_json(200, {**plugin_result, "status": "control", "disposition": "system_command"})
        return
    task_command = parse_conversation_task_command(goal)
    if task_command is not None and not task_command.valid:
        handler._send_json(
            200,
            {
                "kind": task_command.kind,
                "ok": False,
                "message": task_command.usage,
                "request_id": "",
                "status": "control",
                "disposition": "system_command",
            },
        )
        return
    if task_command is None:
        command = parse_conversation_control(goal, reject_unknown_slash=True)
        if command is not None:
            if server.agent is None:
                handler._send_json(500, {"error": "server control service not initialized"})
                return
            if not command.valid:
                payload = ConversationControlResult(
                    command.kind,
                    False,
                    command.usage,
                ).to_dict()
                payload.update({"status": "control", "disposition": "system_command"})
                handler._send_json(200, payload)
                return
            _handle_persistent_control_operation(
                handler,
                server,
                command=command,
                command_text=goal,
                scope=_gateway_control_scope(
                    handler,
                    body,
                    user_id=user_id,
                    channel=channel,
                ),
            )
            return
        request_id, client_input_digest = _http_idempotent_request_identity(
            body,
            goal=goal,
            user_id=user_id,
            channel=channel,
        )
        if request_id:
            _handle_idempotent_ordinary_ask(
                handler,
                server,
                body=body,
                goal=goal,
                user_id=user_id,
                channel=channel,
                request_id=request_id,
                client_input_digest=client_input_digest,
                allow_active_turn=True,
            )
            return
    else:
        body = dict(body)
        body["system_task"] = task_command.to_request_payload()
        goal = task_command.prompt
    request_id, client_input_digest = _http_idempotent_request_identity(
        body,
        goal=goal,
        user_id=user_id,
        channel=channel,
    )
    if request_id:
        _handle_idempotent_ordinary_ask(
            handler,
            server,
            body=body,
            goal=goal,
            user_id=user_id,
            channel=channel,
            request_id=request_id,
            client_input_digest=client_input_digest,
            allow_active_turn=False,
        )
        return
    request_id = request_id_factory()
    request_data = _build_ask_request(_AskRequestContext(body, goal, request_id, user_id, channel))
    try:
        pending_path = server.paths.inbox / f"{request_id}.json"
        pending_path.write_text(
            json.dumps(request_data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        from ..observability.concurrency_metrics import gateway_request_enqueued

        gateway_request_enqueued()
    except OSError as exc:
        handler._send_json(500, {"error": f"failed to write request: {exc}"})
        return
    handler._send_json(202, {"request_id": request_id, "status": "queued"})


# LLM: One stable ingress receipt decides active-turn versus queued disposition before any retry
# reruns routing. The receipt lock is outside the exact active-turn lock, giving one lock order.
# 函数用途: 幂等处理普通消息，在当前回合补充和下一轮排队之间只选择一次并可断线恢复。
def _handle_idempotent_ordinary_ask(
    handler,
    server,
    *,
    body: dict,
    goal: str,
    user_id: str,
    channel: str,
    request_id: str,
    client_input_digest: str,
    allow_active_turn: bool,
) -> None:
    routed_body = dict(body)
    metadata = routed_body.get("metadata")
    metadata = dict(metadata) if isinstance(metadata, dict) else {}
    client_message_id = str(metadata.get("message_id") or "").strip()
    metadata.update(
        {
            "client_input_digest": client_input_digest,
            "gateway_input_request_id": request_id,
        }
    )
    routed_body["metadata"] = metadata
    prepared_request = _build_ask_request(
        _AskRequestContext(routed_body, goal, request_id, user_id, channel)
    )
    scope = _gateway_control_scope(
        handler,
        routed_body,
        user_id=user_id,
        channel=channel,
    )
    guidance_key = active_turn_guidance_dedupe_key(scope)
    try:
        with gateway_input_transition(server.paths, request_id):
            receipt, created = load_or_prepare_gateway_input_locked(
                server.paths,
                request_id=request_id,
                client_input_digest=client_input_digest,
                client_message_id=client_message_id,
                guidance_dedupe_key=guidance_key,
                prepared_request=prepared_request,
            )
            if created and receipt.state == "pending" and allow_active_turn:
                active_turn = _route_ask_to_active_turn(
                    server,
                    body=routed_body,
                    goal=goal,
                    user_id=user_id,
                    channel=channel,
                )
                if active_turn is None or active_turn.delivery_status == "rejected":
                    receipt, _created = queue_gateway_input_locked(server.paths, receipt)
                else:
                    guidance_binding = None
                    binding_unreadable = False
                    try:
                        from .request_worker import _resolve_request_agent

                        owner_agent = _resolve_request_agent(
                            server.agent,
                            receipt.prepared_request,
                        )
                        guidance_binding = gateway_input_guidance_binding(
                            receipt,
                            owner_agent.conversation_store,
                        )
                    except Exception:
                        binding_unreadable = True
                    if guidance_binding is not None:
                        guidance, target_turn_id = guidance_binding
                        receipt = bind_gateway_input_active_locked(
                            server.paths,
                            receipt,
                            target_turn_id=target_turn_id,
                            guidance_dedupe_key=str(guidance.dedupe_key),
                        )
                    elif not binding_unreadable:
                        # A successful authoritative miss proves append never
                        # happened, so queueing cannot duplicate model input.
                        receipt, _created = queue_gateway_input_locked(
                            server.paths,
                            receipt,
                        )
            elif (created and receipt.state == "pending") or receipt.state == "queued":
                receipt, _created = queue_gateway_input_locked(server.paths, receipt)
    except ValueError as exc:
        handler._send_json(
            409,
            {
                "error": str(exc),
                "error_code": "IDEMPOTENCY_CONFLICT",
                "request_id": request_id,
            },
        )
        return
    except DataCorruptionError as exc:
        handler._send_json(
            503,
            {
                "error": "gateway input receipt is temporarily unavailable",
                "error_code": type(exc).__name__,
            },
        )
        return
    except OSError as exc:
        handler._send_json(500, {"error": f"failed to persist input disposition: {exc}"})
        return
    if receipt.state in {"pending", "active_pending", "terminal_unknown"}:
        try:
            from .request_worker import _resolve_request_agent

            owner_agent = _resolve_request_agent(server.agent, receipt.prepared_request)
            reconcile_gateway_input_request(
                server.paths,
                request_id=request_id,
                conversation_store=owner_agent.conversation_store,
                target_terminal=(receipt.state == "terminal_unknown"),
            )
            receipt = read_gateway_input_receipt(server.paths, request_id) or receipt
        except Exception:
            pass
    handler._send_json(202, gateway_input_status_payload(receipt))


# LLM: Lifecycle 请求只接受 close/reset 枚举且不进入模型队列；owner 仍由已认证 scope 解析。
# 函数用途: 接收渠道会话关闭/重置信号并登记统一 Curator durable reason。
def _handle_session_lifecycle_signal(
    handler,
    server,
    body: dict,
    *,
    user_id: str,
    channel: str,
) -> None:
    if server is None or getattr(server, "agent", None) is None:
        handler._send_json(500, {"error": "server memory curator is not initialized"})
        return
    event = str(body.get("event") or "").strip().lower()
    if event not in {"close", "reset"}:
        handler._send_json(400, {"error": "session lifecycle event must be close or reset"})
        return
    try:
        result = request_gateway_memory_curator_lifecycle(
            server.agent,
            scope=_gateway_control_scope(
                handler,
                body,
                user_id=user_id,
                channel=channel,
            ),
            event=event,
        )
    except Exception as exc:  # noqa: BLE001 - request failure must not enqueue a fake lifecycle event.
        handler._send_json(
            503,
            {
                "error": "memory curator lifecycle request failed",
                "error_code": type(exc).__name__,
            },
        )
        return
    payload = result.to_dict()
    payload.update({"status": "accepted", "disposition": "memory_curator_request"})
    handler._send_json(202, payload)


# LLM: The busy-turn decision is based only on authenticated scope plus durable runtime state;
# the prompt body is never inspected for task/chat intent.
# 函数用途：把同一 owner/thread 运行期间的新普通输入交给当前轮；无活跃轮时保持普通入队。
def _route_ask_to_active_turn(
    server,
    *,
    body: dict,
    goal: str,
    user_id: str,
    channel: str,
):
    agent = getattr(server, "agent", None)
    if agent is None:
        return None
    metadata = body.get("metadata")
    metadata = dict(metadata) if isinstance(metadata, dict) else {}
    return steer_active_conversation_if_running(
        agent,
        server.paths,
        message=goal,
        scope=GatewayControlScope(
            user_id=user_id,
            channel=channel,
            conversation_id=_http_conversation_id(body),
            metadata=metadata,
        ),
    )


# LLM: 插件参数不扩展旧 ControlKind；先经原可信来源和 owner 解析，再核对目录版本，业务控制仍持久幂等。
# 函数用途: 分别处理只读命令与原会话控制，不因新命名空间触发默认停止。
def handle_control(handler, server) -> None:
    if require_trusted_source(handler):
        return
    if server is None or server.agent is None:
        handler._send_json(500, {"error": "server control service not initialized"})
        return
    try:
        body = handler._read_json()
    except json.JSONDecodeError as exc:
        handler._send_json(400, {"error": f"invalid JSON: {exc}"})
        return
    command_text = str(body.get("command", body.get("prompt", "")) or "")
    if plugin_namespace(command_text) is not None:
        plugin_result = plugin_http_response(handler, server, body, text=command_text)
        handler._send_json(200, plugin_result)
        return
    command = parse_conversation_control(
        command_text,
        reject_unknown_slash=True,
    )
    if command is None:
        handler._send_json(400, {"error": "unsupported conversation control"})
        return
    if not command.valid:
        handler._send_json(
            200,
            ConversationControlResult(command.kind, False, command.usage).to_dict(),
        )
        return
    user_id, channel = _request_channel(handler)
    if getattr(handler, "_auth_middleware", None) is None:
        user_id = str(body.get("user_id") or user_id).strip()
        channel = str(body.get("channel") or channel).strip()
    if not _http_conversation_id(body):
        handler._send_json(400, {"error": "conversation_id is required"})
        return
    _handle_persistent_control_operation(
        handler,
        server,
        command=command,
        command_text=str(body.get("command", body.get("prompt", "")) or ""),
        scope=_gateway_control_scope(
            handler,
            body,
            user_id=user_id,
            channel=channel,
        ),
    )


# LLM: Both HTTP control entrypoints must prepare the same durable operation before invoking any
# command service. Response loss therefore replays the stored result instead of repeating effects.
# 函数用途: 持久、幂等地执行一条 HTTP 控制命令，并返回独立 operation_id 供客户端对账。
def _handle_persistent_control_operation(
    handler,
    server,
    *,
    command: ConversationControlCommand,
    command_text: str,
    scope: GatewayControlScope,
) -> None:
    try:
        receipt = execute_gateway_control_operation(
            server.agent,
            server.paths,
            command,
            scope,
            command_text=command_text,
        )
    except GatewayControlOperationIdentityRequired as exc:
        handler._send_json(
            400,
            {
                "error": str(exc),
                "error_code": "CONTROL_OPERATION_ID_REQUIRED",
            },
        )
        return
    except GatewayControlOperationConflict as exc:
        handler._send_json(
            409,
            {
                "error": str(exc),
                "error_code": "IDEMPOTENCY_CONFLICT",
            },
        )
        return
    except DataCorruptionError as exc:
        handler._send_json(
            503,
            {
                "error": str(exc),
                "error_code": "CONTROL_OPERATION_RECEIPT_CORRUPT",
            },
        )
        return
    except OSError as exc:
        handler._send_json(
            503,
            {
                "error": "failed to persist control operation",
                "error_code": type(exc).__name__,
            },
        )
        return
    payload = gateway_control_operation_status_payload(receipt)
    status = 202 if (
        receipt.state != "completed"
        or str(payload.get("delivery_status") or "") == "unknown"
    ) else 200
    handler._send_json(status, payload)


# LLM: Polling authenticates against the receipt owner before read-only reconciliation. A GET can
# refresh an existing `/btw` receipt but cannot execute any control effect.
# 函数用途: 返回一条持久控制操作的结果或投递三态，供 TUI、IM 和 Web 断线后继续对账。
def handle_control_status(handler, server) -> None:
    if require_trusted_source(handler):
        return
    if server is None or server.agent is None:
        handler._send_json(500, {"error": "server control service not initialized"})
        return
    operation_id = handler.path.split("?", 1)[0].removeprefix("/control-status/").strip()
    try:
        receipt = read_gateway_control_operation(server.paths, operation_id)
    except (DataCorruptionError, ValueError) as exc:
        handler._send_json(
            503,
            {
                "error": str(exc),
                "error_code": "CONTROL_OPERATION_RECEIPT_CORRUPT",
            },
        )
        return
    if receipt is None:
        handler._send_json(404, {"error": "control operation not found"})
        return
    if not _can_read_control_operation(handler, receipt):
        handler._send_json(403, {"error": "forbidden"})
        return
    try:
        receipt = reconcile_gateway_control_operation(
            server.agent,
            server.paths,
            operation_id,
        ) or receipt
    except (DataCorruptionError, OSError, ValueError) as exc:
        handler._send_json(
            503,
            {
                "error": str(exc),
                "error_code": "CONTROL_OPERATION_RECONCILE_FAILED",
            },
        )
        return
    handler._send_json(200, gateway_control_operation_status_payload(receipt))


# LLM: 可信 owner/thread 选择分页；前台消息与实时过程独立声明去重能力，不授予执行权或隐式升级旧页。
# 函数用途: 读取显式显示能力并投影消息/检查点/活动；旧客户端保持旧类型，控制授权仍独立。
def handle_client_notices(handler, server) -> None:
    """S-BG1: 返回当前会话的进行中任务数和后台主代理轮完成通知。

    body: {"conversation_id": ..., "after": canonical 消息字节位置,
           "event_after": 过程事件游标, "event_stream_id": 当前过程流身份}
    响应同时返回 notices/cursor 与 transcript_events/event_cursor 两条独立增量流。
    """
    if require_trusted_source(handler):
        return
    if server is None or server.agent is None:
        handler._send_json(500, {"error": "server client service not initialized"})
        return
    try:
        body = handler._read_json()
    except json.JSONDecodeError as exc:
        handler._send_json(400, {"error": f"invalid JSON: {exc}"})
        return
    user_id, channel = _request_channel(handler)
    if not _http_conversation_id(body):
        handler._send_json(400, {"error": "conversation_id is required"})
        return
    after = _safe_request_int(body.get("after"))
    try:
        event_after = max(0, int(body.get("event_after") or 0))
    except (TypeError, ValueError):
        event_after = 0
    event_stream_id = body.get("event_stream_id", "")
    if not isinstance(event_stream_id, str):
        handler._send_json(400, {"error": "event_stream_id must be a string"})
        return
    result = read_gateway_client_notices(
        server.agent,
        scope=_gateway_control_scope(
            handler,
            body,
            user_id=user_id,
            channel=channel,
        ),
        after=after,
        event_after=event_after,
        event_stream_id=event_stream_id,
        interactive_approvals=bool(
            isinstance(body.get("client_capabilities"), dict)
            and body["client_capabilities"].get("tool_approval") is True
        ),
        display=NoticeDisplayCapabilities.from_payload(body.get("client_capabilities")),
    )
    handler._send_json(200, result)


# LLM: View requests reuse trusted-source middleware and one owner-resolved
# GatewayControlScope. The base Gateway agent must never read a scoped user's run.
# 函数用途: 从当前用户自己的 Agent 返回一个子代理详情和增量过程事件。
def handle_client_agent_view(handler, server) -> None:
    body = _read_agent_control_body(handler, server)
    if body is None:
        return
    try:
        owner_agent, scope = _agent_control_context(handler, server, body)
        result = read_agent_view(
            owner_agent,
            scope=scope,
            run_id=str(body.get("run_id") or ""),
            after=max(0, _safe_request_int(body.get("event_after"))),
        )
    except AgentControlError as exc:
        _send_agent_control_error(handler, exc)
        return
    handler._send_json(200, result)


# LLM: Guidance accepts ordinary user text only through the exact owner's
# idempotent mailbox service. It cannot fall back to the base Gateway owner.
# 函数用途: 将补充消息排进当前用户会话树内的指定子代理。
def handle_client_agent_guidance(handler, server) -> None:
    body = _read_agent_control_body(handler, server)
    if body is None:
        return
    try:
        owner_agent, scope = _agent_control_context(handler, server, body)
        result = send_agent_guidance(
            owner_agent,
            scope=scope,
            run_id=str(body.get("run_id") or ""),
            message=str(body.get("message") or ""),
            message_id=str(body.get("message_id") or ""),
        )
    except AgentControlError as exc:
        _send_agent_control_error(handler, exc)
        return
    handler._send_json(202, result)


# LLM: Reuse authenticated client owner resolution, then the transport-neutral Goal controller; raw thread IDs are not accepted.
# 函数用途: 接收目标查看与明确保存操作，不在 HTTP 线程调用模型或改变权限。
def handle_client_goal(handler, server) -> None:
    from ..conversation.goal_control import execute_agent_goal_control

    body = _read_agent_control_body(handler, server)
    if body is None:
        return
    try:
        owner_agent, scope = _agent_control_context(handler, server, body)
        result = execute_agent_goal_control(owner_agent, scope=scope, payload=body)
    except AgentControlError as exc:
        _send_agent_control_error(handler, exc)
        return
    handler._send_json(200, result)


# LLM: 主/子审批仅向认证 owner 的原服务传递 mapping；请求格式或 owner 解析失败时不得写耐久记录。
# 函数用途: 把 TUI/Web 的主/子后台工具审批决定写回当前用户的原始等待调用。
def handle_client_agent_permission(handler, server) -> None:
    body = _read_agent_control_body(handler, server)
    if body is None:
        return
    request = body.get("request")
    decision = body.get("decision")
    if not isinstance(request, dict) or not isinstance(decision, dict):
        handler._send_json(400, {"error": "request and decision are required"})
        return
    try:
        owner_agent, scope = _agent_control_context(handler, server, body)
        result = resolve_agent_permission(
            owner_agent,
            scope=scope,
            run_id=str(body.get("run_id") or ""),
            request=request,
            decision=decision,
        )
    except AgentControlError as exc:
        _send_agent_control_error(handler, exc)
        return
    handler._send_json(200, result)


# LLM: Stop accepts one exact run and opaque operation id after resolving the exact owner. The
# transport returns 202 once the owner-scoped worker is admitted; canonical cancellation remains
# asynchronous so slow projection flushes cannot turn a successful Esc into a false timeout.
# 函数用途: 快速接收当前用户对子代理的停止请求，终态由同一底层取消服务后台收口。
def handle_client_agent_stop(handler, server) -> None:
    body = _read_agent_control_body(handler, server)
    if body is None:
        return
    try:
        owner_agent, scope = _agent_control_context(handler, server, body)
        result = enqueue_agent_stop(
            owner_agent,
            scope=scope,
            run_id=str(body.get("run_id") or ""),
            operation_id=str(body.get("operation_id") or ""),
        )
    except AgentControlError as exc:
        _send_agent_control_error(handler, exc)
        return
    handler._send_json(202 if result.get("status") == "accepted" else 200, result)


# LLM: All agent-control endpoints share identical transport validation;
# parsing failure or unavailable service ends the request before scope resolution.
# 函数用途: 读取并校验一条子代理控制 JSON 请求。
def _read_agent_control_body(handler, server) -> dict[str, object] | None:
    if require_trusted_source(handler):
        return None
    if server is None or server.agent is None:
        handler._send_json(500, {"error": "server client service not initialized"})
        return None
    try:
        body = handler._read_json()
    except json.JSONDecodeError as exc:
        handler._send_json(400, {"error": f"invalid JSON: {exc}"})
        return None
    if not _http_conversation_id(body):
        handler._send_json(400, {"error": "conversation_id is required"})
        return None
    return body


# LLM: All delegated-agent HTTP operations must resolve the authenticated owner
# once, then pass that exact Agent and the unchanged scope through view/mutation.
# Resolution failure is fail-closed and can never fall back to server.agent.
# 函数用途: 为查看、插话、审批和停止统一找到当前用户自己的 Agent 与会话作用域。
def _agent_control_context(
    handler,
    server,
    body: dict[str, object],
) -> tuple[object, GatewayControlScope]:
    user_id, channel = _request_channel(handler)
    scope = _gateway_control_scope(
        handler,
        body,
        user_id=user_id,
        channel=channel,
    )
    try:
        owner_agent = resolve_gateway_scope_agent(server.agent, scope)
    except Exception as exc:
        raise AgentControlError(
            503,
            "AGENT_OWNER_UNAVAILABLE",
            "当前用户的子代理状态暂时不可用。",
        ) from exc
    if owner_agent is None:
        raise AgentControlError(
            503,
            "AGENT_OWNER_UNAVAILABLE",
            "当前用户的子代理状态暂时不可用。",
        )
    return owner_agent, scope


# LLM: Integer coercion is used only for display pagination and cannot affect
# run identity, permission, or lifecycle transitions.
# 函数用途: 将过程事件游标安全转换成非负整数。
def _safe_request_int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


# LLM: Public control errors expose stable status/code/message fields only;
# nested exceptions and filesystem details never cross HTTP.
# 函数用途: 将子代理控制服务拒绝转换成结构化响应。
def _send_agent_control_error(handler, error: AgentControlError) -> None:
    handler._send_json(
        max(400, int(error.status or 500)),
        {
            "ok": False,
            "error_code": str(error.error_code or "AGENT_CONTROL_FAILED"),
            "error": str(error.message or "agent control failed"),
        },
    )


# LLM: The canonical thread owns task candidates while each typed task link
# owns its lifecycle. The response adds a bounded direct-child display
# projection from canonical runs; neither notices nor that projection can
# create task state, authorize work, retry, or decide completion. Process-stream identity is
# echoed with its event sequence, independently from the durable message byte cursor.
# 函数用途: 按显式能力开放正文、前台流及完整检查点；旧页跳过新类型，轮询不初始化owner。
def read_gateway_client_notices(
    agent: object,
    *,
    scope: GatewayControlScope,
    after: int,
    event_after: int = 0,
    event_stream_id: str = "",
    interactive_approvals: bool = False,
    display: NoticeDisplayCapabilities | None = None,
) -> dict[str, object]:
    """返回当前 thread 的活动投影、后台过程事件与 after 之后的通知。"""
    from ..conversation.store import ConversationStore

    display = display if display is not None else NoticeDisplayCapabilities()
    conversation_id = str(getattr(scope, "conversation_id", "") or "").strip()
    if not conversation_id:
        return _empty_gateway_notice_response(
            after,
            event_after,
            error="conversation_id required",
        )
    try:
        owner_agent = resolve_loaded_gateway_scope_agent(agent, scope)
    except Exception:
        return _empty_gateway_notice_response(
            after,
            event_after,
            error="owner scope unavailable",
        )
    if owner_agent is None:
        return _read_cold_owner_notice_projection(
            agent,
            scope=scope,
            after=after,
            event_after=event_after,
            event_stream_id=event_stream_id,
            display=display,
        )
    store = getattr(owner_agent, "conversation_store", None)
    if not isinstance(store, ConversationStore):
        return _empty_gateway_notice_response(
            after,
            event_after,
            error="conversation store unavailable",
        )
    try:
        thread, _error = store.threads.resolve_report(
            channel=str(scope.channel or "").strip(),
            channel_conversation_id=conversation_id,
            channel_user_id=str(scope.user_id or "").strip(),
        )
    except Exception as exc:
        return _empty_gateway_notice_response(
            after,
            event_after,
            error=f"thread resolve failed: {exc}",
        )
    if thread is None:
        return _empty_gateway_notice_response(
            after,
            event_after,
            ok=True,
            event_stream_id=event_stream_id,
            agent_activity=ConversationAgentActivity().to_dict(),
        )
    thread_id = str(getattr(thread, "thread_id", "") or "")
    permission_requests = _gateway_agent_permission_requests(
        owner_agent,
        thread,
        interactive_approvals=interactive_approvals,
    )
    transcript = read_background_transcript_events(
        owner_agent,
        thread_id=thread_id,
        after=event_after,
        stream_id=event_stream_id,
    )
    activity = conversation_agent_activity(owner_agent, store, thread_id)
    activity_payload = activity.to_dict()
    active_task_count = activity.active_task_count
    notices, cursor, notices_ok = read_background_response_page(
        store, thread_id, after=after, include_foreground=display.foreground_messages,
        include_display_checkpoints=display.display_checkpoints,
    )
    payload = {
        "ok": notices_ok,
        "notices": notices,
        "cursor": cursor,
        "transcript_events": [row for row in transcript["events"] if display.foreground_transcript or not row.get("gateway_request_id")],
        "event_cursor": transcript["cursor"],
        "event_stream_id": transcript["stream_id"],
        "events_truncated": transcript["truncated"],
        "active_task_count": active_task_count,
        "agent_activity": activity_payload,
        "agent_permission_requests": permission_requests,
    }
    if not notices_ok:
        payload["error"] = "notices read failed"
    return payload


# LLM: A cold owner must remain cold, but committed assistant notices cannot disappear after a
# Gateway restart. Resolve one authenticated owner home, inspect only the finite canonical store
# layouts, and replay the newest exact channel binding without constructing Agent/backend/tool
# state. Volatile activity and approval state intentionally remain empty until the owner is loaded;
# an echoed stream cursor keeps the client unchanged until a new real stream exists.
# 函数用途: 冷用户直接读取已提交正文/完整过程，保持能力声明，不因恢复历史拉起整套Agent。
def _read_cold_owner_notice_projection(
    base_agent: object,
    *,
    scope: GatewayControlScope,
    after: int,
    event_after: int,
    event_stream_id: str,
    display: NoticeDisplayCapabilities,
) -> dict[str, object]:
    from ..conversation.store import ConversationStore
    from ..owner_wake_discovery import existing_conversation_store_roots
    from ..user_space.owner_resolver import resolve_owner_home

    idle_activity = ConversationAgentActivity().to_dict()
    payload = _empty_gateway_notice_response(
        after,
        event_after,
        ok=True,
        event_stream_id=event_stream_id,
        agent_activity=idle_activity,
    )
    payload["owner_state"] = "cold"
    home_paths = getattr(base_agent, "home_paths", None)
    home_root = getattr(home_paths, "root", None)
    if home_root is None:
        return payload
    try:
        owner = resolve_gateway_scope_owner(base_agent, scope)
        owner_home = resolve_owner_home(home_root, owner).home_dir
    except Exception:
        return payload

    candidates: list[tuple[float, str, object, object]] = []
    for store_root in existing_conversation_store_roots(owner_home):
        try:
            store = ConversationStore(store_root, initialize=False)
            thread, load_error = store.threads.resolve_report(
                channel=str(scope.channel or "").strip(),
                channel_conversation_id=str(scope.conversation_id or "").strip(),
                channel_user_id=str(scope.user_id or "").strip(),
            )
        except Exception:
            continue
        if thread is None or load_error is not None:
            continue
        candidates.append(
            (
                float(getattr(thread, "updated_at", 0.0) or 0.0),
                str(store_root),
                store,
                thread,
            )
        )
    if not candidates:
        return payload

    _updated_at, _root_key, store, thread = max(candidates, key=lambda item: item[:2])
    thread_id = str(getattr(thread, "thread_id", "") or "").strip()
    notices, cursor, notices_ok = read_background_response_page(
        store, thread_id, after=after, include_foreground=display.foreground_messages,
        include_display_checkpoints=display.display_checkpoints,
    )
    if not notices_ok:
        payload["ok"] = False
        payload["error"] = "notices read failed"
        return payload
    payload["notices"] = notices
    payload["cursor"] = cursor
    return payload


# LLM: Empty/error snapshots share one stable transport shape so clients never
# infer missing approval/activity fields. Empty projections echo the process cursor without
# claiming a new stream or creating task state.
# 函数用途: 构造无线程或读取失败时的空后台快照。
def _empty_gateway_notice_response(
    after: int,
    event_after: int,
    *,
    ok: bool = False,
    event_stream_id: str = "",
    error: str = "",
    agent_activity: Mapping[str, object] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "ok": bool(ok),
        "notices": [],
        "cursor": after,
        "transcript_events": [],
        "event_cursor": max(0, int(event_after or 0)),
        "event_stream_id": event_stream_id,
        "events_truncated": False,
        "active_task_count": 0,
        "agent_permission_requests": [],
    }
    if error:
        payload["error"] = error
    if agent_activity is not None:
        payload["agent_activity"] = dict(agent_activity)
    return payload


# LLM: 仅显式声明 tool_approval 的客户端续租接收能力；返回行是当前主任务树的只读投影，不批准任何调用。
# 函数用途: 为当前主任务树续接交互审批接收能力，并返回主/子后台等待请求。
def _gateway_agent_permission_requests(
    agent: object,
    thread: object,
    *,
    interactive_approvals: bool,
) -> list[dict[str, object]]:
    if not interactive_approvals:
        return []
    root_task_id = str(getattr(thread, "workspace_task_id", "") or "").strip()
    if not root_task_id:
        return []
    from ..conversation.agent_tool_approval import (
        list_pending_agent_tool_approvals,
        renew_agent_tool_approval_consumer,
    )

    try:
        if interactive_approvals:
            renew_agent_tool_approval_consumer(
                agent,
                root_task_id=root_task_id,
            )
        return list_pending_agent_tool_approvals(
            agent,
            root_task_id=root_task_id,
        )
    except (FileNotFoundError, OSError, RuntimeError, TypeError, ValueError):
        return []




def handle_client_memory(handler, server) -> None:
    if require_trusted_source(handler):
        return
    if server is None or server.agent is None:
        handler._send_json(500, {"error": "server client service not initialized"})
        return
    try:
        body = handler._read_json()
    except json.JSONDecodeError as exc:
        handler._send_json(400, {"error": f"invalid JSON: {exc}"})
        return
    user_id, channel = _request_channel(handler)
    if not _http_conversation_id(body):
        handler._send_json(400, {"error": "conversation_id is required"})
        return
    result = execute_gateway_client_memory(
        server.agent,
        scope=_gateway_control_scope(
            handler,
            body,
            user_id=user_id,
            channel=channel,
        ),
        operation=str(body.get("operation") or ""),
        query=str(body.get("query") or ""),
        content=str(body.get("content") or ""),
        kind=str(body.get("kind") or "fact"),
        limit=_request_limit(body, default=5),
    )
    handler._send_json(200, result.to_dict())


# LLM: 历史 API 只读取 authenticated owner/thread；可选字节游标不能变成路径、身份或无界查询。
# 函数用途: 返回最近或更早的一页聊天历史，非法游标显式拒绝而不是回到最新页。
def handle_client_history(handler, server) -> None:
    if require_trusted_source(handler):
        return
    if server is None or server.agent is None:
        handler._send_json(500, {"error": "server client service not initialized"})
        return
    try:
        body = handler._read_json()
    except json.JSONDecodeError as exc:
        handler._send_json(400, {"error": f"invalid JSON: {exc}"})
        return
    user_id, channel = _request_channel(handler)
    if not _http_conversation_id(body):
        handler._send_json(400, {"error": "conversation_id is required"})
        return
    before = body.get("before_message_cursor")
    if before is not None and (isinstance(before, bool) or not isinstance(before, int) or before < 0):
        handler._send_json(400, {"error": "invalid history cursor"})
        return
    result = read_gateway_client_history(
        server.agent,
        scope=_gateway_control_scope(
            handler,
            body,
            user_id=user_id,
            channel=channel,
        ),
        max_turns=_request_limit(body, default=20),
        before_message_cursor=before,
    )
    handler._send_json(200, result.to_dict())


# LLM: 前端数量只作为资源上限，非法值不能变成无限查询或改变 owner 路由。
# 函数用途: 从 HTTP JSON 中读取有界正整数条数。
def _request_limit(body: dict[str, Any], *, default: int) -> int:
    try:
        return max(1, min(200, int(body.get("limit") or default)))
    except (TypeError, ValueError):
        return default


# LLM: 身份由认证中间件决定；workspace 原样进入版本化控制回执，具体路径由命令服务共用权限门校验。
# 函数用途: 投影 HTTP 控制作用域，保留显式非法目录输入以返回错误，不静默改为默认目录。
def _gateway_control_scope(
    handler,
    body: dict,
    *,
    user_id: str,
    channel: str,
) -> GatewayControlScope:
    permission_user_id, permission = _request_identity(handler)
    if getattr(handler, "_auth_middleware", None) is None:
        user_id = str(body.get("user_id") or user_id).strip()
        channel = str(body.get("channel") or channel).strip()
    metadata = body.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    return GatewayControlScope(
        user_id=user_id,
        channel=channel,
        conversation_id=_http_conversation_id(body),
        metadata=dict(metadata),
        workspace=(body.get("workspace") if body.get("workspace") is not None else {})
        if "workspace" in body else None,
        all_user_access=(
            permission is None
            or bool(getattr(permission, "can_access_all_users", False))
        )
        and permission_user_id == user_id,
    )


# LLM: HTTP ingress preserves typed conversation, workspace and execution options without
# validating host paths here; the shared worker gate is the one filesystem authority.
# 函数用途: 将 HTTP 请求整理成和文件队列客户端一致的 Gateway ask 载荷。
def _build_ask_request(context: _AskRequestContext) -> dict:
    raw_metadata = context.body.get("metadata")
    metadata = dict(raw_metadata) if isinstance(raw_metadata, dict) else {}
    metadata["user_id"] = context.user_id
    metadata["channel"] = context.channel
    payload = {
        "id": context.request_id,
        "request_id": context.request_id,
        "kind": "ask",
        "goal": context.goal,
        "metadata": metadata,
        "priority": "interactive",
        "source": f"http:{context.channel}",
        "submitted_at": time.time(),
        # 进队时刻(§6-A 排队等待直方图的起点):/ask 直写 inbox 不经 write_gateway_request,
        # 曾缺此字段致 queue_wait 探针在 HTTP 主路径恒 0 样本(压测实锤)。
        "created_at": time.time(),
        "user_id": context.user_id,
        "conversation": _http_conversation_payload(context),
    }
    system_task = context.body.get("system_task")
    if isinstance(system_task, dict):
        payload["system_task"] = dict(system_task)
    if "workspace" in context.body:
        raw_workspace = context.body.get("workspace")
        payload["workspace"] = (
            dict(raw_workspace)
            if isinstance(raw_workspace, dict)
            else {"invalid_payload_type": type(raw_workspace).__name__}
        )
    if _ask_body_has_execution_options(context.body):
        payload.update(GatewayAskExecutionOptions.from_payload(context.body).to_payload())
    return payload


# LLM: Legacy HTTP/IM callers may omit all execution options, but once any option is supplied the
# complete normalized snapshot is persisted so active-to-queued fallback cannot depend on defaults
# from a later process.
# 函数用途: 判断 `/ask` 是否显式携带执行选项或媒体 refs；引用仍须经过 owner 校验。
def _ask_body_has_execution_options(body: dict[str, Any]) -> bool:
    return any(
        key in body
        for key in (
            "inject",
            "prompt_files",
            "input_media",
            "save",
            "include_prompt",
            "resume_context",
            "client_capabilities",
        )
    )


# LLM: Stable request identity uses authenticated scope plus the opaque client message id. The
# separate input digest includes canonical content, so reusing that id for other input fails closed.
# 函数用途: 为可重试普通消息生成固定 Gateway 请求 ID 和服务端计算的内容指纹。
def _http_idempotent_request_identity(
    body: dict,
    *,
    goal: str,
    user_id: str,
    channel: str,
) -> tuple[str, str]:
    metadata = body.get("metadata")
    metadata = dict(metadata) if isinstance(metadata, dict) else {}
    message_id = str(metadata.get("message_id") or "").strip()
    if not message_id:
        return "", ""
    conversation_id = _http_conversation_id(body)
    identity = {
        "user_id": str(user_id or "").strip(),
        "channel": str(channel or "").strip(),
        "conversation_id": conversation_id,
        "message_id": message_id,
    }
    identity_json = json.dumps(
        identity,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    request_id = "gwreq-msg-" + hashlib.sha256(identity_json.encode("utf-8")).hexdigest()[:32]
    canonical_metadata = {
        key: value
        for key, value in metadata.items()
        if key != "client_input_digest"
    }
    canonical_input = {
        **identity,
        "goal": str(goal or ""),
        "metadata": canonical_metadata,
        "system_task": body.get("system_task") if isinstance(body.get("system_task"), dict) else {},
        "workspace": body.get("workspace") if isinstance(body.get("workspace"), dict) else {},
        "execution_options": GatewayAskExecutionOptions.from_payload(body).to_payload(),
    }
    input_json = json.dumps(
        canonical_input,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return request_id, hashlib.sha256(input_json.encode("utf-8")).hexdigest()


def _http_conversation_payload(context: _AskRequestContext) -> dict:
    conversation_id = _http_conversation_id(context.body)
    return {
        "channel": str(context.channel or "http"),
        "channel_conversation_id": conversation_id,
        "channel_user_id": str(context.user_id or "anonymous"),
        "canonical_user_id": str(context.user_id or "anonymous"),
    }


def _http_conversation_id(body: dict) -> str:
    if value := _first_conversation_id(body):
        return value
    metadata = body.get("metadata")
    if isinstance(metadata, dict):
        return _first_conversation_id(metadata) or "default"
    return "default"


def _first_conversation_id(payload: dict) -> str:
    for key in ("conversation_id", "session_id", "thread_id", "channel_conversation_id"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    return ""


# LLM: HTTP shutdown is admin-only and must target the PID generation currently recorded by this
# Gateway. A presence-only file can poison the next service-manager restart and is forbidden.
# 函数用途: 接收本机管理员停止请求，并把它精确投递给当前 Gateway 进程。
def handle_stop(handler, server) -> None:
    if require_admin_handler(handler):
        return  # 停网关需管理员:已发 403(鉴权未接线时返回 False,回环本机请求放行)
    if server is None:
        handler._send_json(500, {"error": "server not initialized"})
        return
    server.paths.root.mkdir(parents=True, exist_ok=True)
    pid = get_running_pid(server.paths.pid, cleanup_stale=False)
    payload = write_targeted_gateway_stop_request(
        server.paths.pid,
        server.paths.stop_request,
        reason="http stop request",
        target_pid=pid,
        source="gateway_http",
    )
    if payload is None:
        handler._send_json(409, {"error": "gateway process identity unavailable"})
        return
    handler._send_json(200, {"status": "stopping"})


def handle_session_channels(handler, server) -> None:
    from ..auth.middleware import require_admin_handler

    if require_admin_handler(handler):
        return
    parts = handler.path.split("/")
    if len(parts) < 4:
        handler._send_json(400, {"error": "invalid path"})
        return
    if server is None or server.cross_channel is None:
        handler._send_json(500, {"error": "cross channel not initialized"})
        return
    session_id = parts[2]
    bound = server.cross_channel.get_bound_sessions(session_id)
    primary = server.cross_channel.get_primary_channel(session_id)
    handler._send_json(200, {"session_id": session_id, "bound_channels": bound, "primary_channel": primary})


def handle_session_bind(handler, server) -> None:
    from ..auth.middleware import require_admin_handler

    if require_admin_handler(handler):
        return
    parts = handler.path.split("/")
    if len(parts) < 4:
        handler._send_json(400, {"error": "invalid path"})
        return
    body = _read_bind_body(handler)
    if body is None:
        return
    if not body.get("channel"):
        handler._send_json(400, {"error": "channel is required"})
        return
    if server is None or server.cross_channel is None:
        handler._send_json(500, {"error": "cross channel not initialized"})
        return
    session_id = parts[2]
    success = server.cross_channel.bind_session(session_id, body["channel"], body.get("user_id", "admin"))
    handler._send_json(200, {"success": success, "session_id": session_id, "channel": body["channel"]})


def _read_bind_body(handler) -> dict | None:
    try:
        return handler._read_json()
    except json.JSONDecodeError as exc:
        handler._send_json(400, {"error": f"invalid JSON: {exc}"})
        return None


def handle_admin_summary(handler, server) -> None:
    from ..auth.middleware import require_admin_handler

    if require_admin_handler(handler):
        return
    if server is None or server.admin_query is None:
        handler._send_json(500, {"error": "admin query not initialized"})
        return
    summary = server.admin_query.format_admin_summary("admin")
    handler._send_json(200, {"summary": summary})
