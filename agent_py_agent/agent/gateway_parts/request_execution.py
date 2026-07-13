
from __future__ import annotations

"""execution helpers keep one claimed gateway request inside focused contexts.

Gateway responses expose both per-turn and cumulative token estimates. CLI/TUI
status should use the cumulative field when showing current context pressure.
"""

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from ..agent_core.runtime_mixin import RunParams
from ..conversation.authority import CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR
from ..conversation.channels import project_user_reply
from .audit_service import (
    AuditRequestCompletedParams,
    audit_request_completed,
    audit_request_processing,
)
from .io import gateway_response_path, read_json_file, read_json_file_report
from .lease_service import refresh_processing_lease, start_lease_heartbeat
from .paths import gateway_chunk_path, gateway_paths
from .recovery import _gateway_request_attempts
from .request_errors import ConversationPersistenceError, gateway_request_load_error_response
from .response_renderer import read_gateway_response_file

if TYPE_CHECKING:
    from ...core import SimpleAgent

_EMPTY_PROMPT_MESSAGE = "gateway ask prompt/goal cannot be empty"
_CHUNK_STREAM_FLUSH_INTERVAL_SECONDS = 0.08
_CHUNK_STREAM_FLUSH_CHARS = 128
logger = logging.getLogger(__name__)


def open_chunk_stream(chunk_path: Path) -> tuple[Path, float]:
    chunk_path.parent.mkdir(parents=True, exist_ok=True)
    return chunk_path, time.time()


def write_chunk(chunk_path: Path, text: str) -> None:
    try:
        line = json.dumps({"t": time.time(), "text": text}, ensure_ascii=False)
        with open(chunk_path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass


def close_chunk_stream(chunk_path: Path) -> None:
    # Keep the chunk file after completion so clients that observe the final
    # response first can still drain the last streamed tokens.
    return


@dataclass
class BufferedChunkStreamWriter:
    chunk_path: Path
    flush_interval_seconds: float = _CHUNK_STREAM_FLUSH_INTERVAL_SECONDS
    flush_chars: int = _CHUNK_STREAM_FLUSH_CHARS
    _buffer: list[str] = field(default_factory=list)
    _buffer_chars: int = 0
    _last_flush_at: float = field(default_factory=time.monotonic)

    def write(self, text: str) -> None:
        if not text:
            return
        self._buffer.append(text)
        self._buffer_chars += len(text)
        if self._should_flush(text):
            self.flush()

    def flush(self) -> None:
        if not self._buffer:
            return
        text = "".join(self._buffer)
        self._buffer.clear()
        self._buffer_chars = 0
        self._last_flush_at = time.monotonic()
        write_chunk(self.chunk_path, text)

    def close(self) -> None:
        self.flush()
        close_chunk_stream(self.chunk_path)

    def _should_flush(self, latest_text: str) -> bool:
        if self._buffer_chars >= max(1, int(self.flush_chars)):
            return True
        if latest_text.endswith("\n"):
            return True
        elapsed = time.monotonic() - self._last_flush_at
        return elapsed >= max(0.0, float(self.flush_interval_seconds))


@dataclass(frozen=True)
class _GatewayResponseBaseContext:

    request: dict
    request_path: Path
    request_id: str
    kind: str
    started_at: float


@dataclass(frozen=True)
class _GatewayAskRunContext:

    agent: SimpleAgent
    request: dict
    request_path: Path
    response_path: Path
    request_id: str
    on_chunk: object


@dataclass(frozen=True)
class _GatewayConversationContext:

    thread_id: str = ""
    lane: str = "chat"
    task_ref: str = ""
    active_task_id: str = ""
    active_task_goal: str = ""
    task_workspace: str = ""
    output_dir: str = ""
    work_dir: str = ""
    history: tuple[tuple[str, str], ...] = ()
    recent_artifacts: tuple[dict[str, object], ...] = ()
    task_candidates: tuple[tuple[str, str, str], ...] = ()
    load_errors: tuple[dict, ...] = ()


@dataclass(frozen=True)
class _GatewayConversationLoadRequest:
    agent: SimpleAgent
    request: dict
    request_id: str
    prompt: str


@dataclass(frozen=True)
class _GatewayRunParamsRequest:
    request: dict
    context: _GatewayAskRunContext
    conversation: _GatewayConversationContext
    prompt: str


@dataclass(frozen=True)
class _BindGatewayTaskRequest:
    store: object
    thread_id: str
    request_id: str
    prompt: str
    load_errors: list[dict]


@dataclass(frozen=True)
class _GatewayTaskLinkRequest:
    agent: SimpleAgent
    store: object
    thread_id: str
    lane: str
    task_ref: str
    request_id: str
    prompt: str
    load_errors: list[dict]


@dataclass(frozen=True)
class _GatewayLeaseStartContext:

    agent: SimpleAgent
    request: dict
    request_path: Path
    request_id: str
    refresh_lease: bool
    worker_id: str


def _build_gateway_response_base(context: _GatewayResponseBaseContext) -> dict:
    request = context.request
    return {
        "id": context.request_id,
        "kind": context.kind or "unknown",
        "ok": False,
        "status": "failed",
        "created_at": request.get("created_at", 0),
        "started_at": context.started_at,
        "ended_at": 0,
        "duration_seconds": 0,
        "response": "",
        "error_code": "",
        "error": "",
        "backend": "",
        "used_memories": 0,
        "tool_rounds": 0,
        "prompt": "",
        "request_file": str(context.request_path),
        "attempts": _gateway_request_attempts(request),
        "lease_owner": request.get("lease_owner", ""),
        "lease_started_at": request.get("lease_started_at", 0),
        "lease_heartbeat_at": request.get("lease_heartbeat_at", 0),
    }


# LLM: Gateway 对外 response 只放 user-facing projection；原始内部 closeout 仍留在 run 内部结果。
# 函数用途: 将一次模型运行结果整理成可供客户端读取的最终响应。
def _update_response_from_result(response: dict, result, request: dict) -> None:
    channel_delivery = dict(getattr(result, "channel_delivery", {}) or {})
    public_delivery = _public_channel_delivery(channel_delivery)
    response.update(
        {
            "ok": True,
            "status": "done",
            "response": str(channel_delivery.get("content") or result.response or ""),
            "backend": result.backend,
            "used_memories": result.used_memories,
            "tool_rounds": result.tool_rounds,
            "prompt": result.prompt if request.get("include_prompt") else "",
            "current_context_token_estimate": result.prompt_token_estimate,
            "prompt_token_estimate": result.prompt_token_estimate,
            "runtime_injection_token_estimate": result.runtime_injection_token_estimate,
            "turn_token_estimate": result.turn_token_estimate,
            "cumulative_token_estimate": result.cumulative_token_estimate,
            "memory_resume_context_injected": result.memory_resume_context_injected,
            "memory_resume_context_query": result.memory_resume_context_query,
            "memory_resume_context_matches": result.memory_resume_context_matches,
            "memory_resume_context_token_estimate": result.memory_resume_context_token_estimate,
            "memory_resume_context_error": result.memory_resume_context_error,
            "conversation_persist_degraded": bool(
                getattr(result, "conversation_persist_degraded", False)
            ),
            "conversation_persist_error": str(
                getattr(result, "conversation_persist_error", "") or ""
            ),
            "channel_delivery": public_delivery,
        }
    )


# LLM: Gateway 客户端不需要服务器 path；跨轮复用引用只进 owner transcript metadata，不进公开响应。
# 函数用途: 生成不含绝对路径和验收细节的通道交付响应字段。
def _public_channel_delivery(value: dict[str, object]) -> dict[str, object]:
    artifacts = value.get("artifacts")
    names: list[str] = []
    if isinstance(artifacts, list):
        names = [
            str(item.get("name") or "")
            for item in artifacts
            if isinstance(item, dict) and str(item.get("name") or "")
        ]
    return {
        "content": str(value.get("content") or ""),
        "artifact_names": names,
        "internal_signal": value.get("internal_signal") is True,
        "projection_status": str(value.get("projection_status") or "plain_text"),
    }


def _start_gateway_request_lease(
    context: _GatewayLeaseStartContext,
) -> tuple[threading.Event | None, threading.Thread | None]:
    should_refresh = context.refresh_lease or str(context.request.get("status") or "") == "processing"
    if not should_refresh:
        return None, None
    lease_worker = context.worker_id or str(context.request.get("lease_owner") or "")
    refresh_processing_lease(context.request_path, request_id=context.request_id, worker_id=lease_worker)
    return start_lease_heartbeat(context.agent, context.request_path, request_id=context.request_id, worker_id=lease_worker)


# LLM: assistant 落账前先拆出用户正文与产物 metadata，内部协议不得进入权威 transcript。
# 函数用途: 执行一轮 Gateway 对话，并可靠保存用户消息、回复投影和近期产物引用。
def _run_gateway_ask(context: _GatewayAskRunContext):
    request = context.request
    prompt = str(request.get("prompt") or request.get("goal") or "").strip()
    if not prompt:
        raise ValueError(_EMPTY_PROMPT_MESSAGE)
    conversation = _gateway_conversation_context(
        _GatewayConversationLoadRequest(context.agent, request, context.request_id, prompt)
    )
    _require_gateway_conversation_ready(request, conversation)
    if not _append_gateway_conversation_message(
        context.agent,
        request,
        conversation,
        request_id=context.request_id,
        role="user",
        content=prompt,
    ):
        raise ConversationPersistenceError("当前消息无法可靠写入会话记录，请稍后重试")
    result = context.agent.run(
        prompt,
        params=_gateway_run_params(_GatewayRunParamsRequest(request, context, conversation, prompt)),
    )
    delivery_projection = project_user_reply(str(result.response or ""))
    result.channel_delivery = delivery_projection.to_dict()
    if not _append_gateway_conversation_message(
        context.agent,
        request,
        conversation,
        request_id=context.request_id,
        role="assistant",
        content=delivery_projection.content,
        delivery_artifacts=delivery_projection.artifacts,
    ):
        _queue_gateway_conversation_repair(
            context.agent,
            request,
            conversation,
            request_id=context.request_id,
            role="assistant",
            content=delivery_projection.content,
            delivery_artifacts=delivery_projection.artifacts,
        )
        result.conversation_persist_degraded = True
        result.conversation_persist_error = "assistant transcript append deferred for repair"
    return result


def _require_gateway_conversation_ready(
    request: dict,
    conversation: _GatewayConversationContext,
) -> None:
    if not isinstance(request.get("conversation"), dict):
        return
    if not conversation.thread_id or conversation.load_errors:
        raise ConversationPersistenceError("会话记录当前不可用，请稍后重试")


def _gateway_run_params(inputs: _GatewayRunParamsRequest) -> RunParams:
    request = inputs.request
    context = inputs.context
    conversation = inputs.conversation
    return RunParams(
        inject=_gateway_injections(request, conversation),
        prompt_files=[str(item) for item in request.get("prompt_files", [])],
        save=bool(request.get("save", True)),
        request_id=context.request_id,
        source="gateway",
        resume_context=_gateway_resume_context(request, conversation),
        recovery_task_refs=_gateway_recovery_task_refs(conversation),
        recovery_next_actions=[
            "If this gateway request must be recovered, inspect the gateway response and LocalStore gateway_request records first."
        ],
        recovery_content_paths=[str(context.request_path), str(context.response_path)],
        on_chunk=context.on_chunk,
        root_user_prompt=_root_user_prompt(inputs.prompt, conversation),
        task_attributes=_stamp_audit_intent(_gateway_task_attributes(conversation), inputs.prompt),
    )


def _gateway_injections(request: dict, conversation: _GatewayConversationContext) -> list[str]:
    items = [str(item) for item in request.get("inject", [])]
    section = _conversation_prompt_section(conversation)
    return [*items, section] if section else items


def _gateway_task_attributes(conversation: _GatewayConversationContext) -> dict | None:
    attrs: dict[str, object] = {}
    if conversation.thread_id:
        attrs["conversation_thread_id"] = conversation.thread_id
        attrs["conversation_lane"] = conversation.lane
        attrs[CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR] = True
    if conversation.lane == "task" and conversation.active_task_id:
        attrs["conversation_task_id"] = conversation.active_task_id
    if conversation.lane == "task" and conversation.task_workspace:
        attrs["run_workspace"] = {
            "task_root": conversation.task_workspace,
            "output_dir": conversation.output_dir or str(Path(conversation.task_workspace) / "output"),
            "work_dir": conversation.work_dir or str(Path(conversation.task_workspace) / "work"),
        }
    return attrs or None


def _gateway_resume_context(
    request: dict,
    conversation: _GatewayConversationContext,
) -> bool | None:
    """会话历史已是权威上下文时，默认不再自动续接 owner 旧归档。"""
    if "resume_context" in request:
        return bool(request.get("resume_context"))
    return False if conversation.thread_id else None


def _stamp_audit_intent(attrs: dict | None, prompt: str) -> dict | None:
    """用户在网关任务里显式点了 /audit → 结构化盖进 task_attributes 的保证档标志(前台创建路
    root_user_prompt 就是用户原文,这一刻检测最可靠)。此后跨轮/委派子代理都靠这个结构化标志
    继承激活,不再从会被回填的后台 prompt 里重新猜(治真机静默没激活)。非 /audit 任务不动。"""
    from ..common.audit_activation import AUDIT_ATTR, text_requests_audit

    if not text_requests_audit(prompt):
        return attrs
    stamped = dict(attrs or {})
    stamped[AUDIT_ATTR] = True
    return stamped


def _gateway_recovery_task_refs(conversation: _GatewayConversationContext) -> list[str] | None:
    refs = [conversation.active_task_id] if conversation.lane == "task" and conversation.active_task_id else []
    return refs or None


def _root_user_prompt(prompt: str, conversation: _GatewayConversationContext) -> str:
    """当前用户消息始终是本轮唯一 root prompt；旧任务只能走结构化 task_ref。"""
    return prompt


# LLM: 同一 thread 的历史与近期产物分别加载；普通聊天不因产物引用自动绑定旧 task。
# 函数用途: 组装本轮 Gateway 对话所需的权威历史、候选任务和产物上下文。
def _gateway_conversation_context(inputs: _GatewayConversationLoadRequest) -> _GatewayConversationContext:
    agent = inputs.agent
    request = inputs.request
    request_id = inputs.request_id
    prompt = inputs.prompt
    spec = request.get("conversation")
    if not isinstance(spec, dict):
        return _GatewayConversationContext()
    store = getattr(agent, "conversation_store", None)
    if store is None:
        return _GatewayConversationContext(load_errors=({"error_code": "conversation_store_unavailable"},))
    load_errors: list[dict] = []
    try:
        thread = store.get_or_create_thread(
            {
                "canonical_user_id": str(spec.get("canonical_user_id") or "local-agent"),
                "channel": str(spec.get("channel") or "chat"),
                "channel_conversation_id": str(spec.get("channel_conversation_id") or ""),
                "channel_user_id": str(spec.get("channel_user_id") or "local-cli"),
                "title": prompt[:80] or request_id,
            }
        )
    except Exception as exc:
        return _GatewayConversationContext(load_errors=(_conversation_error(exc, "gateway.conversation.thread"),))
    _repair_gateway_conversation_messages(store, thread.thread_id, load_errors)
    lane = str(spec.get("lane") or "chat").strip().lower()
    lane = lane if lane in {"chat", "task"} else "chat"
    if lane == "chat" and _special_task_mode(prompt):
        lane = "task"
    task_ref = str(spec.get("task_ref") or "").strip()
    history, recent_artifacts = _gateway_conversation_refs(
        agent, thread.thread_id, request_id, load_errors
    )
    task_candidates = _gateway_active_task_candidates(store, thread.thread_id, load_errors)
    active_link = _gateway_task_link(
        _GatewayTaskLinkRequest(
            agent, store, thread.thread_id, lane, task_ref, request_id, prompt, load_errors
        )
    )
    workspace = _task_workspace_for(active_link)
    return _GatewayConversationContext(
        thread_id=thread.thread_id,
        lane=lane,
        task_ref=task_ref,
        active_task_id=active_link.task_id if active_link is not None else "",
        active_task_goal=active_link.goal if active_link is not None else "",
        task_workspace=str(workspace) if workspace else "",
        output_dir=str(workspace / "output") if workspace else "",
        work_dir=str(workspace / "work") if workspace else "",
        history=history,
        recent_artifacts=recent_artifacts,
        task_candidates=task_candidates,
        load_errors=tuple(load_errors),
    )


# LLM: 对话正文和产物引用都来自同一 thread，但保持两种 typed 结果，禁止把 path 混进历史正文。
# 函数用途: 读取本轮所需的有界历史与近期产物引用。
def _gateway_conversation_refs(
    agent: SimpleAgent,
    thread_id: str,
    request_id: str,
    load_errors: list[dict],
) -> tuple[tuple[tuple[str, str], ...], tuple[dict[str, object], ...]]:
    history = _gateway_conversation_history(agent, thread_id, request_id, load_errors)
    artifacts = _gateway_recent_artifacts(agent, thread_id, request_id, load_errors)
    return history, artifacts


def _gateway_active_task_candidates(
    store: object,
    thread_id: str,
    load_errors: list[dict],
) -> tuple[tuple[str, str, str], ...]:
    try:
        links, errors = store.active_task_links_report(thread_id)
    except Exception as exc:
        load_errors.append(_conversation_error(exc, "gateway.conversation.task_candidates"))
        return ()
    load_errors.extend(error for error in errors if isinstance(error, dict))
    active = [
        link
        for link in links
        if str(getattr(link, "status", "") or "").strip().lower() == "active"
    ]
    active.sort(key=lambda item: float(getattr(item, "created_at", 0.0) or 0.0), reverse=True)
    return tuple(
        (
            str(getattr(link, "task_id", "") or ""),
            str(getattr(link, "goal", "") or ""),
            str(getattr(link, "task_path", "") or ""),
        )
        for link in active[:8]
    )


def _gateway_task_link(inputs: _GatewayTaskLinkRequest):
    """仅结构化 Task lane 绑定；普通常规对话永远返回 None。"""
    if inputs.lane != "task":
        return None
    if inputs.task_ref:
        return _thread_task_by_ref(
            inputs.agent, inputs.thread_id, inputs.task_ref, inputs.load_errors
        )
    return _bind_gateway_request_task(
        _BindGatewayTaskRequest(
            inputs.store,
            inputs.thread_id,
            inputs.request_id,
            inputs.prompt,
            inputs.load_errors,
        )
    )


def _special_task_mode(prompt: str) -> bool:
    """只保留显式特殊模式；普通自然语言工作绝不靠关键词/触发词分类。"""
    first = str(prompt or "").strip().split(maxsplit=1)[0].lower() if str(prompt or "").strip() else ""
    return first in {"/audit", "/goal"}


def _thread_task_by_ref(agent: SimpleAgent, thread_id: str, task_ref: str, load_errors: list[dict]):
    store = getattr(agent, "conversation_store", None)
    try:
        links, errors = store.active_task_links_report(thread_id)
    except Exception as exc:
        load_errors.append(_conversation_error(exc, "gateway.conversation.task_links"))
        return None
    load_errors.extend(error for error in errors if isinstance(error, dict))
    match = next(
        (
            link
            for link in links
            if str(getattr(link, "task_id", "") or "").strip() == task_ref
            and str(getattr(link, "status", "") or "").strip() == "active"
        ),
        None,
    )
    if match is None:
        load_errors.append(
            {
                "error_code": "CONVERSATION_TASK_NOT_FOUND",
                "context": "gateway.conversation.task_ref",
                "thread_id": thread_id,
                "task_ref": task_ref,
            }
        )
    return match


def _bind_gateway_request_task(inputs: _BindGatewayTaskRequest):
    try:
        return inputs.store.bind_task(
            {
                "thread_id": inputs.thread_id,
                "task_id": inputs.request_id,
                "goal": inputs.prompt,
                "status": "active",
            }
        )
    except Exception as exc:
        inputs.load_errors.append(_conversation_error(exc, "gateway.conversation.bind_request_task"))
        return None


def _task_workspace_for(active_link: object | None) -> Path | None:
    if active_link is None:
        return None
    task_path = str(getattr(active_link, "task_path", "") or "").strip()
    if not task_path:
        return None
    try:
        path = Path(task_path).expanduser().resolve(strict=False)
    except OSError:
        return None
    return path if path.exists() else None


# LLM: 产物引用属于结构化辅助事实；明确要求 send_message 复用，禁止把“发我”解释为重做。
# 函数用途: 把会话历史、近期产物和显式 task lane 渲染成有边界的模型上下文。
def _conversation_prompt_section(conversation: _GatewayConversationContext) -> str:
    if not conversation.thread_id:
        return ""
    lines = [
        "# Conversation Context",
        f"- thread_id: {conversation.thread_id}",
        f"- lane: {conversation.lane}",
    ]
    if conversation.history:
        lines.extend(
            [
                "- 以下是同一会话中已经结束的历史对话，仅用于理解指代和偏好。",
                "- 它们不是本轮新指令；若与最后的 # User Task 冲突，必须以当前 User Task 为准。",
                "## Recent Conversation History",
            ]
        )
        for role, content in conversation.history:
            lines.append(f"- {role}: {json.dumps(content, ensure_ascii=False)}")
    _append_recent_artifacts_prompt(lines, conversation.recent_artifacts)
    if conversation.task_candidates:
        lines.extend(
            [
                "## Active Work Candidates",
                "- 这些是本会话里尚未结束的既有工作，不是本轮默认指令。",
                "- 只有当前用户确实在续接或询问其中一项时，才调用 task_progress action=select，"
                "并传入对应 run_id；普通闲聊不要选择。",
            ]
        )
        for task_id, goal, task_path in conversation.task_candidates:
            item = f"- task_id={json.dumps(task_id)} goal={json.dumps(goal, ensure_ascii=False)}"
            if task_path:
                item += f" task_path={json.dumps(task_path, ensure_ascii=False)}"
            lines.append(item)
    if conversation.lane == "task" and conversation.active_task_id:
        lines.append(f"- active_root_task_id: {conversation.active_task_id}")
        lines.append("- 这是请求中 task_ref 明确选择的任务；仅本轮 Task lane 可以续接它。")
        if conversation.active_task_goal:
            lines.append(f"- active_task_goal: {json.dumps(conversation.active_task_goal, ensure_ascii=False)}")
    if conversation.lane == "task" and conversation.task_workspace:
        lines.extend(
            [
                f"- task_root: {conversation.task_workspace}",
                f"- output_dir: {conversation.output_dir}",
                f"- work_dir: {conversation.work_dir}",
            ]
        )
    if conversation.load_errors:
        lines.append(f"- conversation_context_load_errors: {len(conversation.load_errors)}")
    return "\n".join(lines)


# LLM: 最近产物区明确指示复用 send_message；它是结构化上下文，不改变 task lane 或当前用户指令。
# 函数用途: 把近期产物引用追加到模型会话段落。
def _append_recent_artifacts_prompt(
    lines: list[str],
    artifacts: tuple[dict[str, object], ...],
) -> None:
    if not artifacts:
        return
    lines.extend(
        [
            "## Recent Artifact Refs",
            "- 这些是同一会话中上一轮已经生成并登记的可信产物，不是要求你重新生成的任务。",
            "- 用户说‘发我/把上一个文件给我’时，直接调用 send_message，并把对应 path 放进 "
            "attachments；不要重新搜索、复制或制作一遍。",
        ]
    )
    for artifact in artifacts:
        lines.append(f"- {json.dumps(artifact, ensure_ascii=False, sort_keys=True)}")


# LLM: 读取旧 assistant 时再次应用 user projection，兼容升级前已落盘的内部完成协议。
# 函数用途: 返回同一 thread 的有界用户可见历史，不把机器协议重新注入模型。
def _gateway_conversation_history(
    agent: SimpleAgent,
    thread_id: str,
    current_request_id: str,
    load_errors: list[dict],
) -> tuple[tuple[str, str], ...]:
    store = getattr(agent, "conversation_store", None)
    config = getattr(agent, "config", None)
    max_turns = max(
        1,
        int(getattr(config, "conversation_history_max_turns", 20) or 20),
    )
    total_chars = max(
        1000,
        int(getattr(config, "conversation_history_max_chars", 48_000) or 48_000),
    )
    message_chars = max(
        1000,
        int(getattr(config, "conversation_history_message_max_chars", 12_000) or 12_000),
    )
    try:
        rows, errors = store.recent_messages_report(thread_id, limit=max_turns * 2 + 8)
    except Exception as exc:
        load_errors.append(_conversation_error(exc, "gateway.conversation.messages"))
        return ()
    load_errors.extend(error for error in errors if isinstance(error, dict))
    candidates: list[tuple[str, str]] = []
    for row in rows:
        role = str(getattr(row, "role", "") or "").strip().lower()
        metadata = getattr(row, "metadata", None)
        metadata = metadata if isinstance(metadata, dict) else {}
        if role not in {"user", "assistant"} or metadata.get("gateway_request_id") == current_request_id:
            continue
        content = str(getattr(row, "content", "") or "")
        if role == "assistant":
            content = project_user_reply(content).content
        candidates.append((role, _clip_conversation_message(content, message_chars)))
    return _latest_conversation_messages(
        candidates,
        max_messages=max_turns * 2,
        max_chars=total_chars,
    )


# LLM: 近期产物只从当前 thread 的 assistant metadata 或旧版完整完成协议恢复；普通对话文字不获此权威。
# 函数用途: 为“把上一个文件发我”提供已登记产物引用，避免模型重新搜索、复制或生成。
def _gateway_recent_artifacts(
    agent: SimpleAgent,
    thread_id: str,
    current_request_id: str,
    load_errors: list[dict],
) -> tuple[dict[str, object], ...]:
    store = getattr(agent, "conversation_store", None)
    try:
        rows, errors = store.recent_messages_report(thread_id, limit=80)
    except Exception as exc:
        load_errors.append(_conversation_error(exc, "gateway.conversation.recent_artifacts"))
        return ()
    load_errors.extend(error for error in errors if isinstance(error, dict))
    selected: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    candidates = [
        ref
        for row in reversed(rows)
        for ref in reversed(_conversation_row_artifacts(row, current_request_id))
    ]
    for ref in candidates:
        key = (str(ref.get("artifact_id") or ""), str(ref.get("path") or ""))
        if key in seen:
            continue
        seen.add(key)
        selected.append(ref)
        if len(selected) >= 20:
            return tuple(reversed(selected))
    return tuple(reversed(selected))


# LLM: 只有非当前请求的 assistant 行能贡献产物引用；metadata 优先，旧 raw closeout 只作迁移兼容。
# 函数用途: 从一条会话消息提取可信的最小产物引用。
def _conversation_row_artifacts(row: object, current_request_id: str) -> list[dict[str, object]]:
    if str(getattr(row, "role", "") or "").strip().lower() != "assistant":
        return []
    metadata = getattr(row, "metadata", None)
    metadata = metadata if isinstance(metadata, dict) else {}
    if metadata.get("gateway_request_id") == current_request_id:
        return []
    refs = _metadata_artifact_refs(metadata.get("delivery_artifacts"))
    if refs:
        return refs
    return list(project_user_reply(str(getattr(row, "content", "") or "")).artifacts)


# LLM: delivery_artifacts 写入会话 metadata 前只保留最小稳定字段；不得复制验收协议或任意嵌套对象。
# 函数用途: 清洗能跨轮复用的产物引用。
def _metadata_artifact_refs(value: object) -> list[dict[str, object]]:
    if not isinstance(value, (list, tuple)):
        return []
    refs: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        artifact_id = str(item.get("artifact_id") or "").strip()
        path = str(item.get("path") or "").strip()
        if not artifact_id and not path:
            continue
        key = (artifact_id, path)
        if key in seen:
            continue
        seen.add(key)
        refs.append(
            {
                "artifact_id": artifact_id,
                "path": path,
                "name": str(item.get("name") or Path(path).name or artifact_id),
                "kind": str(item.get("kind") or "file"),
                "ok": item.get("ok") is True,
            }
        )
    return refs


def _clip_conversation_message(content: str, limit: int) -> str:
    if len(content) <= limit:
        return content
    marker = "\n…[中间内容已折叠]…\n"
    if limit <= len(marker) + 2:
        return content[:limit]
    head = (limit - len(marker)) // 2
    tail = limit - len(marker) - head
    return f"{content[:head]}{marker}{content[-tail:]}"


def _latest_conversation_messages(
    candidates: list[tuple[str, str]],
    *,
    max_messages: int,
    max_chars: int,
) -> tuple[tuple[str, str], ...]:
    selected: list[tuple[str, str]] = []
    used = 0
    for role, content in reversed(candidates[-max_messages:]):
        remaining = max_chars - used
        if remaining <= 0:
            break
        bounded = _clip_conversation_message(content, remaining)
        selected.append((role, bounded))
        used += len(bounded)
    selected.reverse()
    return tuple(selected)


# LLM: assistant 正文与 delivery_artifacts 分栏落账；metadata 只接受清洗后的最小产物引用。
# 函数用途: 幂等追加一条 Gateway 会话消息，并保存可跨轮复用的产物 metadata。
def _append_gateway_conversation_message(
    agent: SimpleAgent,
    request: dict,
    conversation: _GatewayConversationContext,
    *,
    request_id: str,
    role: str,
    content: str,
    delivery_artifacts: object = (),
) -> bool:
    if not conversation.thread_id or not content:
        return not conversation.thread_id
    store = getattr(agent, "conversation_store", None)
    metadata = request.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    channel_message_id = str(metadata.get("message_id") or "") if role == "user" else ""
    try:
        rows, errors = store.recent_messages_report(conversation.thread_id, limit=100)
        if errors:
            raise OSError("conversation message ledger could not be read reliably")
        if any(
            str(getattr(row, "role", "") or "") == role
            and (
                str((getattr(row, "metadata", {}) or {}).get("gateway_request_id") or "") == request_id
                or (
                    role == "user"
                    and channel_message_id
                    and str(getattr(row, "channel_message_id", "") or "") == channel_message_id
                )
            )
            for row in rows
        ):
            return True
        store.append_message(
            {
                "thread_id": conversation.thread_id,
                "role": role,
                "content": content,
                "channel": str(metadata.get("channel") or request.get("source") or "gateway"),
                "channel_message_id": channel_message_id,
                "metadata": {
                    "gateway_request_id": request_id,
                    "conversation_lane": conversation.lane,
                    "delivery_artifacts": _metadata_artifact_refs(delivery_artifacts),
                },
            }
        )
        return True
    except Exception as exc:
        logger.error(
            "会话消息持久化失败(thread=%s, role=%s): %s: %s",
            conversation.thread_id,
            role,
            type(exc).__name__,
            exc,
        )
        return False


# LLM: 延迟补账必须携带同一份净化正文和产物 metadata，不能回退保存原始内部结果。
# 函数用途: assistant transcript 暂时写失败时保存可幂等修复的记录。
def _queue_gateway_conversation_repair(
    agent: SimpleAgent,
    request: dict,
    conversation: _GatewayConversationContext,
    *,
    request_id: str,
    role: str,
    content: str,
    delivery_artifacts: object = (),
) -> None:
    store = getattr(agent, "conversation_store", None)
    root = getattr(store, "root", None)
    if not root or not conversation.thread_id or not content:
        return
    metadata = request.get("metadata") if isinstance(request.get("metadata"), dict) else {}
    payload = {
        "thread_id": conversation.thread_id,
        "role": role,
        "content": content,
        "channel": str(metadata.get("channel") or request.get("source") or "gateway"),
        "channel_message_id": "",
        "metadata": {
            "gateway_request_id": request_id,
            "conversation_lane": conversation.lane,
            "repair": True,
            "delivery_artifacts": _metadata_artifact_refs(delivery_artifacts),
        },
    }
    path = Path(root) / "message_repairs" / f"{request_id}-{role}.json"
    try:
        from .io import write_json_file

        write_json_file(path, payload)
    except Exception as exc:
        logger.error("会话修复记录写入失败(%s): %s: %s", path, type(exc).__name__, exc)


def _repair_gateway_conversation_messages(
    store: object,
    thread_id: str,
    load_errors: list[dict],
) -> None:
    root = getattr(store, "root", None)
    repair_dir = Path(root) / "message_repairs" if root else None
    if repair_dir is None or not repair_dir.exists():
        return
    for path in sorted(repair_dir.glob("*.json"))[:100]:
        report = read_json_file_report(path, context="gateway.conversation.repair.read")
        if report.load_error is not None:
            load_errors.append(report.load_error)
            continue
        payload = report.payload
        if str(payload.get("thread_id") or "") != thread_id:
            continue
        try:
            rows, errors = store.recent_messages_report(thread_id, limit=200)
            if errors:
                load_errors.extend(errors)
                continue
            request_id = str((payload.get("metadata") or {}).get("gateway_request_id") or "")
            role = str(payload.get("role") or "")
            already_written = any(
                str(getattr(row, "role", "") or "") == role
                and str((getattr(row, "metadata", {}) or {}).get("gateway_request_id") or "")
                == request_id
                for row in rows
            )
            if not already_written:
                store.append_message(payload)
            path.unlink()
        except Exception as exc:
            load_errors.append(_conversation_error(exc, "gateway.conversation.repair.append"))


def _conversation_error(exc: BaseException, context: str) -> dict:
    from ..runtime_errors import runtime_error_report

    return runtime_error_report(exc, context=context)


def _stop_gateway_request_lease(
    lease_stop: threading.Event | None,
    lease_thread: threading.Thread | None,
) -> None:
    if lease_stop is not None:
        lease_stop.set()
    if lease_thread is not None:
        lease_thread.join(timeout=2)


def _copy_final_lease_fields(response: dict, request_path: Path) -> None:
    report = read_json_file_report(request_path, context="gateway.request_execution.final_request.read")
    if report.load_error is not None:
        response["final_request_load_error"] = report.load_error
        return
    final_request = report.payload
    if not final_request:
        return
    response["lease_owner"] = final_request.get("lease_owner", response.get("lease_owner", ""))
    response["lease_started_at"] = final_request.get("lease_started_at", response.get("lease_started_at", 0))
    response["lease_heartbeat_at"] = final_request.get(
        "lease_heartbeat_at",
        response.get("lease_heartbeat_at", 0),
    )


def _execute_gateway_request_body(context: dict, on_chunk) -> None:
    response = context["response"]
    kind = str(response.get("kind") or "").strip()
    if kind != "ask":
        response["error_code"] = "UNSUPPORTED_KIND"
        raise ValueError(f"unsupported gateway request kind: {kind or 'empty'}")
    try:
        result = _run_gateway_ask(
            _GatewayAskRunContext(
                context["agent"],
                context["request"],
                context["request_path"],
                context["response_path"],
                context["request_id"],
                on_chunk,
            )
        )
    except ValueError as exc:
        if str(exc) == _EMPTY_PROMPT_MESSAGE:
            response["error_code"] = "EMPTY_PROMPT"
        raise
    _update_response_from_result(response, result, context["request"])


def _prepare_gateway_request_context(agent: SimpleAgent, request_path: Path) -> dict:
    request_report = read_json_file_report(request_path, context="gateway.request_execution.request.read")
    if request_report.load_error is not None:
        started_at = time.time()
        response = gateway_request_load_error_response(
            request_path,
            request_report.load_error,
            started_at=started_at,
        )
        context = {
            "request": {},
            "request_id": str(request_path.stem),
            "kind": "unknown",
            "started_at": started_at,
            "request_path": request_path,
            "response_path": gateway_response_path(gateway_paths(agent), request_path.stem),
            "response": response,
            "skip_execution": True,
        }
        audit_request_processing(agent, context)
        return context
    request = request_report.payload
    request_id = str(request.get("id") or request_path.stem)
    kind = str(request.get("kind") or "").strip() or ("ask" if request_id else "")
    response_path = gateway_response_path(gateway_paths(agent), request_id)
    existing_response = read_gateway_response_file(
        response_path,
        request_id=request_id,
        context="gateway.request_execution.response.read",
    )
    if existing_response:
        return {"existing_response": existing_response}
    started_at = time.time()
    response = _build_gateway_response_base(
        _GatewayResponseBaseContext(request, request_path, request_id, kind, started_at)
    )
    context = {
        "request": request,
        "request_id": request_id,
        "kind": kind,
        "started_at": started_at,
        "request_path": request_path,
        "response_path": response_path,
        "response": response,
    }
    audit_request_processing(agent, context)
    return context


def _finalize_gateway_response(context: dict, response: dict) -> None:
    ended_at = time.time()
    _copy_final_lease_fields(response, context["request_path"])
    response["ended_at"] = ended_at
    response["duration_seconds"] = round(ended_at - context["started_at"], 3)


def _complete_gateway_request_audit(agent: SimpleAgent, context: dict, request_path: Path, response: dict) -> None:
    audit_request_completed(
        agent,
        params=AuditRequestCompletedParams(
            response=response,
            request=context["request"],
            request_path=request_path,
            response_path=context["response_path"],
        ),
    )


def _handle_gateway_request(
    agent: SimpleAgent,
    request_path: Path,
    *,
    refresh_lease: bool = False,
    worker_id: str = "",
) -> dict:
    context = _prepare_gateway_request_context(agent, request_path)
    if context.get("existing_response"):
        return context["existing_response"]
    response = context["response"]
    if context.get("skip_execution"):
        _finalize_gateway_response(context, response)
        _complete_gateway_request_audit(agent, context, request_path, response)
        return response
    lease_stop, lease_thread = _start_gateway_request_lease(
        _GatewayLeaseStartContext(
            agent,
            context["request"],
            request_path,
            context["request_id"],
            refresh_lease,
            worker_id,
        )
    )
    chunk_path = gateway_chunk_path(gateway_paths(agent), context["request_id"])
    chunk_path_abs, _ = open_chunk_stream(chunk_path)
    chunk_writer = BufferedChunkStreamWriter(chunk_path_abs)

    try:
        _execute_gateway_request_body({**context, "agent": agent}, chunk_writer.write)
    except Exception as exc:
        response.update(
            {
                "ok": False,
                "status": "failed",
                "error_code": response.get("error_code")
                or str(getattr(exc, "error_code", "") or type(exc).__name__.upper()),
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
    finally:
        _stop_gateway_request_lease(lease_stop, lease_thread)
        chunk_writer.close()
    _finalize_gateway_response(context, response)
    _complete_gateway_request_audit(agent, context, request_path, response)
    return response
