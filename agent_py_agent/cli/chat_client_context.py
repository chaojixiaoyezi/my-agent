from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from ..agent.gateway_parts.request_client import (
    GatewayAskExecutionOptions,
    gateway_request_workspace_payload,
)
from ..agent.settings import load_config
from ..agent.settings.services.runtime_config_env import apply_runtime_config_environment
from ..agent.user_space.home_layout import home_paths
from ..agent.user_space.home_root import configured_home_root
from ..agent.user_space.owner_resolver import (
    OwnerIdentity,
    home_paths_with_owner,
    owner_identity_from_config,
    resolve_owner_home,
)
from ..agent.user_space.runtime_paths import (
    apply_runtime_paths_to_config,
    resolve_runtime_paths_for_agent,
)
from .workspace_resolution import (
    explicit_workspace_root,
    explicit_workspace_roots,
    owner_home_workspace_root,
    resolve_workspace_roots,
    validate_requested_workspace_roots,
)

# LLM: Gateway chat clients need config, owner-scoped paths, and UI metadata—not a model backend,
# tool registry, scheduler, memory curator, or subagent runtime. Every operation stays on an
# explicit Gateway HTTP/file contract; this process must never promote itself to SimpleAgent.
# 后台易失游标必须携带服务端流身份；canonical 消息位置与模型上下文独立于重连。
# 模块用途: 为 Gateway TUI 构造始终轻量的客户端；聊天、记忆、历史和生命周期都交给已经
# 运行的 Gateway，不在终端进程重复初始化完整智能体。


# LLM: Transport delivery is deliberately tri-state. UNKNOWN is never false/rejected because the
# Gateway may have committed the same client id after the HTTP response path was lost.
# 类用途: 表示活动回合补充消息已接收、明确拒绝或仍需对账。
class ActiveTurnInputDelivery(str, Enum):
    ACCEPTED = "accepted"
    QUEUED = "queued"
    REJECTED = "rejected"
    CONFLICT = "conflict"
    UNKNOWN = "unknown"


# LLM: The client must retain the server's canonical queued request id; a bare enum cannot attach
# the TUI worker after active-turn fallback without submitting the same message again.
# 类用途: 返回普通输入最终进入当前回合、下一轮队列或仍待确认，并携带真实请求 ID。
@dataclass(frozen=True)
class ActiveTurnInputResult:
    delivery: ActiveTurnInputDelivery
    request_id: str = ""
    disposition: str = ""


# LLM: Identity projection is a pure transport helper shared by HTTP headers and JSON bodies.
# It must use only the typed owner identity and never infer an owner from a conversation id.
# 函数用途: 把当前 TUI 的 owner 身份转换成单 Gateway 使用的用户、通道和群聊字段。
def _gateway_request_identity(owner_identity: OwnerIdentity) -> dict[str, str]:
    if owner_identity == OwnerIdentity.local_main():
        user_id, channel = "local-agent", "chat"
    else:
        user_id, channel = str(owner_identity.owner_id), str(owner_identity.provider)
    identity = {"user_id": user_id, "channel": channel}
    if owner_identity.owner_kind == "group":
        identity.update(
            {
                "channel_chat_type": "group",
                "channel_chat_id": str(owner_identity.owner_id),
            }
        )
    return identity


# LLM: HTTP authentication and body audit fields consume the exact same typed projection.
# 函数用途: 生成当前用户访问本机单 Gateway 时统一使用的请求头。
def _gateway_headers(
    owner_identity: OwnerIdentity,
    *,
    json_body: bool = False,
    conversation_id: str = "",
) -> dict[str, str]:
    identity = _gateway_request_identity(owner_identity)
    headers = {
        "X-User-Id": identity["user_id"],
        "X-Channel": identity["channel"],
    }
    if identity.get("channel_chat_type") == "group":
        headers.update(
            {
                "X-Channel-Chat-Type": "group",
                "X-Channel-Chat-Id": identity["channel_chat_id"],
            }
        )
    selected_conversation = str(conversation_id or "").strip()
    if selected_conversation:
        headers["X-Conversation-Id"] = selected_conversation
    if json_body:
        headers["Content-Type"] = "application/json"
    return headers


# LLM: Body identity mirrors authenticated headers; caller payloads are copied and group metadata
# is extended without mutating the original mapping.
# 函数用途: 给请求正文补上当前用户身份，避免不同 TUI 都退化成 local-agent。
def _with_client_identity(
    owner_identity: OwnerIdentity,
    payload: dict[str, object],
) -> dict[str, object]:
    identity = _gateway_request_identity(owner_identity)
    normalized = dict(payload)
    normalized.update({"user_id": identity["user_id"], "channel": identity["channel"]})
    if identity.get("channel_chat_type") == "group":
        raw_metadata = normalized.get("metadata")
        metadata = dict(raw_metadata) if isinstance(raw_metadata, dict) else {}
        metadata.update(
            {
                "channel_chat_type": "group",
                "channel_chat_id": identity["channel_chat_id"],
            }
        )
        normalized["metadata"] = metadata
    return normalized


# LLM: The structured marker is consumed only by client-side adapters. Missing attributes remain
# AttributeError so an accidental server-only call cannot silently allocate a second runtime.
# 类用途: 保存聊天界面所需配置和路径，并通过显式 Gateway 请求完成客户端操作。
class GatewayChatClientAgent:
    gateway_client_only = True

    def __init__(
        self,
        args,
        config,
        root: Path,
        workspace_roots: list[Path],
        scoped_home,
        *,
        owner_identity: OwnerIdentity | None = None,
    ) -> None:
        del args
        self.config = config
        self.root = root
        self.workspace_roots = workspace_roots
        self.home_paths = scoped_home
        self.owner_identity = owner_identity or OwnerIdentity.local_main()

    # LLM: HTTP requests and file-queue asks must consume this exact identity projection. Keeping
    # a public typed projection prevents the idle-submit path from falling back to local-agent.
    # 函数用途: 返回薄 TUI 提交任何 Gateway 请求时统一使用的用户、通道和群聊身份。
    def gateway_request_identity(self) -> dict[str, str]:
        return _gateway_request_identity(self.owner_identity)

    # LLM: 模型表单以独立payload映射走authenticated owner接口；身份字段由宿主覆盖，不进入ask/history或自动重发密钥。
    # 函数用途: 管理当前用户模型；只有主动短测试延长等待预算，配置回执和诊断都必须脱敏。
    def request_models(self, *, session_id: str, operation: str, payload: dict[str, object] | None = None) -> dict:
        status, body = self.post_gateway_json("/client/models", {
            **(payload or {}), "operation": operation, "conversation_id": session_id,
        }, timeout=180.0 if operation == "probe" else 30.0 if operation == "discover" else 10.0)
        if body:
            return body
        return {"ok": False, "http_status": status, "message": "Gateway 未确认操作结果；请重新打开列表确认。"}

    # LLM: 复用薄客户端唯一认证身份；只发送显式模式枚举，不携带权限根、owner 路径或聊天指令。
    # 函数用途: 读取或保存审批模式，网络结果不确定时提示重新读取，绝不假报已开启。
    def request_permissions(self, *, session_id: str, operation: str, mode: str | None = None) -> dict:
        status, body = self.post_gateway_json("/client/permissions", {
            "operation": operation, "mode": mode, "conversation_id": session_id,
        }, timeout=10.0)
        return body or {"ok": False, "http_status": status, "message": "Gateway 未确认权限设置；请重新打开 /permissions 核对。"}

    # LLM: Session lifecycle is submitted to the already running Gateway's typed HTTP contract,
    # preserving owner resolution without constructing another local MemoryCuratorService.
    # 函数用途: 退出或重置会话时通知 Gateway 登记记忆策展事件，失败按 best-effort 返回 False。
    def request_session_lifecycle(self, session_id: str, *, event: str) -> bool:
        payload = {
            "kind": "session_lifecycle",
            "event": event,
            "user_id": "local-agent",
            "channel": "chat",
            "conversation_id": str(session_id or "default"),
        }
        status, body = self.post_gateway_json("/ask", payload, timeout=1.0)
        return status == 202 and body.get("disposition") == "memory_curator_request"

    # LLM: Memory commands are executed by the already initialized owner Agent in Gateway; the
    # client sends only operation/content/query and exact conversation identity. A lost remember
    # response is UNKNOWN rather than failed because the Gateway may have committed the write.
    # 函数用途: 查询最近记忆、搜索记忆或显式保存一条事实；写回执丢失时明确返回结果未知。
    def request_memory(
        self,
        *,
        operation: str,
        session_id: str,
        query: str = "",
        content: str = "",
        limit: int = 5,
    ) -> dict[str, object]:
        status, body = self.post_gateway_json(
            "/client/memory",
            {
                "operation": operation,
                "query": query,
                "content": content,
                "kind": "fact",
                "limit": max(1, int(limit or 5)),
                "user_id": "local-agent",
                "channel": "chat",
                "conversation_id": str(session_id or "default"),
            },
            timeout=10.0,
        )
        if body:
            return {**body, "http_status": status}
        if str(operation or "").strip().lower() == "remember":
            return {
                "ok": False,
                "outcome": "unknown",
                "http_status": status,
                "error_code": "MEMORY_WRITE_RESULT_UNKNOWN",
            }
        return {
            "ok": False,
            "http_status": status,
            "error_code": "GATEWAY_UNAVAILABLE",
        }

    # LLM: 历史及向前分页只访问同 owner Gateway，不打开服务端路径；分页不提交模型任务。
    # 函数用途: 读取最新或给定完整行边界之前的一页会话显示。
    def request_chat_history(
        self, session_id: str, *, max_turns: int, before_message_cursor: int | None = None,
    ) -> dict[str, object]:
        _status, body = self.post_gateway_json(
            "/client/history",
            {
                "limit": max(1, int(max_turns or 1)),
                "user_id": "local-agent",
                "channel": "chat",
                "conversation_id": str(session_id or "default"),
                **({"before_message_cursor": before_message_cursor} if before_message_cursor is not None else {}),
            },
            timeout=10.0,
        )
        return body or {
            "ok": False,
            "turns": [],
            "load_errors": [{"error_code": "GATEWAY_UNAVAILABLE"}],
        }

    # LLM: canonical 游标不随进程流重置；显式声明能按请求去重前台消息，旧客户端不被强发未知用户事件。
    # 函数用途: 显式声明前台和检查点能力；沿同一持久游标补过程，失败不清空已有显示。
    def request_background_notices(
        self,
        session_id: str,
        *,
        after: int,
        event_after: int = 0,
        event_stream_id: str = "",
    ) -> dict[str, object]:
        # 后台状态只是易失展示；2 秒仍无响应就交给 TUI 退避，不能让每个窗口长期占住连接。
        _status, body = self.post_gateway_json(
            "/client/notices",
            {
                "after": max(0, int(after or 0)),
                "event_after": max(0, int(event_after or 0)),
                "event_stream_id": event_stream_id,
                "client_capabilities": {"tool_approval": True, "foreground_messages": True, "foreground_transcript": True, "display_checkpoints": True},
                "user_id": "local-agent",
                "channel": "chat",
                "conversation_id": str(session_id or "default"),
            },
            timeout=2.0,
        )
        return body or {
            "ok": False,
            "notices": [],
            "cursor": after,
            "transcript_events": [],
            "event_cursor": max(0, int(event_after or 0)),
            "event_stream_id": event_stream_id,
            "active_task_count": 0,
            "agent_permission_requests": [],
        }

    # LLM: Agent detail polling is an explicit owner-scoped HTTP projection. The
    # thin client sends one run id and display cursor and never opens child files.
    # 函数用途: 拉取当前选中子代理的状态、过程事件、Todo、直属下级和最终回复。
    def request_agent_view(
        self,
        session_id: str,
        *,
        run_id: str,
        event_after: int = 0,
    ) -> dict[str, object]:
        _status, body = self.post_gateway_json(
            "/client/agent-view",
            {
                "user_id": "local-agent",
                "channel": "chat",
                "conversation_id": str(session_id or "default"),
                "run_id": str(run_id or "").strip(),
                "event_after": max(0, int(event_after or 0)),
            },
            timeout=2.0,
        )
        return body or {"ok": False, "error_code": "GATEWAY_UNAVAILABLE"}

    # LLM: Child guidance carries a stable client message id to the dedicated
    # mailbox endpoint. It cannot fall back to /ask or create a main-agent job.
    # 函数用途: 给正在工作的指定子代理插入一条普通自然语言补充要求。
    def request_agent_guidance(
        self,
        session_id: str,
        *,
        run_id: str,
        message: str,
        message_id: str,
    ) -> dict[str, object]:
        status, body = self.post_gateway_json(
            "/client/agent-guidance",
            {
                "user_id": "local-agent",
                "channel": "chat",
                "conversation_id": str(session_id or "default"),
                "run_id": str(run_id or "").strip(),
                "message": str(message or ""),
                "message_id": str(message_id or "").strip(),
            },
            timeout=2.0,
        )
        return {**body, "http_status": status} if body else {
            "ok": False,
            "http_status": status,
            "error_code": "GATEWAY_UNAVAILABLE",
        }

    # LLM: Child approval submission carries the exact server-projected request
    # and typed decision to its dedicated endpoint. It never retries as chat text
    # or turns a transport failure into an approval.
    # 函数用途: 把用户在 TUI 里选择的子代理工具审批结果写回单 Gateway。
    def request_agent_permission(
        self,
        session_id: str,
        *,
        run_id: str,
        request: dict[str, object],
        decision: dict[str, object],
    ) -> dict[str, object]:
        status, body = self.post_gateway_json(
            "/client/agent-permission",
            {
                "user_id": "local-agent",
                "channel": "chat",
                "conversation_id": str(session_id or "default"),
                "run_id": str(run_id or "").strip(),
                "request": dict(request),
                "decision": dict(decision),
            },
            timeout=2.0,
        )
        return {**body, "http_status": status} if body else {
            "ok": False,
            "http_status": status,
            "error_code": "GATEWAY_UNAVAILABLE",
        }

    # LLM: Esc in a child view maps to one exact owner-scoped cancellation request; the client
    # never selects a process, pid, role name, or visible row index. The Gateway acknowledges
    # admission quickly while canonical terminal state continues asynchronously.
    # 函数用途: 请求停止当前正在查看的子代理，并接收服务端的快速排队回执。
    def request_agent_stop(
        self,
        session_id: str,
        *,
        run_id: str,
        operation_id: str,
    ) -> dict[str, object]:
        status, body = self.post_gateway_json(
            "/client/agent-stop",
            {
                "user_id": "local-agent",
                "channel": "chat",
                "conversation_id": str(session_id or "default"),
                "run_id": str(run_id or "").strip(),
                "operation_id": str(operation_id or "").strip(),
            },
            timeout=10.0,
        )
        return {**body, "http_status": status} if body else {
            "ok": False,
            "http_status": status,
            "error_code": "GATEWAY_UNAVAILABLE",
        }

    # LLM: Ordinary TUI input carries both 会话运行时 expected turn id and an opaque client id.
    # Only a typed rejected response permits queue fallback; transport failure remains UNKNOWN so
    # the caller can reconcile the same id without duplicating work.
    # 函数用途: 把运行中的普通补充消息送进精确回合，并返回接收、拒绝或未知三态。
    def request_active_turn_input(
        self,
        session_id: str,
        *,
        message: str,
        message_id: str,
        expected_turn_id: str,
        execution_options: GatewayAskExecutionOptions | None = None,
    ) -> ActiveTurnInputResult:
        content = str(message or "").strip()
        client_message_id = str(message_id or "").strip()
        turn_id = str(expected_turn_id or "").strip()
        if not content or not client_message_id or not turn_id:
            return ActiveTurnInputResult(ActiveTurnInputDelivery.REJECTED)
        payload: dict[str, object] = {
            "goal": content,
            "user_id": "local-agent",
            "channel": "chat",
            "conversation_id": str(session_id or "default"),
            "metadata": {
                "message_id": client_message_id,
                "expected_turn_id": turn_id,
            },
        }
        workspace = self.gateway_request_workspace()
        if workspace:
            payload["workspace"] = workspace
        if execution_options is not None:
            payload.update(execution_options.to_payload())
        status, body = self.post_gateway_json("/ask", payload, timeout=2.0)
        return _active_turn_input_result(status, body)

    # LLM: Only local-main may propose a host cwd. A scoped user already selects its canonical
    # owner home through authenticated identity; serializing that path as a remote-owner cwd
    # override would cross the Gateway's hard authority boundary and break every first request.
    # 函数用途: 本机管理员返回显式会话目录；普通用户省略目录，由 Gateway 固定到自己的家目录。
    def gateway_request_workspace(self) -> dict[str, object]:
        if self.owner_identity != OwnerIdentity.local_main():
            return {}
        return gateway_request_workspace_payload(self.root, self.workspace_roots)

    # LLM: Once POST returns the stable ingress id, all later reconciliation is read-only. This
    # prevents a TUI session from resubmitting text merely because a consumed event was missed.
    # 函数用途: 查询一条补充消息的 Gateway 持久投递状态。
    def request_active_turn_input_status(self, request_id: str) -> ActiveTurnInputResult:
        stable_id = str(request_id or "").strip()
        if not stable_id:
            return ActiveTurnInputResult(ActiveTurnInputDelivery.UNKNOWN)
        status, body = self.get_gateway_json(
            f"/input-status/{stable_id}",
            timeout=2.0,
        )
        return _active_turn_input_result(status, body)

    # LLM: This is the sole HTTP reader for lightweight client status contracts. Callers that
    # query a conversation-scoped receipt must pass that exact conversation id as authentication;
    # transport errors remain typed UNKNOWN at the caller.
    # 函数用途: 从本机 Gateway 读取 JSON 状态，并可携带精确会话身份完成权限校验。
    def get_gateway_json(
        self,
        path: str,
        *,
        timeout: float,
        conversation_id: str = "",
    ) -> tuple[int, dict[str, object]]:
        port = int(getattr(self.config, "gateway_port", 0) or 0)
        if port <= 0:
            return 0, {}
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}{path}",
            headers=_gateway_headers(
                self.owner_identity,
                conversation_id=conversation_id,
            ),
        )
        try:
            with urllib.request.urlopen(request, timeout=max(0.1, float(timeout))) as response:
                body = json.loads(response.read().decode("utf-8", "replace"))
                status = int(response.status)
        except urllib.error.HTTPError as exc:
            return _http_error_json(exc)
        except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError):
            return 0, {}
        return status, body if isinstance(body, dict) else {}
    # LLM: This is the sole HTTP writer for the lightweight chat context. It returns safe empty
    # state on transport/JSON failure and never falls back to local Agent services.
    # 函数用途: 向本机 Gateway 发送一条 JSON 请求，并返回 HTTP 状态和对象响应。
    def post_gateway_json(
        self,
        path: str,
        payload: dict[str, object],
        *,
        timeout: float,
    ) -> tuple[int, dict[str, object]]:
        port = int(getattr(self.config, "gateway_port", 0) or 0)
        if port <= 0:
            return 0, {}
        normalized_payload = _with_client_identity(self.owner_identity, payload)
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}{path}",
            data=json.dumps(normalized_payload, ensure_ascii=False).encode("utf-8"),
            headers=_gateway_headers(self.owner_identity, json_body=True),
        )
        try:
            with urllib.request.urlopen(request, timeout=max(0.1, float(timeout))) as response:
                body = json.loads(response.read().decode("utf-8", "replace"))
                status = int(response.status)
        except urllib.error.HTTPError as exc:
            return _http_error_json(exc)
        except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError):
            return 0, {}
        return status, body if isinstance(body, dict) else {}


# LLM: POST /ask and GET /input-status share one result decoder. Client UI decisions rely only on
# typed disposition/delivery fields and retain the server's canonical request id.
# 函数用途: 把 Gateway 普通消息状态转换成 TUI 使用的四态结果。
def _active_turn_input_result(
    status: int,
    body: dict[str, object],
) -> ActiveTurnInputResult:
    if status == 409 and str(body.get("error_code") or "") == "IDEMPOTENCY_CONFLICT":
        return ActiveTurnInputResult(
            ActiveTurnInputDelivery.CONFLICT,
            request_id=str(body.get("request_id") or "").strip(),
        )
    if status not in {200, 202}:
        return ActiveTurnInputResult(ActiveTurnInputDelivery.UNKNOWN)
    request_id = str(body.get("request_id") or "").strip()
    disposition = str(body.get("disposition") or "").strip()
    delivery_status = str(body.get("delivery_status") or "").strip().lower()
    if disposition == "queued" and request_id:
        return ActiveTurnInputResult(
            ActiveTurnInputDelivery.QUEUED,
            request_id=request_id,
            disposition=disposition,
        )
    if disposition == "active_turn_input" and delivery_status == "accepted":
        return ActiveTurnInputResult(
            ActiveTurnInputDelivery.ACCEPTED,
            request_id=request_id,
            disposition=disposition,
        )
    if disposition == "active_turn_input" and delivery_status == "unknown":
        return ActiveTurnInputResult(
            ActiveTurnInputDelivery.UNKNOWN,
            request_id=request_id,
            disposition=disposition,
        )
    if delivery_status == "rejected":
        return ActiveTurnInputResult(
            ActiveTurnInputDelivery.REJECTED,
            request_id=request_id,
            disposition=disposition,
        )
    return ActiveTurnInputResult(ActiveTurnInputDelivery.UNKNOWN, request_id=request_id)


# LLM: urllib raises HTTPError for structured 4xx/5xx bodies; preserving status and JSON lets the
# typed delivery decoder stop retrying deterministic idempotency conflicts.
# 函数用途: 从 HTTP 异常读取 Gateway 的结构化 JSON 响应，解析失败时保留状态和空对象。
def _http_error_json(exc: urllib.error.HTTPError) -> tuple[int, dict[str, object]]:
    try:
        payload = json.loads(exc.read().decode("utf-8", "replace"))
    except (OSError, json.JSONDecodeError, ValueError):
        payload = {}
    return int(exc.code), payload if isinstance(payload, dict) else {}


# LLM: Reuse canonical config layers and owner paths, but bind all thin clients to the local-main
# process service. The scoped owner still owns sessions/workspace/memory and travels in HTTP auth.
# This function may create the workspace directory but must not initialize model/tool services.
# 函数用途: 构造连接同一个后台 Gateway、但会话和工作目录属于当前用户的轻量客户端。
def make_gateway_chat_client(args) -> GatewayChatClientAgent:
    config = apply_runtime_config_environment(load_config(args.config))
    # 一次声明多个目录时，整串会被当成一条路径——必须逐项解析后再写回配置。
    explicit_roots = explicit_workspace_roots(args)
    if explicit_roots:
        config.workspace_root = [str(root) for root in explicit_roots]
    base_home = home_paths(configured_home_root(config))
    owner_identity = owner_identity_from_config(config)
    owner = resolve_owner_home(base_home.root, owner_identity)
    scoped_home = home_paths_with_owner(base_home, owner)
    roots = resolve_workspace_roots(
        config,
        args.config,
        current_dir=owner_home_workspace_root(config),
    )
    validate_requested_workspace_roots(config, roots)
    root = roots[0]
    root.mkdir(parents=True, exist_ok=True)
    base_owner = resolve_owner_home(base_home.root, OwnerIdentity.local_main())
    base_scoped_home = home_paths_with_owner(base_home, base_owner)
    shared_gateway_workspace = resolve_runtime_paths_for_agent(
        config,
        base_owner.home_dir,
        base_scoped_home,
    ).paths["gateway_workspace"]
    resolution = resolve_runtime_paths_for_agent(config, root, scoped_home)
    apply_runtime_paths_to_config(config, resolution)
    config.gateway_workspace = str(shared_gateway_workspace)
    return GatewayChatClientAgent(
        args,
        config,
        root,
        roots,
        scoped_home,
        owner_identity=owner_identity,
    )


__all__ = [
    "ActiveTurnInputDelivery",
    "ActiveTurnInputResult",
    "GatewayChatClientAgent",
    "make_gateway_chat_client",
]
