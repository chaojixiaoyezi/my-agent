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
    home_paths_with_owner,
    owner_identity_from_config,
    resolve_owner_home,
)
from ..agent.user_space.runtime_paths import (
    apply_runtime_paths_to_config,
    resolve_runtime_paths_for_agent,
)
from .workspace_resolution import explicit_workspace_root, resolve_workspace_roots

# LLM: Gateway chat clients need config, owner-scoped paths, and UI metadata—not a model backend,
# tool registry, scheduler, memory curator, or subagent runtime. Every operation stays on an
# explicit Gateway HTTP/file contract; this process must never promote itself to SimpleAgent.
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


# LLM: The structured marker is consumed only by client-side adapters. Missing attributes remain
# AttributeError so an accidental server-only call cannot silently allocate a second runtime.
# 类用途: 保存聊天界面所需配置和路径，并通过显式 Gateway 请求完成客户端操作。
class GatewayChatClientAgent:
    gateway_client_only = True

    def __init__(self, args, config, root: Path, workspace_roots: list[Path], scoped_home) -> None:
        del args
        self.config = config
        self.root = root
        self.workspace_roots = workspace_roots
        self.home_paths = scoped_home

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
        status, body = self._post_gateway_json("/ask", payload, timeout=1.0)
        return status == 202 and body.get("disposition") == "memory_curator_request"

    # LLM: Memory commands are executed by the already initialized owner Agent in Gateway; the
    # client sends only operation/content/query and exact conversation identity.
    # 函数用途: 查询最近记忆、搜索记忆或显式保存一条事实，并返回结构化结果。
    def request_memory(
        self,
        *,
        operation: str,
        session_id: str,
        query: str = "",
        content: str = "",
        limit: int = 5,
    ) -> dict[str, object]:
        _status, body = self._post_gateway_json(
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
        return body or {"ok": False, "error_code": "GATEWAY_UNAVAILABLE"}

    # LLM: Resume history comes from the same owner ConversationStore used by Gateway turns; the
    # terminal must not open that store or create a local full Agent.
    # 函数用途: 读取一个聊天会话最近的完整问答回合。
    def request_chat_history(self, session_id: str, *, max_turns: int) -> dict[str, object]:
        _status, body = self._post_gateway_json(
            "/client/history",
            {
                "limit": max(1, int(max_turns or 1)),
                "user_id": "local-agent",
                "channel": "chat",
                "conversation_id": str(session_id or "default"),
            },
            timeout=10.0,
        )
        return body or {
            "ok": False,
            "turns": [],
            "load_errors": [{"error_code": "GATEWAY_UNAVAILABLE"}],
        }

    # LLM: S-BG1 notices and the typed active-task count share one lightweight
    # HTTP snapshot; transport failure returns no activity claim so the caller
    # preserves its last projection instead of guessing from prose.
    # 函数用途: 拉取当前会话后台任务数量，以及 after 游标之后的后台完成通知。
    def request_background_notices(
        self,
        session_id: str,
        *,
        after: float,
        event_after: int = 0,
    ) -> dict[str, object]:
        # 后台状态只是易失展示；2 秒仍无响应就交给 TUI 退避，不能让每个窗口长期占住连接。
        _status, body = self._post_gateway_json(
            "/client/notices",
            {
                "after": max(0.0, float(after or 0.0)),
                "event_after": max(0, int(event_after or 0)),
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
            "active_task_count": 0,
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
        workspace = self._workspace_payload()
        if workspace:
            payload["workspace"] = workspace
        if execution_options is not None:
            payload.update(execution_options.to_payload())
        status, body = self._post_gateway_json("/ask", payload, timeout=2.0)
        return _active_turn_input_result(status, body)

    # LLM: The thin client reports its cwd on queued turns without using that cwd to locate the
    # daemon. Gateway service identity stays owner-scoped while the server validates this setting.
    # 函数用途: 返回当前 TUI 希望本会话使用的工作目录和可见根目录。
    def _workspace_payload(self) -> dict[str, object]:
        return gateway_request_workspace_payload(self.root, self.workspace_roots)

    # LLM: Once POST returns the stable ingress id, all later reconciliation is read-only. This
    # prevents a TUI session from resubmitting text merely because a consumed event was missed.
    # 函数用途: 查询一条补充消息的 Gateway 持久投递状态。
    def request_active_turn_input_status(self, request_id: str) -> ActiveTurnInputResult:
        stable_id = str(request_id or "").strip()
        if not stable_id:
            return ActiveTurnInputResult(ActiveTurnInputDelivery.UNKNOWN)
        status, body = self._get_gateway_json(
            f"/input-status/{stable_id}",
            timeout=2.0,
        )
        return _active_turn_input_result(status, body)

    # LLM: This is the sole HTTP reader for lightweight client status contracts; authentication
    # headers match the POST path and transport errors remain typed UNKNOWN at the caller.
    # 函数用途: 从本机 Gateway 读取一个 JSON 状态对象。
    def _get_gateway_json(
        self,
        path: str,
        *,
        timeout: float,
    ) -> tuple[int, dict[str, object]]:
        port = int(getattr(self.config, "gateway_port", 0) or 0)
        if port <= 0:
            return 0, {}
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}{path}",
            headers={"X-User-Id": "local-agent", "X-Channel": "chat"},
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
    def _post_gateway_json(
        self,
        path: str,
        payload: dict[str, object],
        *,
        timeout: float,
    ) -> tuple[int, dict[str, object]]:
        port = int(getattr(self.config, "gateway_port", 0) or 0)
        if port <= 0:
            return 0, {}
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}{path}",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-User-Id": "local-agent",
                "X-Channel": "chat",
            },
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


# LLM: Reuse canonical config layers, home precedence, owner identity, and runtime-path resolver.
# This function may create the workspace directory but must not initialize model/tool services.
# 函数用途: 根据现有命令参数构造一个与后台 Gateway 指向同一 owner/workspace 的轻量客户端。
def make_gateway_chat_client(args) -> GatewayChatClientAgent:
    config = apply_runtime_config_environment(load_config(args.config))
    explicit_root = explicit_workspace_root(args)
    if explicit_root is not None:
        config.workspace_root = str(explicit_root)
    roots = resolve_workspace_roots(config, args.config)
    root = roots[0]
    root.mkdir(parents=True, exist_ok=True)
    base_home = home_paths(configured_home_root(config))
    owner = resolve_owner_home(base_home.root, owner_identity_from_config(config))
    scoped_home = home_paths_with_owner(base_home, owner)
    resolution = resolve_runtime_paths_for_agent(config, root, scoped_home)
    apply_runtime_paths_to_config(config, resolution)
    return GatewayChatClientAgent(args, config, root, roots, scoped_home)


__all__ = [
    "ActiveTurnInputDelivery",
    "ActiveTurnInputResult",
    "GatewayChatClientAgent",
    "make_gateway_chat_client",
]
