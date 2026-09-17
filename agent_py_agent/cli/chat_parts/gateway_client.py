

# LLM: 本模块是 chat 客户端与 Gateway 文件协议的边界；typed row 可旁路 legacy 文本投影，但不能改变 Gateway 业务事实或授权。
# 模块用途: 提交聊天请求、增量读取 Gateway chunk/response 文件，并把结构化事件或兼容文本交给调用方。

from __future__ import annotations

import json
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ...agent.gateway_parts.io import read_complete_utf8_rows
from ...agent.gateway_parts.paths import gateway_chunk_path, gateway_chunk_path_candidates
from ...agent.gateway_parts.request_client import GatewayAskParams, submit_gateway_ask
from ...agent.gateway_parts.response_renderer import (
    GatewayResponsePollState,
    current_context_token_estimate,
    project_gateway_stream_chunk,
    read_gateway_terminal_response_file_when_ready,
)


# LLM: ChatRequestContent 声明当前 CLI 表面真实可消费的交互能力；只有 TUI 主链开启审批和富 transcript，plain/其它调用保持最小输出。
# 类用途: 汇总一次聊天请求正文、上下文、会话和交互能力。
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


# LLM: GatewayChunkPollRequest carries the canonical terminal path as the only completion fact;
# on_event consumes typed chunks while on_chunk remains the plain-UI compatibility projection.
# 类用途: 描述一次 Gateway 流式轮询所需的终态路径、游标、回调和活跃租约。
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


# LLM: submit 保留可信身份/工作区；可选分配回调在原子入队前登记显示关联，不改变权限、请求正文或模型配置。
# 函数用途: 提交聊天请求并返回真实编号和路径，让发起页提前识别后到的同源消息。
def submit_chat_request(
    paths,
    content: ChatRequestContent,
    agent,
    *,
    workspace_root: object = "",
    workspace_roots: object = None,
    on_request_allocated: Callable[[str], None] | None = None,
) -> tuple[str, Path, Path]:
    identity = _gateway_submit_identity(agent)
    workspace = _gateway_submit_workspace(
        agent,
        workspace_root=workspace_root,
        workspace_roots=workspace_roots,
    )
    request_id, _, response_path = submit_gateway_ask(
        paths,
        params=GatewayAskParams(
            prompt=content.prompt,
            inject=content.inject,
            prompt_files=content.prompt_files,
            save=content.save,
            include_prompt=content.show_prompt,
            resume_context=content.resume_context,
            chat_session_id=content.chat_session_id,
            channel=identity["channel"],
            channel_user_id=identity["user_id"],
            canonical_user_id=identity["user_id"],
            channel_chat_type=identity.get("channel_chat_type", ""),
            channel_chat_id=identity.get("channel_chat_id", ""),
            agent=(
                None
                if getattr(agent, "gateway_client_only", False) is True
                else agent
            ),
            system_task=content.system_task,
            interactive_approvals=content.interactive_approvals,
            rich_transcript=content.rich_transcript,
            workspace_root=str(workspace.get("cwd") or ""),
            workspace_roots=[str(item) for item in workspace.get("roots", [])],
        ),
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


# LLM: 终态仍只认 canonical envelope；入口绝对 deadline 仅转换一次为单调时钟，
# 后续活动续租不受 NTP/手动校时影响。chunk/response 投影不得成为完成事实。
# 函数用途: 持续读取新增事件与唯一终态，使用稳定计时等待，并写回调用方游标。
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
    while True:
        deadline = _extend_active_request_deadline(
            request,
            activity_fingerprints,
            deadline=deadline,
        )
        if time.monotonic() > deadline:
            break
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
        time.sleep(0.1)
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


# 单次读取上限:流式 chunk 文件可能很大,整体 f.read() 无上限会 MemoryError。
_MAX_CHUNK_READ_BYTES = 8 * 1024 * 1024


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
            max_bytes=_MAX_CHUNK_READ_BYTES,
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


# LLM: typed consumer 返回 True 表示已接管该 object；回调异常仅记录诊断并继续 legacy 投影，不能打断业务请求。
# 函数用途: 解码一行 Gateway JSON，优先分发结构化事件，否则投影为兼容文本。
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
    "GatewayChunkPollRequest",
    "GatewayTimingContext",
    "check_gateway_alive",
    "format_gateway_timing",
    "poll_gateway_chunks",
    "submit_chat_request",
]
