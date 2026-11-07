

# LLM: chat提交统一走认证HTTP让宿主冻结owner；文件只观察原chunk/terminal，typed row不改变业务事实或授权。
# 退休raw candidate kind必须在typed callback前消费且正常推进游标；保私有原档和未知future kind原语义。
# 模块用途: 提交聊天请求、增量读取 Gateway chunk/response 文件，并把结构化事件或兼容文本交给调用方。

from __future__ import annotations

import json
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ...agent.gateway_parts.io import STREAM_CHUNK_READ_MAX_BYTES, read_complete_utf8_rows
from ...agent.gateway_parts.paths import gateway_chunk_path, gateway_chunk_path_candidates
from ...agent.gateway_parts.request_client import GatewayAskParams, submit_gateway_ask
from ...agent.gateway_parts.response_renderer import (
    GatewayResponsePollState,
    current_context_token_estimate,
    project_gateway_stream_chunk,
    read_gateway_terminal_response_file_when_ready,
)


# LLM: ChatRequestContent 声明当前 CLI 表面真实可消费的交互能力；只有 TUI 主链开启审批和富 transcript，plain/其它调用保持最小输出。
# 类用途: 汇总正文、附件、执行快照及原始命令运输事实；显示摘要不替代用户原输入。
@dataclass
class ChatRequestContent:
    prompt: str
    inject: list[str]
    prompt_files: list[str]
    save: bool
    show_prompt: bool
    resume_context: object
    chat_session_id: str = ""
    system_task: dict[str, object] | None = None
    interactive_approvals: bool = False
    rich_transcript: bool = False
    input_media: tuple[dict, ...] = ()
    original_prompt: str | None = None
    client_message_id: str = ""


# LLM: GatewayChunkPollRequest carries the canonical terminal path as the only completion fact;
# on_event consumes typed chunks while on_chunk remains the plain-UI compatibility projection.
# 类用途: 描述流式观察的终态路径、游标和活跃租约；TUI 可显式续等并随页面退出取消观察。
@dataclass(frozen=True)
class GatewayChunkPollRequest:
    chunk_path: Path
    terminal_path: Path
    deadline: float
    on_chunk: object
    chunks_printed_ref: list[int]
    visible_chunks_ref: list[int] | None = None
    chunk_offset_ref: list[int] | None = None
    activity_paths: tuple[Path, ...] = ()
    inactivity_timeout_seconds: float = 0.0
    on_event: object | None = None
    on_wait_timeout: Callable[[], None] | None = None
    is_wait_cancelled: Callable[[], bool] | None = None


# LLM: 仅供可信宿主替换观察运输；实现须保留原终态、游标、计数及等待回调，不授权或重新提交请求。
# 类用途: 约定 plain/TUI 共用的聊天观察器调用合同。
class GatewayChatObserver(Protocol):
    def __call__(self, request: GatewayChunkPollRequest) -> dict[str, object]: ...


@dataclass(frozen=True)
class GatewayTimingContext:
    request_id: str
    elapsed: float
    response: dict
    use_gateway: bool


@dataclass(frozen=True)
# LLM: ChunkFilePollRequest 只携带单次尾读状态；on_event 消费成功时禁止再投影为 legacy text。
# 类用途: 描述一次 chunk 文件增量读取及累计计数。
class ChunkFilePollRequest:
    chunk_path: Path
    on_chunk: object
    chunks_printed: int
    visible_chunks: int
    chunk_offset: int
    on_event: object | None = None


# LLM: 正式薄/完整客户端统一HTTP提交，传原身份/工作区/原始命令，owner由宿主决定；callback在POST前登记同canonical ID，网络未知不回退文件队列。
# 函数用途: 传递附件refs与输入快照，先关联显示再请求Gateway；chunk/终态仍沿原文件观察，不在客户端执行任务。
def submit_chat_request(
    paths,
    content: ChatRequestContent,
    agent,
    *,
    workspace_root: object = "",
    workspace_roots: object = None,
    on_request_allocated: Callable[[str], None] | None = None,
) -> tuple[str, Path, Path]:
    from functools import partial

    from ...agent.gateway_parts.request_http_client import post_gateway_ask

    identity = _gateway_submit_identity(agent)
    workspace = _gateway_submit_workspace(
        agent,
        workspace_root=workspace_root,
        workspace_roots=workspace_roots,
    )
    network = network_gateway_observer(agent)
    request_id, _, response_path = submit_gateway_ask(
        paths,
        params=GatewayAskParams(
            prompt=content.prompt,
            inject=content.inject,
            prompt_files=content.prompt_files,
            input_media=content.input_media,
            save=content.save,
            include_prompt=content.show_prompt,
            resume_context=content.resume_context,
            chat_session_id=content.chat_session_id,
            channel=identity["channel"],
            channel_user_id=identity["user_id"],
            canonical_user_id=identity["user_id"],
            channel_chat_type=identity.get("channel_chat_type", ""),
            channel_chat_id=identity.get("channel_chat_id", ""),
            agent=agent,
            original_prompt=content.original_prompt,
            client_message_id=content.client_message_id,
            system_task=content.system_task,
            interactive_approvals=content.interactive_approvals,
            rich_transcript=content.rich_transcript,
            workspace_root=str(workspace.get("cwd") or ""),
            workspace_roots=[str(item) for item in workspace.get("roots", [])],
        ),
        submit_request=network.submit if network is not None else partial(post_gateway_ask, agent),
        **({"on_request_allocated": on_request_allocated} if on_request_allocated is not None else {}),
    )
    chunk_path = gateway_chunk_path(paths, request_id)
    return request_id, chunk_path, response_path


# LLM: The thin-client identity projection is the sole source for queued chat ownership. Full
# local Agents retain the historic local-agent/chat identity without inspecting paths or prose.
# 函数用途: 为普通空闲提交取得和 HTTP 控制请求完全一致的用户及通道身份。
def _gateway_submit_identity(agent: object) -> dict[str, str]:
    projector = getattr(agent, "gateway_request_identity", None)
    if callable(projector):
        value = projector()
        if isinstance(value, dict):
            user_id = str(value.get("user_id") or "").strip()
            channel = str(value.get("channel") or "").strip()
            if user_id and channel:
                return {str(key): str(item) for key, item in value.items() if str(item)}
    return {"user_id": "local-agent", "channel": "chat"}


# LLM: A scoped client deliberately returns no cwd so the Gateway chooses its authenticated owner
# home. Local/full Agents may still submit an explicit project cwd through the canonical helper.
# 函数用途: 决定本轮是否携带工作目录；普通用户不再把家目录误报成远程 cwd 覆盖。
def _gateway_submit_workspace(
    agent: object,
    *,
    workspace_root: object,
    workspace_roots: object,
) -> dict[str, object]:
    projector = getattr(agent, "gateway_request_workspace", None)
    if callable(projector):
        value = projector()
        return dict(value) if isinstance(value, dict) else {}
    effective_root = workspace_root or getattr(agent, "root", "")
    effective_roots = (
        workspace_roots
        if workspace_roots is not None
        else getattr(agent, "workspace_roots", None)
    )
    roots = [str(item) for item in (effective_roots or []) if str(item or "").strip()]
    return {"cwd": str(effective_root or ""), "roots": roots}


# 活动租约的最小采样间隔；截止判定至少基于这么长的观察窗。
_BASE_POLL_INTERVAL_SECONDS = 0.1


# LLM: 终态仍只认 canonical envelope；入口绝对 deadline 仅转换一次为单调时钟，
# 后续续租不受校时影响，但只有新增chunk字节重置退避；截止前仍先查终态及排空尾字节，不重提请求或游标。
# 两次活动采样至少间隔 _BASE_POLL_INTERVAL_SECONDS：不能为贴合 deadline 缩短采样，
# 否则租约心跳还没来得及落盘就被判成死请求；退避只放大空闲间隔，不缩短最小采样窗。
# 函数用途: 读取事件和唯一终态；无活动时退避，TUI 等待超时仅提示继续观察，退出只停止本地观察。
def poll_gateway_chunks(request: GatewayChunkPollRequest) -> dict:
    chunks_printed = request.chunks_printed_ref[0]
    visible_chunks = request.visible_chunks_ref[0] if request.visible_chunks_ref else 0
    chunk_offset = request.chunk_offset_ref[0] if request.chunk_offset_ref else 0
    response = {}
    response_poll_state = GatewayResponsePollState()
    deadline = time.monotonic() + max(0.0, request.deadline - time.time())
    activity_fingerprints = {
        path: _path_activity_fingerprint(path) for path in request.activity_paths
    }
    poll_interval = _BASE_POLL_INTERVAL_SECONDS
    while True:
        if request.is_wait_cancelled is not None and request.is_wait_cancelled():
            raise InterruptedError("Gateway 请求观察已退出")
        previous_offset = chunk_offset
        deadline = _extend_active_request_deadline(
            request,
            activity_fingerprints,
            deadline=deadline,
        )
        chunks_printed, visible_chunks, chunk_offset = _poll_chunk_file(
            ChunkFilePollRequest(
                request.chunk_path,
                request.on_chunk,
                chunks_printed,
                visible_chunks,
                chunk_offset,
                request.on_event,
            )
        )
        response = read_gateway_terminal_response_file_when_ready(
            request.terminal_path,
            state=response_poll_state,
            request_id=request.terminal_path.stem,
            context="gateway.chat.terminal.read",
        )
        if response:
            chunks_printed, visible_chunks, chunk_offset = _poll_chunk_file(
                ChunkFilePollRequest(
                    request.chunk_path,
                    request.on_chunk,
                    chunks_printed,
                    visible_chunks,
                    chunk_offset,
                    request.on_event,
                )
            )
            break
        now = time.monotonic()
        chunk_active = chunk_offset != previous_offset
        if now > deadline:
            if request.on_wait_timeout is None:
                break
            request.on_wait_timeout()
            deadline = now + max(1.0, request.inactivity_timeout_seconds)
        poll_interval = _BASE_POLL_INTERVAL_SECONDS if chunk_active else min(0.25, poll_interval * 1.5)
        time.sleep(min(poll_interval, max(_BASE_POLL_INTERVAL_SECONDS, deadline - now)))
    request.chunks_printed_ref[0] = chunks_printed
    if request.visible_chunks_ref is not None:
        request.visible_chunks_ref[0] = visible_chunks
    if request.chunk_offset_ref is not None:
        request.chunk_offset_ref[0] = chunk_offset
    return response


# LLM: deadline 属于当前进程单调时钟；只按请求租约/chunk 文件活动续租，
# 不用自然语言、系统日期或其它请求的心跳决定本请求是否活跃。
# 函数用途: 活跃请求刷新不活跃等待窗，避免慢任务因系统校时提前断开或无限延后。
def _extend_active_request_deadline(
    request: GatewayChunkPollRequest,
    activity_fingerprints: dict[Path, tuple[int, int] | None],
    *,
    deadline: float,
) -> float:
    timeout = max(0.0, float(request.inactivity_timeout_seconds or 0.0))
    if timeout <= 0:
        return deadline
    changed = False
    for path in request.activity_paths:
        current = _path_activity_fingerprint(path)
        previous = activity_fingerprints.get(path)
        if current is not None and current != previous:
            changed = True
        activity_fingerprints[path] = current
    if not changed:
        return deadline
    return max(deadline, time.monotonic() + timeout)


def _path_activity_fingerprint(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size


def gateway_request_activity_paths(paths: object, request_id: str, chunk_path: Path) -> tuple[Path, ...]:
    candidates = [chunk_path]
    for name in ("inbox", "processing"):
        folder = getattr(paths, name, None)
        if folder is not None:
            candidates.append(Path(folder) / f"{request_id}.json")
    return tuple(candidates)


# LLM: 单次尾读只按 byte offset 推进；每个完整 JSONL row 先交 typed consumer，再按需走 legacy projector。
# 函数用途: 读取 chunk 文件新增部分并返回累计行数、终态可见数和新游标。
def _poll_chunk_file(request: ChunkFilePollRequest) -> tuple[int, int, int]:
    chunks_printed = request.chunks_printed
    visible_chunks = request.visible_chunks
    chunk_offset = request.chunk_offset
    readable_chunk_path = _readable_chunk_path(request.chunk_path)
    if readable_chunk_path is None:
        return chunks_printed, visible_chunks, chunk_offset
    try:
        rows, next_offset, decode_error = read_complete_utf8_rows(
            readable_chunk_path,
            chunk_offset,
            max_bytes=STREAM_CHUNK_READ_MAX_BYTES,
        )
    except OSError as exc:
        print(f"gateway chat chunk load_error path={readable_chunk_path} message={exc}", file=sys.stderr)
        return chunks_printed, visible_chunks, chunk_offset
    if decode_error:
        print("gateway chat chunk load_error category=utf8_decode", file=sys.stderr)
    chunk_offset = next_offset
    for cline in rows:
        consumed, visible = _emit_chunk_line(cline, request.on_chunk, request.on_event)
        chunks_printed += consumed
        visible_chunks += visible
    return chunks_printed, visible_chunks, chunk_offset


def _readable_chunk_path(chunk_path: Path) -> Path | None:
    for candidate in gateway_chunk_path_candidates(chunk_path):
        if candidate.exists():
            return candidate
    return None


# LLM: 未提交candidate kind在任何callback前退休并正常推进游标；其它typed consumer异常仍沿原legacy投影，未知kind语义不变。
# 函数用途: 解码原chunk并只把合法公开事件交消费者，不改私有原档或断连恢复计数。
def _emit_chunk_line(
    cline: str,
    on_chunk: callable,
    on_event: object | None = None,
) -> tuple[int, int]:
    if not cline.strip():
        return 1, 0
    try:
        cobj = json.loads(cline)
    except json.JSONDecodeError as exc:
        print(f"gateway chat chunk load_error category=json_decode message={exc}", file=sys.stderr)
        return 1, 0
    if not isinstance(cobj, dict):
        print("gateway chat chunk load_error category=non_object_root", file=sys.stderr)
        return 1, 0
    if cobj.get("kind") in ("model_delta", "model_draft"):
        return 1, 0
    if callable(on_event):
        try:
            if on_event(cobj) is True:
                terminal = str(cobj.get("kind") or "") == "assistant_final"
                return 1, 1 if terminal else 0
        except Exception as exc:  # noqa: BLE001 UI consumer 失败不能中断 Gateway 主链
            print(
                "gateway chat chunk event_error "
                f"category=consumer_exception message={exc}",
                file=sys.stderr,
            )
    chunk_text, terminal_response_streamed = project_gateway_stream_chunk(cobj)
    if chunk_text:
        visible = on_chunk(chunk_text)
        return 1, 1 if visible is True and terminal_response_streamed else 0
    return 1, 0


def check_gateway_alive(paths) -> bool:
    from ...agent.gateway_parts.status_rendering import gateway_running

    _, alive = gateway_running(paths)
    return alive


def format_gateway_timing(ctx: GatewayTimingContext) -> str:
    ctx_tokens = current_context_token_estimate(ctx.response)
    if ctx.use_gateway:
        return (
            f"[耗时 {ctx.elapsed:.2f}s; "
            f"工具轮数 {ctx.response.get('tool_rounds', 0)}; "
            f"ctx_tokens~{ctx_tokens}; "
            f"prompt_tokens~{ctx.response.get('prompt_token_estimate', 0)}; "
            f"resume_context={1 if ctx.response.get('memory_resume_context_injected') else 0}]"
        )
    return (
        f"[耗时 {ctx.elapsed:.2f}s; 工具轮数 {ctx.response.get('tool_rounds', 0)}; "
        f"ctx_tokens~{ctx_tokens}; "
        f"prompt_tokens~{ctx.response.get('prompt_token_estimate', 0)}; "
        f"resume_context={1 if ctx.response.get('memory_resume_context_injected') else 0}]"
    )


__all__ = [
    "ChatRequestContent",
    "GatewayChatObserver",
    "GatewayChunkPollRequest",
    "GatewayTimingContext",
    "check_gateway_alive",
    "format_gateway_timing",
    "poll_gateway_chunks",
    "submit_chat_request",
]


# LLM: Network selection is explicit or based on URL host, never on local queue availability. No DNS lookup or local credential read occurs here.
# 函数用途: 为远程Gateway或强制网络模式选择纯HTTP观察器，本地默认保持原行为。
def network_gateway_observer(agent):
    import ipaddress
    import os
    from urllib.parse import urlsplit
    config = getattr(agent, "config", None)
    url = str(getattr(agent, "gateway_url", "") or getattr(config, "gateway_url", "")
              or os.environ.get("MY_AGENT_GATEWAY_URL", "") or f"http://127.0.0.1:{getattr(config, 'gateway_port', 0)}").rstrip("/")
    host = urlsplit(url).hostname or ""
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host.lower() == "localhost"
    if os.environ.get("MY_AGENT_TUI_NETWORK") != "1" and loopback:
        return None
    observer = NetworkGatewayObserver(agent, url)
    observer.bind_client_transport()
    return observer


# LLM: A request-scoped HTTP observer owns only replay positions. It must never read server queue/chunks/heartbeat/terminal or export the host G1 credential.
# 类用途: 用认证SSE重连读取同一请求，繁忙或认证刷新失败后本请求仅走HTTP轮询。
class NetworkGatewayObserver:
    # LLM: Construction does not read credentials, create threads, touch files or issue requests.
    # 函数用途: 保存目标地址和原身份来源；运输凭据仅在发HTTP时读取显式远程配置。
    def __init__(self, agent, base_url):
        self.agent, self.base_url = agent, base_url
        self.cursor, self.line_cursor = "", 0
        self._http_only = False
        self._refreshed = False

    # LLM: Only an explicit remote-header provider/configured bearer may authenticate network mode; never call gateway_client_credentials or local credential files.
    # 函数用途: 生成原用户/群身份头及远程认证头，不读取宿主G1凭据。
    def _headers(self):
        identity = _gateway_submit_identity(self.agent)
        headers = {"X-User-Id": identity["user_id"], "X-Channel": identity["channel"]}
        if identity.get("channel_chat_type"):
            headers["X-Channel-Chat-Type"] = identity["channel_chat_type"]
        if identity.get("channel_chat_id"):
            headers["X-Channel-Chat-Id"] = identity["channel_chat_id"]
        provider = getattr(self.agent, "gateway_network_headers", None)
        if callable(provider):
            identity_headers = {key.lower() for key in headers}
            headers.update({key: value for key, value in provider().items() if key.lower() not in identity_headers})
        else:
            token = getattr(getattr(self.agent, "config", None), "gateway_auth_token", "")
            if isinstance(token, str) and token:
                headers["Authorization"] = f"Bearer {token}"
        return headers

    # LLM: Reuse authenticated no-redirect transport; this method never queues a file request or retries a write.
    # 函数用途: 执行一次有界JSON请求，所有控制和审批沿同一HTTP入口。
    def request_json(self, method, path, payload=None, *, timeout=None):
        import urllib.error
        import urllib.request

        from ...agent.common.authenticated_http import open_authenticated_request
        headers = self._headers()
        data = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(self.base_url + path, data=data, headers=headers, method=method)
        timeout = max(0.1, float(timeout if timeout is not None else getattr(self.agent.config, "gateway_request_timeout", 30)))
        try:
            with open_authenticated_request(request, timeout=timeout) as response:
                raw = response.read(STREAM_CHUNK_READ_MAX_BYTES + 1)
                if len(raw) > STREAM_CHUNK_READ_MAX_BYTES:
                    return 0, {}
                value = json.loads(raw.decode("utf-8"))
                return response.status, value if isinstance(value, dict) else {}
        except urllib.error.HTTPError as error:
            return error.code, {}
        except (OSError, ValueError, urllib.error.URLError):
            return 0, {}

    # LLM: The existing authenticated ask path still freezes identity and invokes allocation callback before this single HTTP effect.
    # 函数用途: 只提交一次原输入，不做受理不明重试或文件队列降级。
    def submit(self, payload):
        return self.request_json("POST", "/ask", payload)

    # LLM: Only existing HTTP /approve or /deny controls may decide a pending request; never downgrade persistent grants to approval.
    # 函数用途: 核对精确审批编号后发送原HTTP控制，服务端仍重新核owner、会话、截止和绑定。
    def permission_sink(self, request_value, decision_value, *, conversation_id):
        from ...agent.gateway_parts.permission_bridge import _approval_decision, _approval_request
        request = _approval_request(request_value)
        decision = _approval_decision(decision_value)
        if request.permission_id != decision.permission_id:
            raise ValueError("gateway permission decision id mismatch")
        if decision.decision not in {"approved", "denied"}:
            raise ValueError("该远程审批选项不支持通过原HTTP控制表达，未发送决定")
        command = ("/approve " if decision.approved else "/deny ") + request.permission_id
        status, body = self.request_json("POST", "/control", {"command": command,
            "conversation_id": conversation_id, "request_id": request.request_id})
        if status != 200 or not body.get("ok"):
            raise PermissionError("Gateway未确认审批决定")

    # LLM: Existing thin-client methods keep their schemas and authority checks; only their transport is rebound for this client instance.
    # 函数用途: 为当前客户端绑定远程只读查询和控制HTTP，不更改共享类或其他用户客户端。
    def bind_client_transport(self):
        self.agent.get_gateway_json = lambda path, *, timeout, **_: self.request_json("GET", path, timeout=timeout)
        self.agent.post_gateway_json = lambda path, payload, *, timeout: self.request_json("POST", path, payload, timeout=timeout)

    # LLM: LF/blank-line terminated frames alone are observable. An incomplete UTF-8/data/id fragment never commits the cursor.
    # 函数用途: 解析有界SSE完整帧，断连时丢弃半帧并保留上一条已消费游标。
    def _frames(self, response):
        event, cursor, data, size = "message", "", [], 0
        while True:
            raw = response.readline(STREAM_CHUNK_READ_MAX_BYTES + 1)
            if not raw:
                return
            size += len(raw)
            if size > STREAM_CHUNK_READ_MAX_BYTES or not raw.endswith(b"\n"):
                return
            line = raw.rstrip(b"\r\n").decode("utf-8")
            if not line:
                if data:
                    yield event, json.loads("\n".join(data)), cursor
                event, cursor, data, size = "message", "", [], 0
            elif line.startswith("event:"):
                event = line[6:].lstrip(" ")
            elif line.startswith("id:"):
                cursor = line[3:].lstrip(" ")
            elif line.startswith("data:"):
                data.append(line[5:].lstrip(" "))

    # LLM: Count/dispatch through the exact existing public event consumer; neither transport nor heartbeat implies model completion.
    # 函数用途: 将公开事件交原TUI/plain回调，保留原显示计数。
    def _emit(self, request, event):
        count, visible = _emit_chunk_line(json.dumps(event, ensure_ascii=False), request.on_chunk, request.on_event)
        request.chunks_printed_ref[0] += count
        if request.visible_chunks_ref is not None:
            request.visible_chunks_ref[0] += visible

    # LLM: Terminal is accepted only from the original HTTP result projection or terminal SSE frame; no local stat/open/read fallback exists.
    # 函数用途: 优先SSE续读原请求；503、重复连接失败或认证刷新失败后仅用HTTP进度与结果兜底。
    def __call__(self, request):
        import urllib.error
        import urllib.parse
        import urllib.request

        from ...agent.common.authenticated_http import open_authenticated_request
        from ...agent.gateway_parts.request_worker import _WAIT_POLL_STEPS_SECONDS
        request_id = request.terminal_path.stem
        quoted = urllib.parse.quote(request_id, safe="")
        include = "assistant_text_delta,assistant_text_reset,assistant_text_done,workflow_step_progress"
        deadline = request.deadline
        failures = 0
        while True:
            if callable(request.is_wait_cancelled) and request.is_wait_cancelled():
                return {}
            if time.time() >= deadline:
                if request.on_wait_timeout is None:
                    return {}
                request.on_wait_timeout()
                deadline = time.time() + max(_WAIT_POLL_STEPS_SECONDS[-1], request.inactivity_timeout_seconds)
            if not self._http_only:
                headers = {**self._headers(), "Accept": "text/event-stream"}
                if self.cursor:
                    headers["Last-Event-ID"] = self.cursor
                target = urllib.request.Request(self.base_url + f"/client/requests/{quoted}/events?include={include}", headers=headers)
                try:
                    with open_authenticated_request(target, timeout=max(0.1, deadline - time.time())) as response:
                        for kind, data, cursor in self._frames(response):
                            if callable(request.is_wait_cancelled) and request.is_wait_cancelled():
                                return {}
                            if kind == "progress" and isinstance(data, dict) and isinstance(data.get("event"), dict):
                                self._emit(request, data["event"])
                                if request.inactivity_timeout_seconds > 0:
                                    deadline = time.time() + request.inactivity_timeout_seconds
                            elif kind == "reset":
                                self.cursor, self.line_cursor = "", 0
                                if callable(request.on_event):
                                    request.on_event({"kind": "assistant_text_reset", "request_id": request_id})
                            elif kind == "terminal" and isinstance(data, dict) and isinstance(data.get("result"), dict):
                                return data["result"]
                            if cursor.startswith(f"v1:{request_id}:") and "\x00" not in cursor:
                                parts = cursor.split(":")
                                if len(parts) == 5 and parts[3].isdecimal() and parts[4].isdecimal():
                                    self.cursor, self.line_cursor = cursor, int(parts[3])
                            if time.time() >= deadline:
                                break
                    failures += 1
                except urllib.error.HTTPError as error:
                    if error.code == 503:
                        self._http_only = True
                    elif error.code in (401, 403):
                        refresh = getattr(self.agent, "refresh_gateway_network_credentials", None)
                        if self._refreshed or not callable(refresh) or not refresh():
                            self._http_only = True
                        self._refreshed = True
                    else:
                        failures += 1
                except (OSError, UnicodeError, ValueError, urllib.error.URLError):
                    failures += 1
                if failures >= 2:
                    self._http_only = True
                if not self._http_only:
                    time.sleep(_WAIT_POLL_STEPS_SECONDS[0])
                    continue
            status, batch = self.request_json("GET", f"/progress/{quoted}?since={self.line_cursor}&include={include}", timeout=max(0.1, deadline-time.time()))
            if status in (401, 403):
                raise PermissionError("Gateway网络观察未获授权")
            if status == 200:
                for event in batch.get("events", []):
                    self._emit(request, event)
                self.line_cursor = max(self.line_cursor, int(batch.get("next") or 0))
            status, result = self.request_json("GET", f"/result/{quoted}", timeout=max(0.1, deadline-time.time()))
            if status == 200:
                return result
            if status in (401, 403):
                raise PermissionError("Gateway网络结果未获授权")
            time.sleep(_WAIT_POLL_STEPS_SECONDS[-1])
